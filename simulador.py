"""
SIMULADOR DE ESTRATEGIAS — define tus propias reglas y pruebalas.

Escribes la estrategia en texto plano, eliges el timeframe, y el simulador la
prueba contra el historico con validacion out-of-sample. No hay que tocar codigo.

EJEMPLOS
--------
    # tu propia regla: comprar cuando RSI este bajo y el precio en banda inferior
    py simulador.py --tf 15m --call "rsi14 < 30 and bb_pct < 0.1" \
                             --put  "rsi14 > 70 and bb_pct > 0.9"

    # probar a varios timeframes de una vez
    py simulador.py --tf 5m,15m,30m --call "stoch_k < 15" --put "stoch_k > 85"

    # con filtro de sesion y de regimen
    py simulador.py --tf 15m --call "rsi7 < 25" --put "rsi7 > 75" \
                    --filtro "adx14 < 25 and 12 <= hora < 16"

    # ver que indicadores puedes usar
    py simulador.py --listar

    # buscar automaticamente (ojo: mina muchas combinaciones, ver aviso)
    py simulador.py --tf 15m --buscar

INDICADORES DISPONIBLES
-----------------------
    rsi7 rsi14 rsi21        0-100, sobrevendido <30, sobrecomprado >70
    stoch_k stoch_d         0-100
    williams_r              -100 a 0
    cci20                   tipicamente -200 a 200
    adx14                   0-100, fuerza de tendencia (>25 = tendencia clara)
    plus_di minus_di        0-100, direccion de la tendencia
    macd_line macd_signal macd_hist
    bb_pct                  0=banda inferior, 1=banda superior
    bb_width                ancho relativo de las bandas
    atr_pct                 volatilidad como % del precio
    roc5 roc10              momentum, % de cambio
    body_ratio              cuerpo/rango de la vela, 0-1
    wick_sup wick_inf       mechas relativas al rango
    dist_ema9 dist_ema21 dist_sma50   distancia % a cada media
    ema9_sobre_ema21        1 si EMA9 > EMA21
    racha                   velas consecutivas en la misma direccion (+/-)
    hora dow                hora UTC (0-23), dia de semana (0=lunes)
    vela_alcista            1 si cerro por encima de la apertura

REGLAS DE LA CASA
-----------------
El simulador NO te va a decir que algo es rentable solo porque el WR sea alto:
  * exige que el limite BAJO del IC95 supere el breakeven
  * separa train (70% pasado) y test (30% futuro) y te muestra ambos
  * te avisa si la tasa base del activo esta sesgada (deriva)
  * excluye empates (en IQ Option devuelven la apuesta)
"""
import argparse
import ast
import itertools
import math
import sqlite3

import numpy as np
import pandas as pd
from scipy import stats

PAYOUT_DEFECTO = 0.84

TFS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


# ───────────────────────── carga y resampleo ─────────────────────────

def cargar_tf(db, activo, segundos):
    """Carga velas del timeframe pedido; si no existe, las agrega desde 1m."""
    con = sqlite3.connect(db)
    df = pd.read_sql("SELECT ts,open,high,low,close FROM velas "
                     "WHERE activo=? AND timeframe=? ORDER BY ts",
                     con, params=(activo, segundos))
    if df.empty and segundos != 60:
        base = pd.read_sql("SELECT ts,open,high,low,close FROM velas "
                           "WHERE activo=? AND timeframe=60 ORDER BY ts",
                           con, params=(activo,))
        con.close()
        if base.empty:
            return base
        df = _agregar(base, segundos // 60)
    else:
        con.close()
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df.ts, unit="s", utc=True)
    df["hora"] = df.dt.dt.hour
    df["dow"] = df.dt.dt.dayofweek
    df["contigua"] = df.ts.diff().fillna(0).eq(segundos)
    return df.reset_index(drop=True)


