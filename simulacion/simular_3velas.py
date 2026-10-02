import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Nueva idea del usuario: operar a favor de la tendencia cuando hay 3 velas
SEGUIDAS del mismo color, y las 3 quedan del lado correcto de la línea media
de Bollinger (naranja en la captura, la media de 14 — la misma "línea
intermedia" de antes):

  COMPRA: 3 velas verdes seguidas, las 3 cierran por ENCIMA de la media.
  VENTA:  3 velas rojas seguidas, las 3 cierran por DEBAJO de la media.

Se opera a favor de esa tendencia (compra tras 3 verdes, venta tras 3 rojas)
justo en el cierre de la 3a vela. Es lo contrario del setup anterior: ahí se
apostaba a que la tendencia se mantenía DESPUÉS de un rebote en la media;
aquí se apuesta a que la tendencia YA en marcha sigue un paso más.

No se exige aviso del Gold 4.0 ni inclinación de la cian — es literalmente
la regla que describió el usuario, para verla tal cual. Se agrega solo como
comparación una variante "+ cian a favor" (la cian también inclinada en esa
dirección) para ver si ayuda o no.

Velas y operación de 1 min, martingala continua de 6 pasos, pago 86%.

    py simular_3velas.py                    # tabla resumen + por par
    py simular_3velas.py --dias-nuevo        # usa solo si ya hay mas de 43 dias en velas.db
"""
import sqlite3

import numpy as np
import pandas as pd
from scipy.stats import binom

from simular_bb_martingala import CRIPTO, FOREX, PAYOUT, ciclos_de
from simular_bb_tendencia import BE, wilson

rng = np.random.default_rng(2026)


def preparar(v):
    c, o = v.close, v.open
    mid = c.rolling(14).mean()
    cian = c.rolling(50).mean()

    verde, roja = c > o, c < o
    tres_verdes = verde & verde.shift(1, fill_value=False) & verde.shift(2, fill_value=False)
    tres_rojas = roja & roja.shift(1, fill_value=False) & roja.shift(2, fill_value=False)
    arriba_3 = (c > mid) & (c.shift(1) > mid.shift(1)) & (c.shift(2) > mid.shift(2))
    abajo_3 = (c < mid) & (c.shift(1) < mid.shift(1)) & (c.shift(2) < mid.shift(2))

    compra = tres_verdes & arriba_3
    venta = tres_rojas & abajo_3
    cian_favor_compra = cian > cian.shift(10)
    cian_favor_venta = cian < cian.shift(10)
    return {
        "literal (sin cian)": (compra, venta),
        "+ cian a favor":     (compra & cian_favor_compra, venta & cian_favor_venta),
    }


def evaluar(v, op, compra, venta):
    ts, cl = v.ts.values, v.close.values
    ok = np.r_[ts[1:] - ts[:-1] == 60, False] & op
    r = np.sign(np.r_[cl[1:] - cl[:-1], 0])
    g = p = 0
    for m, sg in ((compra.values & ok, 1), (venta.values & ok, -1)):
        x = r[m] * sg
        g += int((x > 0).sum()); p += int((x < 0).sum())
    cs = ciclos_de(v, compra, venta, op, 6)
    return g, p, cs


def main():
    c = sqlite3.connect("velas.db")
    for grupo, activos, cripto in (("FOREX (10 pares, 01-17h UTC)", FOREX, False),
                                   ("CRIPTO (BTC, ETH, 24x7)", CRIPTO, True)):
        datos = []
        for a in activos:
            v = pd.read_sql("SELECT ts, open, high, low, close FROM velas WHERE activo=? "
                            "AND timeframe=60 ORDER BY ts", c, params=(a,))
            v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
            op = (np.ones(len(v), bool) if cripto else
                  (v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)).values)
            datos.append((v, preparar(v), op))
        dias = len({d for v, _, op in datos for d in v.dt.dt.normalize()[op].unique()})
        print("=" * 108)
        print(f"{grupo} · {dias} días · 3 velas seguidas del mismo color del lado correcto "
              f"de la media (14) · BE {BE:.2f}%")
        print("=" * 108)
        print(f"  {'variante':<22}{'señales/día':>12}{'acierto 1ª op':>15}{'IC95':>16}"
              f"   {'martingala 6 ops:':<18}{'perdidos':>9}{'$/día':>9}")
        for clave in datos[0][1]:
            g = p = 0
            cs = []
            for v, var, op in datos:
                compra, venta = var[clave]
                gg, pp, cc = evaluar(v, op, compra, venta)
                g += gg; p += pp; cs += cc
            n = g + p
            if n < 20:
                print(f"  {clave:<22}   solo {n} señales")
                continue
            lo, hi = wilson(g, n)
            netos = np.array([x[1] for x in cs])
            perd = sum(1 for x in cs if not x[3])
            marca = "  <-- supera BE" if lo > BE else ""
            print(f"  {clave:<22}{n / dias:>12.1f}{100 * g / n:>14.2f}%"
                  f"  [{lo:5.2f},{hi:5.2f}]   {len(cs):>8} ciclos{perd:>9}{netos.sum() / dias:>+9.2f}{marca}")
        print()

    # detalle por par, forex, variante literal
    print("=" * 108)
    print("DETALLE POR PAR · forex · variante LITERAL (sin cian) · 43+ días")
    print("=" * 108)
    print(f"{'par':<9}{'señales':>8}{'ganadas':>9}{'perdidas':>9}{'%acierto':>10}"
          f"{'$/día sin mart.':>17}   {'ciclos':>7}{'gana ciclo':>11}{'pierde ciclo':>13}{'$/día mart.':>12}")
    tot = dict(g=0, p=0, cg=0, cp=0, neto=0.0, flat=0.0)
    dias_ref = None
    todos_ciclos = []
    for a in FOREX:
        v = pd.read_sql("SELECT ts,open,high,low,close FROM velas WHERE activo=? "
                        "AND timeframe=60 ORDER BY ts", c, params=(a,))
        v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
        op = (v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)).values
        if dias_ref is None:
            dias_ref = len(v.dt.dt.normalize()[op].unique())
        compra, venta = preparar(v)["literal (sin cian)"]
        g, p, cs = evaluar(v, op, compra, venta)
        flat = g * PAYOUT - p
        cg = sum(1 for x in cs if x[3]); cp = len(cs) - cg
        neto = sum(x[1] for x in cs)
        n = g + p
        acierto = f"{100 * g / n:.1f}%" if n else "-"
        print(f"{a:<9}{g + p:>8}{g:>9}{p:>9}{acierto:>10}{flat / dias_ref:>+17.2f}   "
              f"{len(cs):>7}{cg:>11}{cp:>13}{neto / dias_ref:>+12.2f}")
        for k, v2 in zip(("g", "p", "cg", "cp"), (g, p, cg, cp)):
            tot[k] += v2
        tot["neto"] += neto; tot["flat"] += flat
        todos_ciclos += cs
    print("-" * 105)
    ac = f"{100 * tot['g'] / (tot['g'] + tot['p']):.1f}%"
    print(f"{'TOTAL':<9}{tot['g'] + tot['p']:>8}{tot['g']:>9}{tot['p']:>9}{ac:>10}"
          f"{tot['flat'] / dias_ref:>+17.2f}   {tot['cg'] + tot['cp']:>7}{tot['cg']:>11}{tot['cp']:>13}"
          f"{tot['neto'] / dias_ref:>+12.2f}")

    # probabilidad de 10 operaciones al dia (igual que antes)
    acierto = tot['g'] / (tot['g'] + tot['p'])
    evs = [(k, binom.pmf(k, 10, acierto), k * PAYOUT - (10 - k)) for k in range(11)]
    p_neg = sum(pr for k, pr, neto in evs if neto < 0)
    ev = sum(pr * neto for k, pr, neto in evs)
    print(f"\nSIN MARTINGALA, 10 operaciones/día (10 pares combinados, acierto {100*acierto:.1f}%):")
    print(f"  prob. día PERDEDOR: {100*p_neg:.1f}%   esperado ${ev:+.2f}")
    netos_ciclo = np.array([x[1] for x in todos_ciclos])
    if len(netos_ciclo) >= 15:
        N = 100000
        muestras = rng.choice(netos_ciclo, size=(N, 10), replace=True).sum(axis=1)
        p_negm = (muestras < -0.005).mean()
        print(f"\nCON MARTINGALA, bootstrap 10 ciclos/día ({len(todos_ciclos)} ciclos históricos, "
              f"{N:,} repeticiones):")
        print(f"  prob. día PERDEDOR: {100*p_negm:.1f}%   resultado medio ${muestras.mean():+.2f}   "
              f"mediana ${np.median(muestras):+.2f}")
        for pct in (10, 5, 1):
            print(f"  el {pct}% peor de los días: por debajo de ${np.percentile(muestras, pct):+.2f}")


if __name__ == "__main__":
    main()
