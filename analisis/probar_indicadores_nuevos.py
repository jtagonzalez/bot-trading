import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
¿Mejora la estrategia si se añade Fibonacci o EMA200 como filtro?

La EMA ya está dentro del modelo (distancia a EMA9, EMA21 y SMA50). Lo que no
tiene es la EMA200 como filtro de tendencia ni Fibonacci, así que se prueban
esos, con reglas ESTÁNDAR y umbrales clásicos, sin ajustarlos:

  fib zona dorada   CALL en retroceso 38,2-61,8% de un tramo ALCISTA,
                    PUT en retroceso 38,2-61,8% de un tramo BAJISTA
  fib extremos      CALL bajo el 23,6% del rango, PUT sobre el 76,4%
  EMA200 a favor    CALL sobre la EMA200, PUT bajo ella
  EMA200 en contra  lo contrario
  cruce EMA 9/21    a favor / en contra

Cada filtro se aplica ENCIMA de la señal del modelo, sobre:
  1. histórico: 376.145 predicciones fuera de muestra, 2,5 años, 10 pares
  2. en vivo: las 1.484 señales con desenlace virtual (solo lo calculable con
     la ventana de 20 velas que se guarda: sin EMA200)

La pregunta de cada filtro no es solo "¿sube el acierto?", sino "¿sube la
ganancia diaria?". Un filtro que sube el acierto quitando operaciones puede
dejar la ganancia igual o peor.

    py probar_indicadores_nuevos.py
