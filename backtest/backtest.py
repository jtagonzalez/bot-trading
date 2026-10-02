import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Backtester de opciones binarias de 1 minuto sobre velas historicas (`velas.db`).

Reglas de la casa (no negociables, son las que evitan auto-enganarse):
  * La metrica es WIN RATE contra el BREAKEVEN, nunca el P&L.
  * Sin martingala. La martingala no crea edge; solo esconde su ausencia.
  * Todo resultado se valida OUT-OF-SAMPLE (temporal) + test de permutacion.
  * Entrada al CIERRE de la vela senal; salida al cierre de la vela siguiente.

Uso:
    py backtest.py                       # estrategia actual, todos los activos
    py backtest.py --activos EURUSD
    py backtest.py --sesion londres_ny   # solo horario de alta liquidez
    py backtest.py --comparar            # corre todas las estrategias registradas
"""
import argparse
import math
import sqlite3

import numpy as np
import pandas as pd

PAYOUT = 0.84                     # payout real observado en IQ Option
BREAKEVEN = 1 / (1 + PAYOUT)      # 54.35%

# Horarios en UTC. La sesion importa: fuera de ella el spread se come el edge.
SESIONES = {
    "todo":       (0, 24),
    "londres":    (7, 16),
    "ny":         (12, 21),
    "londres_ny": (12, 16),   # solape: maxima liquidez del dia
    "asia":       (23, 8),
}


# --------------------------- datos ---------------------------

def cargar(db, activo, tf=60):
    con = sqlite3.connect(db)
    df = pd.read_sql(
        "SELECT ts,open,high,low,close,volume FROM velas "
        "WHERE activo=? AND timeframe=? ORDER BY ts", con, params=(activo, tf))
    con.close()
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df.ts, unit="s", utc=True)
    df["hora"] = df.dt.dt.hour
    df["dow"] = df.dt.dt.dayofweek
    # Una vela solo es utilizable si la anterior es contigua (sin hueco de 60s)
    df["contigua"] = df.ts.diff().fillna(0).eq(tf)
    return df.reset_index(drop=True)


def activos_disponibles(db, tf=60):
    con = sqlite3.connect(db)
    r = [x[0] for x in con.execute(
        "SELECT activo FROM velas WHERE timeframe=? GROUP BY activo "
        "HAVING COUNT(*)>500 ORDER BY activo", (tf,))]
    con.close()
    return r


# ------------------------- indicadores -------------------------

def indicadores(df):
    c, h, l, o = df.close, df.high, df.low, df.open
    df["sma3"] = c.rolling(3).mean()
    df["sma50"] = c.rolling(50).mean()
    df["sma20"] = c.rolling(20).mean()
    df["ema9"] = c.ewm(span=9, adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()

    df["cuerpo"] = (c - o).abs()
    df["alcista"] = c > o
    df["bajista"] = c < o
    df["mecha_sup"] = h - pd.concat([o, c], axis=1).max(axis=1)
    df["mecha_inf"] = pd.concat([o, c], axis=1).min(axis=1) - l
    df["suma_mechas"] = df.mecha_sup + df.mecha_inf
    rango = (h - l).replace(0, np.nan)
    df["body_ratio"] = df.cuerpo / rango

    # RSI de Wilder
    d = c.diff()
    for p in (7, 14):
        g = d.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
        pr = (-d.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
        df[f"rsi{p}"] = 100 - 100 / (1 + g / pr.replace(0, np.nan))

    # ATR + ADX
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    up, dn = h.diff(), -l.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = df.atr14.replace(0, np.nan)
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / atr
    df["plus_di"], df["minus_di"] = pdi, mdi
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    df["adx14"] = dx.ewm(alpha=1 / 14, adjust=False).mean()

    # Bollinger + estocastico
    m20, s20 = c.rolling(20).mean(), c.rolling(20).std()
    df["bb_pct"] = (c - (m20 - 2 * s20)) / (4 * s20).replace(0, np.nan)
    df["bb_width"] = (4 * s20) / m20.replace(0, np.nan)
    ll, hh = l.rolling(14).min(), h.rolling(14).max()
    df["stoch_k"] = 100 * (c - ll) / (hh - ll).replace(0, np.nan)

    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    df["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()
    df["slope20_pct"] = (m20 - m20.shift(20)) / m20.shift(20).replace(0, np.nan) * 100
    df["atr_pct"] = df.atr14 / c * 100
    return df


# ------------------------- estrategias -------------------------
# Cada estrategia devuelve (serie_call, serie_put) alineadas al df.
ESTRATEGIAS = {}


def estrategia(nombre):
    def deco(fn):
        ESTRATEGIAS[nombre] = fn
        return fn
    return deco


@estrategia("actual")
def _actual(df, pct_cuerpo=0.60, max_mecha=1.0, incl=3):
    """Replica de la estrategia en produccion (SMA3/SMA50 + filtros de vela)."""
    up = df.sma50 > df.sma50.shift(incl)
    dn = df.sma50 < df.sma50.shift(incl)
    s, c = df.sma3, df.close
    cuerpo = df.cuerpo.replace(0, np.nan)
    pct_enc = ((c - s) / cuerpo).clip(0, 1).where(df.alcista, 0)
    pct_deb = ((s - c) / cuerpo).clip(0, 1).where(df.bajista, 0)
    mechas_ok = (df.cuerpo > 0) & (df.suma_mechas < df.cuerpo * max_mecha)

    # pullback: vela contraria en las 2 previas con cuerpo >= el de la senal
    pb_call = ((df.bajista.shift(1) & (df.cuerpo.shift(1) >= df.cuerpo)) |
               (df.bajista.shift(2) & (df.cuerpo.shift(2) >= df.cuerpo)))
    pb_put = ((df.alcista.shift(1) & (df.cuerpo.shift(1) >= df.cuerpo)) |
              (df.alcista.shift(2) & (df.cuerpo.shift(2) >= df.cuerpo)))

    toca = (df.low <= df.sma50) & (df.sma50 <= df.high)
    sin_cruce = toca.shift(1).rolling(15).sum().eq(0)

    call = up & (s > df.sma50) & df.alcista & (pct_enc >= pct_cuerpo) & mechas_ok & pb_call & sin_cruce
    put = dn & (s < df.sma50) & df.bajista & (pct_deb >= pct_cuerpo) & mechas_ok & pb_put & sin_cruce
    return call.fillna(False), put.fillna(False)


@estrategia("reversion_bandas")
def _reversion(df, rsi_lo=25, rsi_hi=75):
    """Reversion a la media: extremo de Bollinger + RSI extremo + mercado calmo."""
    calmo = df.adx14 < 25
    call = calmo & (df.bb_pct < 0.02) & (df.rsi14 < rsi_lo)
    put = calmo & (df.bb_pct > 0.98) & (df.rsi14 > rsi_hi)
    return call.fillna(False), put.fillna(False)


@estrategia("momentum_adx")
def _momentum(df, adx_min=30):
    """Continuacion de tendencia fuerte con DI confirmando."""
    fuerte = df.adx14 > adx_min
    call = fuerte & (df.plus_di > df.minus_di) & (df.close > df.ema21) & df.alcista
    put = fuerte & (df.minus_di > df.plus_di) & (df.close < df.ema21) & df.bajista
    return call.fillna(False), put.fillna(False)


@estrategia("cruce_ema")
def _cruce(df):
    """Cruce EMA9/EMA21 recien producido."""
    up = (df.ema9 > df.ema21) & (df.ema9.shift(1) <= df.ema21.shift(1))
    dn = (df.ema9 < df.ema21) & (df.ema9.shift(1) >= df.ema21.shift(1))
    return up.fillna(False), dn.fillna(False)


@estrategia("reversion_rsi_stoch")
def _rev_rsi_stoch(df, rsi=30, st=10):
    """Reversion a la media: el UNICO edge real encontrado (validado 2026-09-20).

    Sobre 614.500 velas de 10 pares reales: 51.90% WR con n=95.357, p<0.000001,
    y 10/10 activos por encima del 50%. Subiendo la exigencia llega a ~53%.

    OJO: sigue SIN ser rentable a payout 84% (breakeven 54.35%). Necesitaria
    payout 88.7%. Queda aqui documentada porque es la unica familia con senal
    medible — todas las de momentum quedaron por debajo del azar.
    """
    call = (df.rsi7 < rsi) & (df.stoch_k < st)
    put = (df.rsi7 > 100 - rsi) & (df.stoch_k > 100 - st)
    return call.fillna(False), put.fillna(False)


@estrategia("aleatoria")
def _azar(df, seed=0):
    """Control: entradas al azar. Cualquier estrategia debe superar a esta."""
    rng = np.random.default_rng(seed)
    r = rng.random(len(df))
    return pd.Series(r < 0.02, index=df.index), pd.Series(r > 0.98, index=df.index)


# --------------------------- motor ---------------------------

def operar(df, call, put, sesion="todo"):
    """Genera las operaciones. Entrada al cierre de i, resultado en el cierre de i+1."""
    h0, h1 = SESIONES[sesion]
    en_sesion = (df.hora >= h0) & (df.hora < h1) if h0 < h1 else (df.hora >= h0) | (df.hora < h1)
    # la vela siguiente debe existir y ser contigua (sin hueco de fin de semana)
    sig_ok = df.contigua.shift(-1).fillna(False).astype(bool)
    hab = en_sesion & sig_ok & (df.dow < 5)

    c_next = df.close.shift(-1)
    ops = []
    for direc, sen in (("call", call), ("put", put)):
        m = (sen & hab).fillna(False).values
        if not m.any():
            continue
        sub = df[m]
        nxt = c_next[m]
        gano = (nxt > sub.close) if direc == "call" else (nxt < sub.close)
        empate = nxt == sub.close
        t = pd.DataFrame({
            "ts": sub.ts.values, "dt": sub.dt.values, "hora": sub.hora.values,
            "direccion": direc, "entrada": sub.close.values, "salida": nxt.values,
            "gano": gano.values.astype(int), "empate": empate.values.astype(int),
        })
        # retorno con signo: cuanto se movio a favor (en puntos de precio)
        t["ret"] = (t.salida - t.entrada) * (1 if direc == "call" else -1)
        ops.append(t)
    if not ops:
        return pd.DataFrame()
    return pd.concat(ops).sort_values("ts").reset_index(drop=True)


def wilson(k, n, z=1.96):
    """Intervalo de confianza de Wilson: fiable tambien con n pequeno."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def evaluar(ops, etiqueta=""):
    """Metricas honestas: WR con intervalo de confianza y EV, nunca P&L de martingala."""
    if ops is None or len(ops) == 0:
        return None
    val = ops[ops.empate == 0]
    n, k = len(val), int(val.gano.sum())
    if n == 0:
        return None
    wr = 100 * k / n
    lo, hi = wilson(k, n)
    ev = (k / n) * PAYOUT - (1 - k / n)
    from scipy import stats
    p = stats.binomtest(k, n, BREAKEVEN, alternative="greater").pvalue
    return {"etiqueta": etiqueta, "n": n, "wr": wr, "ic_lo": lo, "ic_hi": hi,
            "ev": ev, "p": p, "ret_medio": val.ret.mean(),
            "supera_be": lo > 100 * BREAKEVEN}