def _agregar(base, mins):
    """Agrega velas de 1m a N minutos. Solo bloques completos."""
    d = base.copy()
    d["dt"] = pd.to_datetime(d.ts, unit="s", utc=True)
    d = d.set_index("dt")
    r = pd.DataFrame({
        "open": d.open.resample(f"{mins}min").first(),
        "high": d.high.resample(f"{mins}min").max(),
        "low": d.low.resample(f"{mins}min").min(),
        "close": d.close.resample(f"{mins}min").last(),
        "cuenta": d.close.resample(f"{mins}min").count(),
    }).dropna()
    r = r[r.cuenta == mins].drop(columns="cuenta").reset_index()
    r["ts"] = r.dt.astype("int64") // 10 ** 9
    return r[["ts", "open", "high", "low", "close"]]


def activos_disponibles(db, segundos):
    con = sqlite3.connect(db)
    r = [x[0] for x in con.execute(
        "SELECT activo FROM velas WHERE timeframe=? GROUP BY activo "
        "HAVING COUNT(*)>300 ORDER BY activo", (segundos,))]
    if not r and segundos != 60:
        r = [x[0] for x in con.execute(
            "SELECT activo FROM velas WHERE timeframe=60 GROUP BY activo "
            "HAVING COUNT(*)>5000 ORDER BY activo")]
    con.close()
    return r


# ───────────────────────── indicadores ─────────────────────────

