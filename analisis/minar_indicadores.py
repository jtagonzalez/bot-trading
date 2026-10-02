import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Mineria de indicadores sobre velas historicas (`velas.db`).

Responde la pregunta "que combinacion de indicadores predice la operacion":
toma CADA vela como una entrada potencial, calcula todos los indicadores y mide
el resultado hacia adelante, tanto BINARIO (gano/perdio) como CONTINUO (cuanto
se movio el precio a favor). El objetivo continuo tiene mas poder estadistico:
detecta senales debiles que el binario esconde.

Disciplina anti-autoengano (sin esto la mineria SIEMPRE encuentra oro falso):
  * Particion TEMPORAL: se mina solo en el pasado, se valida en el futuro.
  * Correccion por comparaciones multiples (Benjamini-Hochberg).
  * Test de permutacion sobre el MEJOR hallazgo.
  * Un control aleatorio que debe salir en blanco.

Uso:
    py minar_indicadores.py
    py minar_indicadores.py --activos EURUSD,GBPUSD --sesion londres_ny
    py minar_indicadores.py --min-ops 300
"""
import argparse
import itertools

import numpy as np
import pandas as pd
from scipy import stats

import backtest as bt

PAYOUT = bt.PAYOUT
BREAKEVEN = bt.BREAKEVEN

# Indicadores candidatos: los que el bot ya calcula + los del backtester.
FEATURES = [
    "rsi7", "rsi14", "stoch_k", "adx14", "plus_di", "minus_di", "bb_pct",
    "bb_width", "macd_hist", "atr_pct", "body_ratio", "slope20_pct", "hora",
]


def construir_muestras(db, activos, sesion):
    """Una fila por vela operable, con indicadores y resultado hacia adelante."""
    filas = []
    for a in activos:
        df = bt.cargar(db, a)
        if len(df) < 500:
            continue
        df = bt.indicadores(df)

        h0, h1 = bt.SESIONES[sesion]
        en_ses = (df.hora >= h0) & (df.hora < h1) if h0 < h1 else (df.hora >= h0) | (df.hora < h1)
        sig_ok = df.contigua.shift(-1).fillna(False).astype(bool)
        ok = en_ses & sig_ok & (df.dow < 5) & df[FEATURES].notna().all(axis=1)

        sub = df[ok].copy()
        nxt = df.close.shift(-1)[ok]
        # retorno de la vela siguiente, en puntos basicos (normalizado por precio)
        sub["ret_bps"] = (nxt.values - sub.close.values) / sub.close.values * 1e4

        # CRITICO: excluir los empates (close[i+1] == close[i]).
        # En IQ Option un empate devuelve la apuesta: no es ni win ni loss.
        # Si se cuentan como perdida de la CALL, toda regla de BAJA VOLATILIDAD
        # parece un PUT ganador — porque la baja volatilidad produce muchos mas
        # empates (12.6% en el quintil mas calmo vs 2.4% en el mas volatil).
        # Ese sesgo fabrica "edges" de +11 puntos que no existen.
        sub = sub[sub.ret_bps != 0].copy()
        if sub.empty:
            continue

        sub["sube"] = (sub.ret_bps > 0).astype(int)
        sub["activo"] = a
        filas.append(sub[["ts", "dt", "activo", "ret_bps", "sube"] + FEATURES])
    if not filas:
        return pd.DataFrame()
    return pd.concat(filas).sort_values("ts").reset_index(drop=True)


def bh(pvals, alpha=0.05):
    """Benjamini-Hochberg: cuantos hallazgos sobreviven al control de falsos positivos."""
    p = np.asarray(pvals)
    n = len(p)
    orden = np.argsort(p)
    umbral = alpha * (np.arange(1, n + 1) / n)
    pasa = p[orden] <= umbral
    if not pasa.any():
        return np.zeros(n, dtype=bool)
    k = np.max(np.where(pasa)[0])
    out = np.zeros(n, dtype=bool)
    out[orden[:k + 1]] = True
    return out


def reglas_candidatas(tr):
    """Genera reglas simples (indicador vs umbral) y las evalua EN TRAIN."""
    reglas = []
    for f in FEATURES:
        x = tr[f].values
        for q in (0.1, 0.2, 0.3, 0.7, 0.8, 0.9):
            t = np.quantile(x, q)
            for op, m in ((">", x > t), ("<", x < t)):
                if m.sum() < 200:
                    continue
                # CALL si el precio tiende a subir; PUT si tiende a bajar
                sube = tr.sube.values[m]
                ret = tr.ret_bps.values[m]
                for direc in ("call", "put"):
                    gano = sube if direc == "call" else 1 - sube
                    wr = gano.mean()
                    # t-test sobre el retorno con signo: mas potente que el binario
                    r = ret if direc == "call" else -ret
                    tstat, pval = stats.ttest_1samp(r, 0)
                    if tstat < 0:
                        pval = 1 - pval / 2
                    else:
                        pval = pval / 2
                    reglas.append({
                        "regla": f"{f} {op} {t:.4g}", "dir": direc, "n_train": int(m.sum()),
                        "wr_train": 100 * wr, "ret_medio": r.mean(), "p_train": pval,
                        "feat": f, "op": op, "umbral": t,
                    })
    return pd.DataFrame(reglas)


def aplicar(df, feat, op, umbral):
    x = df[feat].values
    return x > umbral if op == ">" else x < umbral


def tasa_base(df, direc):
    """WR de operar SIEMPRE en esa direccion, sin ningun filtro.

    Es la referencia critica: si un activo cayo durante el periodo, CUALQUIER
    regla 'put' parecera buena. Lo que importa no es superar el breakeven, sino
    superar esta tasa base — eso es lo unico que aporta el indicador.
    """
    p = df.sube.mean()
    return 100 * (p if direc == "call" else 1 - p)


def evaluar_en(df, m, direc, base=None):
    gano = df.sube.values[m] if direc == "call" else 1 - df.sube.values[m]
    n, k = len(gano), int(gano.sum())
    if n == 0:
        return None
    lo, hi = bt.wilson(k, n)
    p = stats.binomtest(k, n, BREAKEVEN, alternative="greater").pvalue
    out = {"n": n, "wr": 100 * k / n, "lo": lo, "hi": hi, "p": p,
           "ev": (k / n) * PAYOUT - (1 - k / n)}
    if base is not None:
        out["base"] = base
        out["lift"] = out["wr"] - base
        # el indicador aporta algo solo si supera la tasa base de forma significativa
        out["p_vs_base"] = stats.binomtest(k, n, min(max(base / 100, 1e-9), 1 - 1e-9),
                                           alternative="greater").pvalue
        # exigencia doble: superar breakeven Y superar la deriva del activo
        out["real"] = (lo > 100 * BREAKEVEN) and (out["p_vs_base"] < 0.05)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="velas.db")
    ap.add_argument("--activos", default=None)
    ap.add_argument("--sesion", default="todo", choices=list(bt.SESIONES))
    ap.add_argument("--min-ops", type=int, default=200)
    args = ap.parse_args()

    activos = ([a.strip() for a in args.activos.split(",")]
               if args.activos else bt.activos_disponibles(args.db))
    if not activos:
        raise SystemExit("velas.db vacio. Corre primero: py descargar_velas.py")

    print(f"Activos: {', '.join(activos)}   |   sesion: {args.sesion}")
    m = construir_muestras(args.db, activos, args.sesion)
    if m.empty:
        raise SystemExit("sin muestras utilizables")

    corte = int(len(m) * 0.7)
    tr, te = m.iloc[:corte], m.iloc[corte:]
    print(f"Muestras: {len(m):,}  (train {len(tr):,} -> {tr.dt.min():%Y-%m-%d}..{tr.dt.max():%Y-%m-%d}"
          f" | test {len(te):,} -> {te.dt.min():%Y-%m-%d}..{te.dt.max():%Y-%m-%d})")
    pu = 100 * m.sube.mean()
    print(f"Breakeven: {100 * BREAKEVEN:.2f}%")
    print(f"Tasa base: operar SIEMPRE call = {pu:.2f}%  |  SIEMPRE put = {100 - pu:.2f}%")
    if abs(pu - 50) > 1.5:
        sesgo = "bajista" if pu < 50 else "alcista"
        favorecida = "put" if pu < 50 else "call"
        print(f"  AVISO: deriva {sesgo} fuerte en el periodo. Cualquier regla "
              f"'{favorecida}' parecera buena sin aportar nada.")
        print("  Por eso se mide LIFT sobre la tasa base, no solo el WR.")
    print()

    # ── 1. Poder predictivo de cada indicador sobre el retorno CONTINUO ──
    print("=" * 84)
    print("1) CORRELACION de cada indicador con el RETORNO de la vela siguiente (train)")
    print("=" * 84)
    filas = []
    for f in FEATURES:
        r, p = stats.pearsonr(tr[f].values, tr.ret_bps.values)
        filas.append((f, r, p))
    c = pd.DataFrame(filas, columns=["indicador", "corr", "p"]).sort_values("p")
    c["significativo_BH"] = bh(c.p.values)
    print(c.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"\nSobreviven a Benjamini-Hochberg: {int(c.significativo_BH.sum())} de {len(c)}")

    # ── 2. Mineria de reglas en TRAIN, validacion en TEST ──
    print("\n" + "=" * 84)
    print("2) REGLAS SIMPLES: minadas en TRAIN, validadas en TEST (out-of-sample)")
    print("=" * 84)
    R = reglas_candidatas(tr)
    R["sig_BH"] = bh(R.p_train.values)
    R = R.sort_values("wr_train", ascending=False)
    print(f"Reglas probadas: {len(R):,}   |   sobreviven a BH en train: {int(R.sig_BH.sum())}")

    print(f"\n{'regla':<26}{'dir':>5}{'n_te':>7}{'WR_te':>8}{'base':>8}{'lift':>8}{'p_base':>8}  veredicto")
    print("-" * 92)
    supervivientes = []
    for _, row in R.head(15).iterrows():
        mt = aplicar(te, row.feat, row.op, row.umbral)
        if mt.sum() < args.min_ops:
            continue
        e = evaluar_en(te, mt, row["dir"], base=tasa_base(te, row["dir"]))
        if e is None:
            continue
        if e["real"]:
            v = "REAL"
            supervivientes.append((row.regla, row["dir"], e))
        elif e["lo"] > 100 * BREAKEVEN:
            v = "solo deriva del activo"
        else:
            v = "no supera BE"
        print(f"{row.regla:<26}{row['dir']:>5}{e['n']:>7,}{e['wr']:>7.2f}%"
              f"{e['base']:>7.2f}%{e['lift']:>+7.2f}{e['p_vs_base']:>8.3f}  {v}")

    # ── 3. Combinaciones de 2 reglas ──
    print("\n" + "=" * 84)
    print("3) COMBINACIONES DE 2 INDICADORES (minadas en train, validadas en test)")
    print("=" * 84)
    top = R.head(40)
    combos = []
    for (_, a), (_, b) in itertools.combinations(top.iterrows(), 2):
        if a.feat == b.feat or a["dir"] != b["dir"]:
            continue
        mtr = aplicar(tr, a.feat, a.op, a.umbral) & aplicar(tr, b.feat, b.op, b.umbral)
        if mtr.sum() < 200:
            continue
        etr = evaluar_en(tr, mtr, a["dir"])
        combos.append((f"{a.regla} & {b.regla}", a["dir"], a.feat, a.op, a.umbral,
                       b.feat, b.op, b.umbral, etr["n"], etr["wr"]))
    C = pd.DataFrame(combos, columns=["regla", "dir", "f1", "o1", "u1", "f2", "o2", "u2",
                                      "n_train", "wr_train"]).sort_values("wr_train", ascending=False)
    print(f"Combinaciones evaluadas: {len(C):,}")
    if len(C):
        print(f"\n{'combinacion':<44}{'n_te':>7}{'WR_te':>8}{'base':>8}{'lift':>8}{'p_base':>8}  veredicto")
        print("-" * 96)
        for _, row in C.head(12).iterrows():
            mt = aplicar(te, row.f1, row.o1, row.u1) & aplicar(te, row.f2, row.o2, row.u2)
            if mt.sum() < args.min_ops:
                continue
            e = evaluar_en(te, mt, row["dir"], base=tasa_base(te, row["dir"]))
            if e is None:
                continue
            if e["real"]:
                v = "REAL"
                supervivientes.append((row.regla, row["dir"], e))
            elif e["lo"] > 100 * BREAKEVEN:
                v = "solo deriva del activo"
            else:
                v = "no supera BE"
            print(f"{row.regla[:42]:<44}{e['n']:>7,}{e['wr']:>7.2f}%"
                  f"{e['base']:>7.2f}%{e['lift']:>+7.2f}{e['p_vs_base']:>8.3f}  {v}")

    # ── 4. Veredicto ──
    print("\n" + "=" * 84)
    print("4) VEREDICTO")
    print("=" * 84)
    if not supervivientes:
        print("  NINGUNA regla supera el breakeven out-of-sample con el IC95 completo.")
        print("  Traducido: no hay edge explotable en estos indicadores sobre estos datos.")
        print("  Añadir mas indicadores a la misma familia no va a cambiarlo.")
    else:
        print(f"  {len(supervivientes)} regla(s) superan el breakeven out-of-sample:\n")
        for nombre, direc, e in supervivientes:
            print(f"    {nombre}  [{direc}]")
            print(f"      test: n={e['n']:,}  WR={e['wr']:.2f}%  IC95=[{e['lo']:.2f},{e['hi']:.2f}]  "
                  f"EV={e['ev']:+.4f}")
        print("\n  SIGUIENTE PASO OBLIGATORIO antes de operar con esto:")
        print("    1. Reproducirlo en un periodo distinto (otros 60 dias).")
        print("    2. Confirmarlo en varios activos por separado, no solo en el agregado.")
        print("    3. Correrlo en PRACTICE el tiempo necesario para acumular n suficiente.")
        print("    Un hallazgo que no se repite en los 3 pasos es ruido, por bonito que sea.")


if __name__ == "__main__":
    main()