def fmt(r):
    if r is None:
        return "    sin operaciones"
    ok = "SI" if r["supera_be"] else "no"
    return (f"    n={r['n']:>6,}  WR={r['wr']:5.2f}%  IC95=[{r['ic_lo']:5.2f},{r['ic_hi']:5.2f}]  "
            f"EV={r['ev']:+.4f}  p={r['p']:.3f}  supera_breakeven={ok}")


def validacion_temporal(ops, frac=0.7):
    if len(ops) < 100:
        return None, None
    corte = int(len(ops) * frac)
    return evaluar(ops.iloc[:corte], "train"), evaluar(ops.iloc[corte:], "test")


def walk_forward(ops, bloques=5):
    """Divide el historico en bloques cronologicos: un edge real aparece en la mayoria."""
    if len(ops) < bloques * 40:
        return []
    out = []
    for i, b in enumerate(np.array_split(ops, bloques)):
        r = evaluar(b, f"bloque {i + 1}")
        if r:
            out.append(r)
    return out


def permutacion(ops, n_perm=1000, seed=0):
    """Bajo H0 cada operacion gana con prob = breakeven. Probabilidad de ver este WR."""
    val = ops[ops.empate == 0]
    n, k = len(val), int(val.gano.sum())
    if n == 0:
        return 1.0
    rng = np.random.default_rng(seed)
    sim = rng.binomial(n, BREAKEVEN, n_perm)
    return float((sim >= k).mean())