"""
import json
import math
import sqlite3
import warnings

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from scipy import stats

warnings.filterwarnings("ignore")

GOLDEN = (0.382, 0.618)


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
    o = np.argsort(p)
    m = len(p)
    q = np.empty(m)
    prev = 1.0
    for r in range(m, 0, -1):
        i = o[r - 1]
        prev = min(prev, p[i] * m / r)
        q[i] = prev
    return q


def fib(high, low, close, n):
    """Posición en el rango de n velas y si el tramo es alcista.
    Alcista = el máximo del rango llegó DESPUÉS que el mínimo."""
    H = sliding_window_view(high, n)
    L = sliding_window_view(low, n)
    hi, lo = H.max(1), L.min(1)
    alcista = H.argmax(1) > L.argmin(1)
    pad = np.full(n - 1, np.nan)
    hi = np.concatenate([pad, hi])
    lo = np.concatenate([pad, lo])
    alc = np.concatenate([np.zeros(n - 1, bool), alcista])
    rango = hi - lo
    pos = np.where(rango > 0, (close - lo) / rango, np.nan)
    # retroceso medido desde el extremo del tramo
    retro = np.where(alc, 1 - pos, pos)
    return pos, retro, alc


def reglas(call, pos20, retro20, alc20, pos50, retro50, alc50,
           sobre200=None, ema9_21=None):
    R = {}
    for n, pos, retro, alc in ((20, pos20, retro20, alc20), (50, pos50, retro50, alc50)):
        en_zona = (retro >= GOLDEN[0]) & (retro <= GOLDEN[1])
        R[f"fib{n} zona dorada"] = en_zona & np.where(call, alc, ~alc)
        R[f"fib{n} extremos"] = np.where(call, pos < 0.236, pos > 0.764)
    if sobre200 is not None:
        R["EMA200 a favor"] = np.where(call, sobre200, ~sobre200)
        R["EMA200 en contra"] = np.where(call, ~sobre200, sobre200)
    if ema9_21 is not None:
        R["cruce 9/21 a favor"] = np.where(call, ema9_21, ~ema9_21)
        R["cruce 9/21 en contra"] = np.where(call, ~ema9_21, ema9_21)
    return R


def historico():
    D = pd.read_pickle("oos_15m_con_features.pkl")
    c = sqlite3.connect("velas.db")
    extras = []
    for a in D.activo.unique():
        v = pd.read_sql("SELECT ts, high, low, close FROM velas WHERE activo=? "
                        "AND timeframe=900 ORDER BY ts", c, params=(a,))
        h, l, cl = v.high.values, v.low.values, v.close.values
        e = pd.DataFrame({"activo": a, "ts": v.ts})
        e["pos20"], e["retro20"], e["alc20"] = fib(h, l, cl, 20)
        e["pos50"], e["retro50"], e["alc50"] = fib(h, l, cl, 50)
        ema200 = v.close.ewm(span=200, adjust=False).mean()
        e["sobre200"] = (v.close > ema200).values
        e["ema9_21"] = (v.close.ewm(span=9, adjust=False).mean() >
                        v.close.ewm(span=21, adjust=False).mean()).values
        extras.append(e)
    D = D.merge(pd.concat(extras), on=["activo", "ts"], how="left")
    D["conf"] = (D.p - 0.5).abs()
    D["call"] = D.p > 0.5
    D["gano"] = np.where(D.call, D.sube, 1 - D.sube)
    return D


def evaluar(T, R, payout, dias, titulo):
    base_n, base_k = len(T), int(T.gano.sum())
    base_ev = (base_k / base_n) * payout - (1 - base_k / base_n)
    base_dia = base_ev * base_n / dias
    print("=" * 98)
    print(titulo)
    print("=" * 98)
    print(f"  {'filtro':<22}{'n':>7}{'WR':>8}{'IC95':>16}{'ops/día':>9}{'$/día':>8}"
          f"{'vs sin filtro':>15}{'q(BH)':>8}")
    lo, hi = wilson(base_k, base_n)
    print(f"  {'sin filtro':<22}{base_n:>7,}{100 * base_k / base_n:>7.2f}%"
          f"  [{lo:5.2f},{hi:5.2f}]{base_n / dias:>9.1f}{base_dia:>8.2f}")
    filas = []
    for nombre, m in R.items():
        m = np.asarray(m, bool)
        s, fuera = T[m], T[~m]
        if len(s) < 100 or len(fuera) < 100:
            continue
        k = int(s.gano.sum())
        # ¿las que el filtro deja aciertan más que las que quita?
        tabla = [[k, len(s) - k], [int(fuera.gano.sum()), len(fuera) - int(fuera.gano.sum())]]
        _, pv = stats.fisher_exact(tabla, alternative="greater")
        ev = (k / len(s)) * payout - (1 - k / len(s))
        filas.append((nombre, len(s), 100 * k / len(s), wilson(k, len(s)),
                      len(s) / dias, ev * len(s) / dias, pv))
    q = bh([f[6] for f in filas])
    for (nombre, n, wr, (lo, hi), od, dia, pv), qq in zip(filas, q):
        dif = dia - base_dia
        marca = "  <-- mejora real" if qq < 0.05 and dif > 0 else ""
        print(f"  {nombre:<22}{n:>7,}{wr:>7.2f}%  [{lo:5.2f},{hi:5.2f}]{od:>9.1f}"
              f"{dia:>8.2f}{dif:>+14.2f}${qq:>8.3f}{marca}")
    print()


def en_vivo():
    c = sqlite3.connect("file:live.db?mode=ro", uri=True)
    d = pd.read_sql("""SELECT direccion, ventana_ohlc, resultado_virtual, operada
                       FROM predicciones WHERE run_id IN (6,7)
                       AND resultado_virtual IN ('win','loss')""", c)
    pos20, retro20, alc20, e921 = [], [], [], []
    for v in d.ventana_ohlc:
        w = np.array(json.loads(v), dtype=float)
        h, l, cl = w[:, 1], w[:, 2], w[:, 3]
        p, r, a = fib(h, l, cl, 20)
        pos20.append(p[-1]); retro20.append(r[-1]); alc20.append(a[-1])
        s = pd.Series(cl)
        e921.append(s.ewm(span=9, adjust=False).mean().iloc[-1] >
                    s.ewm(span=21, adjust=False).mean().iloc[-1])
    call = (d.direccion == "call").values
    pos20, retro20 = np.array(pos20), np.array(retro20)
    alc20, e921 = np.array(alc20), np.array(e921)
    R = {k: v for k, v in reglas(call, pos20, retro20, alc20,
                                 pos20, retro20, alc20, None, e921).items()
         if not k.startswith("fib50")}
    gano = (d.resultado_virtual == "win").values
    print("=" * 98)
    print(f"EN VIVO: {len(d):,} señales (1 semana, mercado bajista) · solo lo calculable "
          f"con 20 velas")
    print("=" * 98)
    k0 = gano.sum()
    lo, hi = wilson(k0, len(d))
    print(f"  {'sin filtro':<22}{len(d):>7,}{100 * k0 / len(d):>7.2f}%  [{lo:5.2f},{hi:5.2f}]")
    for nombre, m in R.items():
        m = np.asarray(m, bool)
        n, k = int(m.sum()), int(gano[m].sum())
        if n < 30:
            continue
        lo, hi = wilson(k, n)
        print(f"  {nombre:<22}{n:>7,}{100 * k / n:>7.2f}%  [{lo:5.2f},{hi:5.2f}]")
    op = d.operada.values == 1
    print(f"\n  de las {op.sum()} operadas de verdad: demasiado pocas para cualquier filtro "
          f"(ganaron {gano[op].sum()} de {op.sum()})")
    print()


def main():
    D = historico()
    dias = D.dt.dt.normalize().nunique()
    u = D.conf.quantile(0.9)
    for titulo, T, payout in (
            ("HISTÓRICO · EURUSD, 10% más confiable (lo que opera hoy)",
             D[(D.activo == "EURUSD") & (D.conf >= u)], 0.90),
            ("HISTÓRICO · 10 pares, 10% más confiable",
             D[D.conf >= u], 0.87),
            ("HISTÓRICO · 10 pares, TODAS las señales (para ver si algo permite operar más)",
             D, 0.87)):
        T = T.reset_index(drop=True)
        R = reglas(T.call.values, T.pos20.values, T.retro20.values, T.alc20.values,
                   T.pos50.values, T.retro50.values, T.alc50.values,
                   T.sobre200.values, T.ema9_21.values)
        evaluar(T, R, payout, dias, titulo)
    en_vivo()


if __name__ == "__main__":
    main()