def indicadores(df):
    c, h, l, o = df.close, df.high, df.low, df.open

    for p in (7, 14, 21):
        d = c.diff()
        g = d.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
        pr = (-d.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
        df[f"rsi{p}"] = 100 - 100 / (1 + g / pr.replace(0, np.nan))

    ll, hh = l.rolling(14).min(), h.rolling(14).max()
    rango14 = (hh - ll).replace(0, np.nan)
    df["stoch_k"] = 100 * (c - ll) / rango14
    df["stoch_d"] = df.stoch_k.rolling(3).mean()
    df["williams_r"] = -100 * (hh - c) / rango14

    tp = (h + l + c) / 3
    ma = tp.rolling(20).mean()
    md = (tp - ma).abs().rolling(20).mean()
    df["cci20"] = (tp - ma) / (0.015 * md.replace(0, np.nan))

    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["atr_pct"] = atr / c * 100
    up, dn = h.diff(), -l.diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr.replace(0, np.nan)
    df["plus_di"] = 100 * plus.ewm(alpha=1 / 14, adjust=False).mean() / a
    df["minus_di"] = 100 * minus.ewm(alpha=1 / 14, adjust=False).mean() / a
    dx = 100 * (df.plus_di - df.minus_di).abs() / (df.plus_di + df.minus_di).replace(0, np.nan)
    df["adx14"] = dx.ewm(alpha=1 / 14, adjust=False).mean()

    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    df["macd_line"] = e12 - e26
    df["macd_signal"] = df.macd_line.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df.macd_line - df.macd_signal

    m20, s20 = c.rolling(20).mean(), c.rolling(20).std()
    df["bb_pct"] = (c - (m20 - 2 * s20)) / (4 * s20).replace(0, np.nan)
    df["bb_width"] = (4 * s20) / m20.replace(0, np.nan)

    df["roc5"] = c.pct_change(5) * 100
    df["roc10"] = c.pct_change(10) * 100

    rango = (h - l).replace(0, np.nan)
    df["body_ratio"] = (c - o).abs() / rango
    df["wick_sup"] = (h - pd.concat([o, c], axis=1).max(axis=1)) / rango
    df["wick_inf"] = (pd.concat([o, c], axis=1).min(axis=1) - l) / rango
    df["vela_alcista"] = (c > o).astype(int)

    e9, e21 = c.ewm(span=9, adjust=False).mean(), c.ewm(span=21, adjust=False).mean()
    df["dist_ema9"] = (c - e9) / c * 100
    df["dist_ema21"] = (c - e21) / c * 100
    df["dist_sma50"] = (c - c.rolling(50).mean()) / c * 100
    df["ema9_sobre_ema21"] = (e9 > e21).astype(int)

    signo = np.sign(c - o)
    racha = np.zeros(len(df))
    for i in range(1, len(df)):
        racha[i] = racha[i - 1] + signo.iloc[i] if signo.iloc[i] == signo.iloc[i - 1] else signo.iloc[i]
    df["racha"] = racha
    return df


INDICADORES = ["rsi7", "rsi14", "rsi21", "stoch_k", "stoch_d", "williams_r", "cci20",
               "adx14", "plus_di", "minus_di", "macd_line", "macd_signal", "macd_hist",
               "bb_pct", "bb_width", "atr_pct", "roc5", "roc10", "body_ratio",
               "wick_sup", "wick_inf", "vela_alcista", "dist_ema9", "dist_ema21",
               "dist_sma50", "ema9_sobre_ema21", "racha", "hora", "dow"]


# ───────────────────────── evaluacion ─────────────────────────

class _Traductor(ast.NodeTransformer):
    """Convierte `and`/`or`/`not` y comparaciones encadenadas a operadores de pandas.

    Permite escribir las reglas en Python natural ("rsi14 < 30 and bb_pct < 0.1",
    "12 <= hora < 16") en lugar de la sintaxis de pandas con & | ~ y parentesis.
    """

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        op = ast.BitAnd() if isinstance(node.op, ast.And) else ast.BitOr()
        izq = node.values[0]
        for der in node.values[1:]:
            izq = ast.BinOp(left=izq, op=op, right=der)
        return ast.copy_location(izq, node)

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            return ast.copy_location(ast.UnaryOp(op=ast.Invert(), operand=node.operand), node)
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1:
            return node
        # a < b < c  ->  (a < b) & (b < c)
        partes, izq = [], node.left
        for op, der in zip(node.ops, node.comparators):
            partes.append(ast.Compare(left=izq, ops=[op], comparators=[der]))
            izq = der
        out = partes[0]
        for p in partes[1:]:
            out = ast.BinOp(left=out, op=ast.BitAnd(), right=p)
        return ast.copy_location(out, node)


def compilar_regla(expr):
    """Traduce la expresion del usuario a una que pandas pueda evaluar."""
    arbol = ast.parse(expr, mode="eval")
    arbol = _Traductor().visit(arbol)
    ast.fix_missing_locations(arbol)
    return compile(arbol, "<regla>", "eval")


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def simular(dfs, expr_call, expr_put, filtro=None):
    """Aplica las reglas y devuelve las operaciones con su resultado."""
    ops = []
    for activo, df in dfs.items():
        ctx = {c: df[c] for c in df.columns}
        ctx.update({"np": np, "pd": pd, "abs": abs})
        base = df.contigua.shift(-1).fillna(False) & (df.dow < 5)
        if filtro:
            try:
                base = base & eval(compilar_regla(filtro), {"__builtins__": {}}, ctx)
            except Exception as e:
                raise SystemExit(f"Error en --filtro: {e}")
        nxt = df.close.shift(-1)
        for expr, direc in ((expr_call, "call"), (expr_put, "put")):
            if not expr:
                continue
            try:
                sen = eval(compilar_regla(expr), {"__builtins__": {}}, ctx)
            except Exception as e:
                raise SystemExit(f"Error en la regla '{expr}': {e}")
            m = (base & sen).fillna(False)
            if not m.any():
                continue
            s, n2 = df[m], nxt[m]
            vivo = (n2 != s.close)          # excluir empates
            if not vivo.any():
                continue
            gano = ((n2 > s.close) if direc == "call" else (n2 < s.close))
            ops.append(pd.DataFrame({
                "ts": s.ts[vivo].values, "dt": s.dt[vivo].values, "activo": activo,
                "dir": direc, "gano": gano[vivo].astype(int).values,
                "sube": (n2 > s.close)[vivo].astype(int).values,
            }))
    if not ops:
        return pd.DataFrame()
    return pd.concat(ops).sort_values("ts").reset_index(drop=True)


def metricas(ops, payout):
    if ops is None or len(ops) == 0:
        return None
    n, k = len(ops), int(ops.gano.sum())
    be = 100 / (1 + payout)
    lo, hi = wilson(k, n)
    wr = 100 * k / n
    # tasa base: que daria operar SIEMPRE en esa direccion (controla la deriva)
    p_sube = ops.sube.mean()
    base = 100 * np.mean([p_sube if d == "call" else 1 - p_sube for d in ops["dir"]])
    return {
        "n": n, "wr": wr, "lo": lo, "hi": hi, "be": be,
        "ev": (k / n) * payout - (1 - k / n),
        "base": base, "lift": wr - base,
        "p_be": stats.binomtest(k, n, 1 / (1 + payout), alternative="greater").pvalue,
        "p_base": stats.binomtest(k, n, min(max(base / 100, 1e-9), 1 - 1e-9),
                                  alternative="greater").pvalue,
        "rentable": lo > be,
    }


def imprimir(m, etiqueta, indent="  "):
    if m is None:
        print(f"{indent}{etiqueta:<10} sin operaciones")
        return
    veredicto = "RENTABLE" if m["rentable"] else "no"
    print(f"{indent}{etiqueta:<10} n={m['n']:>7,}  WR={m['wr']:6.2f}%  "
          f"IC95=[{m['lo']:5.2f},{m['hi']:5.2f}]  base={m['base']:5.2f}%  "
          f"lift={m['lift']:+5.2f}  EV={m['ev']:+.4f}  {veredicto}")


def informe(ops, payout, titulo):
    print(f"\n{'=' * 96}\n{titulo}\n{'=' * 96}")
    g = metricas(ops, payout)
    if g is None:
        print("  Sin operaciones. Revisa las reglas o relaja los umbrales.")
        return None
    be = 100 / (1 + payout)
    print(f"  breakeven = {be:.2f}%  (payout {100 * payout:.0f}%)")
    imprimir(g, "GLOBAL")

    corte = int(len(ops) * 0.7)
    if len(ops) >= 200:
        imprimir(metricas(ops.iloc[:corte], payout), "train 70%")
        imprimir(metricas(ops.iloc[corte:], payout), "test 30%")

    if len(ops) >= 400:
        print("\n  Walk-forward (bloques cronologicos):")
        buenos = 0
        for i, b in enumerate(np.array_split(ops, 5)):
            mb = metricas(b, payout)
            if mb:
                ok = mb["wr"] > be
                buenos += ok
                print(f"    bloque {i + 1}  n={mb['n']:>6,}  WR={mb['wr']:6.2f}%  {'ok' if ok else '--'}")
        print(f"    por encima del breakeven: {buenos}/5")

    if ops.activo.nunique() > 1:
        print("\n  Por activo:")
        for a, sub in ops.groupby("activo"):
            ma = metricas(sub, payout)
            if ma and ma["n"] >= 40:
                print(f"    {a:10s} n={ma['n']:>6,}  WR={ma['wr']:6.2f}%  "
                      f"IC95=[{ma['lo']:.1f},{ma['hi']:.1f}]")
        arriba = sum(1 for a, s in ops.groupby("activo")
                     if len(s) >= 40 and 100 * s.gano.mean() > be)
        total = sum(1 for a, s in ops.groupby("activo") if len(s) >= 40)
        if total:
            print(f"    activos sobre breakeven: {arriba}/{total}")

    print("\n  VEREDICTO: ", end="")
    if g["rentable"] and g["p_base"] < 0.05:
        print("supera el breakeven Y la tasa base. Candidato serio.")
        print("  Antes de operarlo: repetir en otro periodo y confirmar en PRACTICE.")
    elif g["rentable"]:
        print("supera el breakeven, pero NO la tasa base del activo.")
        print("  Probablemente sea deriva del periodo, no habilidad del indicador.")
    else:
        falta = be - g["wr"]
        print(f"no supera el breakeven. Faltan {falta:+.2f} puntos.")
        pay_nec = (100 - g["wr"]) / g["wr"]
        print(f"  Con este WR haria falta un payout del {100 * pay_nec:.1f}%.")
    return g


# ───────────────────────── busqueda automatica ─────────────────────────

def buscar(dfs, payout, min_ops=300):
    """Mina reglas simples y pares. Minar sobre train, validar sobre test."""
    print("\n" + "=" * 96)
    print("BUSQUEDA AUTOMATICA")
    print("=" * 96)
    print("  AVISO: probar miles de reglas GARANTIZA encontrar ganadoras por azar.")
    print("  Por eso se mina solo en train y se valida en test. Fia solo de la")
    print("  columna 'test', y aun asi confirma en otro periodo antes de operar.\n")

    filas = []
    for a, df in dfs.items():
        base = df.contigua.shift(-1).fillna(False) & (df.dow < 5)
        nxt = df.close.shift(-1)
        sub = df[base].copy()
        n2 = nxt[base]
        vivo = (n2 != sub.close)
        sub = sub[vivo]
        sub["sube"] = (n2[vivo] > sub.close).astype(int)
        filas.append(sub)
    M = pd.concat(filas).sort_values("ts").reset_index(drop=True)
    usables = [c for c in INDICADORES if c in M.columns and M[c].notna().sum() > len(M) * 0.8]
    M = M.dropna(subset=usables)
    corte = int(len(M) * 0.7)
    tr, te = M.iloc[:corte], M.iloc[corte:]
    be = 100 / (1 + payout)
    print(f"  Muestras: {len(M):,}  (train {len(tr):,} / test {len(te):,})   breakeven {be:.2f}%\n")

    cond = []
    for f in usables:
        x = tr[f].values
        if len(np.unique(x)) < 5:
            continue
        for q in (0.05, 0.1, 0.2, 0.3, 0.7, 0.8, 0.9, 0.95):
            t = np.quantile(x, q)
            for op in (">", "<"):
                m = x > t if op == ">" else x < t
                if m.sum() < min_ops:
                    continue
                for direc in ("call", "put"):
                    g = tr.sube.values[m] if direc == "call" else 1 - tr.sube.values[m]
                    cond.append({"f": f, "op": op, "t": t, "dir": direc,
                                 "wr_tr": 100 * g.mean(), "n_tr": int(m.sum())})
    C = pd.DataFrame(cond).sort_values("wr_tr", ascending=False)

    def ev_test(reglas, direc):
        m = np.ones(len(te), dtype=bool)
        for f, op, t in reglas:
            x = te[f].values
            m &= (x > t) if op == ">" else (x < t)
        if m.sum() < min_ops // 2:
            return None
        g = te.sube.values[m] if direc == "call" else 1 - te.sube.values[m]
        n, k = len(g), int(g.sum())
        lo, hi = wilson(k, n)
        # La tasa base es la INCONDICIONAL sobre todo el periodo de test, no la
        # del subconjunto seleccionado: dentro del subconjunto coincidiria con el
        # propio WR y el lift saldria siempre 0.
        p_sube = te.sube.values.mean()
        base_r = 100 * (p_sube if direc == "call" else 1 - p_sube)
        return {"n": n, "wr": 100 * k / n, "lo": lo, "base": base_r,
                "lift": 100 * k / n - base_r, "rentable": lo > be}

    print(f"  {'regla':<42}{'dir':>5}{'n_te':>8}{'WR_te':>8}{'IC95lo':>9}{'lift':>7}  veredicto")
    print("  " + "-" * 92)
    ganadoras = []
    for _, r in C.head(12).iterrows():
        e = ev_test([(r.f, r.op, r.t)], r["dir"])
        if not e:
            continue
        v = "RENTABLE" if e["rentable"] else "no"
        if e["rentable"]:
            ganadoras.append((f"{r.f} {r.op} {r.t:.4g}", r["dir"], e))
        print(f"  {f'{r.f} {r.op} {r.t:.4g}':<42}{r['dir']:>5}{e['n']:>8,}"
              f"{e['wr']:>7.2f}%{e['lo']:>8.2f}%{e['lift']:>+7.2f}  {v}")

    print(f"\n  --- pares ---")
    vistos = set()
    for (_, a), (_, b) in itertools.combinations(C.head(30).iterrows(), 2):
        if a.f == b.f or a["dir"] != b["dir"]:
            continue
        clave = tuple(sorted([f"{a.f}{a.op}{a.t:.4g}", f"{b.f}{b.op}{b.t:.4g}"])) + (a["dir"],)
        if clave in vistos:
            continue
        vistos.add(clave)
        e = ev_test([(a.f, a.op, a.t), (b.f, b.op, b.t)], a["dir"])
        if e and e["rentable"]:
            ganadoras.append((f"{a.f} {a.op} {a.t:.4g} AND {b.f} {b.op} {b.t:.4g}", a["dir"], e))
    if ganadoras:
        print(f"  {'regla':<52}{'dir':>5}{'n_te':>8}{'WR_te':>8}{'lift':>7}")
        print("  " + "-" * 82)
        for nom, d, e in ganadoras[:12]:
            print(f"  {nom[:50]:<52}{d:>5}{e['n']:>8,}{e['wr']:>7.2f}%{e['lift']:>+7.2f}")
    else:
        print("  Ninguna combinacion supera el breakeven out-of-sample.")
    return ganadoras


def main():
    ap = argparse.ArgumentParser(description="Simulador de estrategias de opciones binarias")
    ap.add_argument("--db", default="velas.db")
    ap.add_argument("--tf", default="15m", help="timeframe(s): 1m,5m,15m,30m,1h")
    ap.add_argument("--activos", default=None)
    ap.add_argument("--call", default=None, help="condicion para comprar, ej: \"rsi14 < 30\"")
    ap.add_argument("--put", default=None, help="condicion para vender, ej: \"rsi14 > 70\"")
    ap.add_argument("--filtro", default=None, help="condicion extra aplicada a ambas")
    ap.add_argument("--payout", type=float, default=PAYOUT_DEFECTO)
    ap.add_argument("--buscar", action="store_true", help="busqueda automatica")
    ap.add_argument("--listar", action="store_true", help="lista los indicadores disponibles")
    args = ap.parse_args()

    if args.listar:
        print(__doc__)
        return
    if not args.buscar and not (args.call or args.put):
        ap.error("necesitas --call y/o --put, o bien --buscar. Usa --listar para ver indicadores.")

    for nombre_tf in [t.strip() for t in args.tf.split(",")]:
        if nombre_tf not in TFS:
            raise SystemExit(f"timeframe invalido: {nombre_tf}. Validos: {', '.join(TFS)}")
        seg = TFS[nombre_tf]
        activos = ([a.strip() for a in args.activos.split(",")]
                   if args.activos else activos_disponibles(args.db, seg))
        if not activos:
            raise SystemExit(f"sin datos. Corre: py descargar_velas.py --tf {nombre_tf}")

        dfs = {}
        for a in activos:
            d = cargar_tf(args.db, a, seg)
            if len(d) > 200:
                dfs[a] = indicadores(d)
        if not dfs:
            print(f"[{nombre_tf}] sin datos suficientes")
            continue

        total = sum(len(d) for d in dfs.values())
        cab = (f"TF {nombre_tf}  |  {len(dfs)} activos  |  {total:,} velas  |  "
               f"payout {100 * args.payout:.0f}%")
        if args.buscar:
            print(f"\n{'#' * 96}\n{cab}\n{'#' * 96}")
            buscar(dfs, args.payout)
        else:
            ops = simular(dfs, args.call, args.put, args.filtro)
            reglas = f"CALL: {args.call or '-'}   |   PUT: {args.put or '-'}"
            if args.filtro:
                reglas += f"   |   FILTRO: {args.filtro}"
            informe(ops, args.payout, f"{cab}\n{reglas}")


if __name__ == "__main__":
    main()