def correr(db, activos, nombre_estrategia, sesion, **kw):
    fn = ESTRATEGIAS[nombre_estrategia]
    todas = []
    for a in activos:
        df = cargar(db, a)
        if len(df) < 200:
            continue
        df = indicadores(df)
        call, put = fn(df, **kw)
        ops = operar(df, call, put, sesion)
        if not ops.empty:
            ops["activo"] = a
            todas.append(ops)
    if not todas:
        return pd.DataFrame()
    return pd.concat(todas).sort_values("ts").reset_index(drop=True)


def informe(ops, nombre, sesion):
    print(f"\n{'=' * 82}")
    print(f"ESTRATEGIA: {nombre}   |   sesion: {sesion}   |   breakeven: {100 * BREAKEVEN:.2f}%")
    print("=" * 82)
    g = evaluar(ops, "global")
    if g is None:
        print("    sin operaciones")
        return
    print("  GLOBAL")
    print(fmt(g))
    print(f"    p-valor permutacion (1000 sims): {permutacion(ops):.4f}")

    tr, te = validacion_temporal(ops)
    if tr:
        print("\n  VALIDACION TEMPORAL (70% pasado -> 30% futuro)")
        print(f"    TRAIN {fmt(tr).strip()}")
        print(f"    TEST  {fmt(te).strip()}")

    wf = walk_forward(ops)
    if wf:
        print("\n  WALK-FORWARD (bloques cronologicos)")
        for r in wf:
            marca = "ok" if r["wr"] > 100 * BREAKEVEN else "--"
            print(f"    {r['etiqueta']:10s} n={r['n']:>5,}  WR={r['wr']:5.2f}%  {marca}")
        buenos = sum(1 for r in wf if r["wr"] > 100 * BREAKEVEN)
        print(f"    bloques por encima del breakeven: {buenos}/{len(wf)}")

    if "activo" in ops.columns and ops.activo.nunique() > 1:
        print("\n  POR ACTIVO")
        for a, sub in ops.groupby("activo"):
            r = evaluar(sub, a)
            if r and r["n"] >= 50:
                print(f"    {a:12s} n={r['n']:>5,}  WR={r['wr']:5.2f}%  "
                      f"IC95=[{r['ic_lo']:.1f},{r['ic_hi']:.1f}]")

    print("\n  POR HORA (UTC)")
    hh = ops[ops.empate == 0].groupby("hora").gano.agg(["size", "mean"])
    hh = hh[hh["size"] >= 30]
    for h, row in hh.iterrows():
        marca = " <--" if 100 * row["mean"] > 100 * BREAKEVEN else ""
        print(f"    {int(h):02d}:00  n={int(row['size']):>5,}  WR={100 * row['mean']:5.2f}%{marca}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="velas.db")
    ap.add_argument("--estrategia", default="actual", choices=list(ESTRATEGIAS))
    ap.add_argument("--activos", default=None)
    ap.add_argument("--sesion", default="todo", choices=list(SESIONES))
    ap.add_argument("--comparar", action="store_true")
    args = ap.parse_args()

    activos = ([a.strip() for a in args.activos.split(",")]
               if args.activos else activos_disponibles(args.db))
    if not activos:
        raise SystemExit(f"{args.db} no tiene velas. Corre primero: py descargar_velas.py")
    print(f"Activos: {', '.join(activos)}")

    if args.comparar:
        print(f"\n{'estrategia':<20}{'n':>8}{'WR':>9}{'IC95 bajo':>12}{'p':>9}  veredicto")
        print("-" * 72)
        for nom in ESTRATEGIAS:
            ops = correr(args.db, activos, nom, args.sesion)
            r = evaluar(ops, nom)
            if r is None:
                print(f"{nom:<20}{'sin ops':>8}")
                continue
            v = "RENTABLE" if r["supera_be"] else "no supera breakeven"
            print(f"{nom:<20}{r['n']:>8,}{r['wr']:>8.2f}%{r['ic_lo']:>11.2f}%{r['p']:>9.3f}  {v}")
        print(f"\nbreakeven = {100 * BREAKEVEN:.2f}%  (payout {100 * PAYOUT:.0f}%)")
        print("'RENTABLE' exige que el limite BAJO del IC95 supere el breakeven.")
    else:
        ops = correr(args.db, args.activos and activos or activos, args.estrategia, args.sesion)
        informe(ops, args.estrategia, args.sesion)


if __name__ == "__main__":
    main()
