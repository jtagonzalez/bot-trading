import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
¿Qué pares, además de EURUSD, tienen una ventaja que aguante?

Misma prueba que pasó EURUSD, ampliada a 20 pares (los 10 de siempre + 10
nuevos que la cuenta puede operar como '-op'):

  1. Walk-forward en 8 bloques: el modelo nunca ve el bloque que se evalúa.
  2. El umbral de confianza (10% más confiable) se fija SOLO con los datos de
     entrenamiento de cada bloque — igual que opera el bot en vivo.
  3. Cada par se juzga contra SU breakeven: EURUSD y GBPUSD pagan 90%
     (52,63%); el resto 87% (53,48%).
  4. Corrección de Benjamini-Hochberg sobre los 20 pares.
  5. Estabilidad: el par tiene que superar su breakeven en las DOS mitades
     del periodo, no solo en el total.

Un par entra al bot solo si pasa 4 y 5.

    py validar_pares.py
"""
import math
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

import entrenar_modelo as E
import ml_walkforward as ML

VIEJOS = ["AUDUSD", "EURGBP", "EURJPY", "EURUSD", "GBPJPY",
          "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]
NUEVOS = ["AUDCHF", "CADCHF", "EURAUD", "EURCAD", "EURCHF",
          "EURNZD", "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD"]
PAYOUT = {}   # turbo 5m: 86% (comision 14%)
NO_OPERABLE = {"USDCHF"}                            # USDCHF-op no existe
PCT = 0.10


def be(par):
    return 100 / (1 + PAYOUT.get(par, 0.86))


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def bh(p):
    p = np.asarray(p, dtype=float)
    orden = np.argsort(p)
    m = len(p)
    q = np.empty(m)
    prev = 1.0
    for rank in range(m, 0, -1):
        i = orden[rank - 1]
        prev = min(prev, p[i] * m / rank)
        q[i] = prev
    return q


def main():
    print("Preparando 10 pares a 5m, 1-17h UTC...")
    M = E.preparar("velas.db", 300, (1, 17), VIEJOS)
    print(f"  {len(M):,} muestras · {M.activo.nunique()} pares · "
          f"{M.dt.min():%Y-%m-%d} a {M.dt.max():%Y-%m-%d}")
    faltan = sorted(set(VIEJOS + NUEVOS) - set(M.activo.unique()))
    if faltan:
        print(f"  SIN DATOS: {', '.join(faltan)}")

    feats = [f for f in ML.FEATURES if f in M.columns]
    X = M[feats].replace([np.inf, -np.inf], np.nan)
    X = X.fillna(X.median()).values
    y = M.sube.values

    cortes = np.linspace(0, len(X), 9).astype(int)
    partes = []
    for i in range(1, 8):
        trf, ti, tf_ = cortes[i], cortes[i], cortes[i + 1]
        m = ML.crear_modelo("gb")
        m.fit(X[:trf], y[:trf])
        umbral = np.quantile(np.abs(m.predict_proba(X[:trf])[:, 1] - 0.5), 1 - PCT)
        p = m.predict_proba(X[ti:tf_])[:, 1]
        sub = M.iloc[ti:tf_][["activo", "dt", "hora", "sube"]].copy()
        sub["p"] = p
        sub["operaria"] = np.abs(p - 0.5) >= umbral
        partes.append(sub)
        print(f"  bloque {i}: umbral {umbral:.4f} · operaria {sub.operaria.sum():,} "
              f"de {len(sub):,}")
    D = pd.concat(partes)
    D["gano"] = np.where(D.p > 0.5, D.sube, 1 - D.sube)
    D.to_pickle("oos_5m.pkl")
    S = D[D.operaria]
    dias = D.dt.dt.normalize().nunique()
    corte_t = S.dt.quantile(0.5)

    filas = []
    for par, g in S.groupby("activo"):
        n, k = len(g), int(g.gano.sum())
        b = be(par)
        pv = stats.binomtest(k, n, b / 100, alternative="greater").pvalue
        g1, g2 = g[g.dt <= corte_t], g[g.dt > corte_t]
        w1 = 100 * g1.gano.mean() if len(g1) else 0
        w2 = 100 * g2.gano.mean() if len(g2) else 0
        filas.append(dict(par=par, n=n, wr=100 * k / n, ic=wilson(k, n), be=b,
                          p=pv, w1=w1, w2=w2, ops=n / dias,
                          ev=(k / n) * PAYOUT.get(par, 0.86) - (1 - k / n)))
    R = pd.DataFrame(filas)
    R["q"] = bh(R.p)
    R["pasa"] = (R.q < 0.05) & (R.w1 > R.be) & (R.w2 > R.be) & \
        (~R.par.isin(NO_OPERABLE))
    R = R.sort_values("wr", ascending=False)

    print("\n" + "=" * 100)
    print("CADA PAR, OPERANDO EL 10% MÁS CONFIABLE (umbral fijado solo con entrenamiento)")
    print("=" * 100)
    print(f"  {'par':<8}{'n':>6}{'WR':>8}{'IC95':>16}{'BE':>7}{'q(BH)':>8}"
          f"{'1a mitad':>10}{'2a mitad':>10}{'ops/día':>9}{'$/día':>8}  veredicto")
    for _, r in R.iterrows():
        nuevo = "*" if r.par in NUEVOS else " "
        if r.pasa:
            v = "PASA"
        elif r.par in NO_OPERABLE:
            v = "no operable"
        elif r.wr > r.be:
            v = "sobre BE, no concluyente"
        else:
            v = "no"
        print(f" {nuevo}{r.par:<8}{r.n:>6,}{r.wr:>7.2f}%  [{r.ic[0]:5.2f},{r.ic[1]:5.2f}]"
              f"{r.be:>6.2f}%{r.q:>8.3f}{r.w1:>9.1f}%{r.w2:>9.1f}%{r.ops:>9.1f}"
              f"{r.ev * r.ops:>8.2f}  {v}")
    print("  (* = par nuevo)")

    ok = R[R.pasa]
    print("\n" + "=" * 100)
    print("VEREDICTO")
    print("=" * 100)
    if ok.empty:
        print("  Ningún par pasa las dos pruebas.")
    else:
        print(f"  {len(ok)} par(es) pasan corrección múltiple Y estabilidad en dos mitades:")
        for _, r in ok.iterrows():
            print(f"    {r.par:<8} {r.wr:.2f}%  ·  {r.ops:.1f} ops/día  ·  "
                  f"ganancia esperada ${r.ev * r.ops:.2f}/día a $1")
        print(f"\n  Total: {ok.ops.sum():.1f} operaciones/día · "
              f"${(ok.ev * ok.ops).sum():.2f}/día esperados a $1 por operación")


if __name__ == "__main__":
    main()

