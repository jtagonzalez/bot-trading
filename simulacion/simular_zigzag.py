import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Zigzag entre las Bandas de Bollinger, CORREGIDO: además del zigzag
confirmado (toques alternados de extremo a extremo), ahora se exige que la
cian esté prácticamente PLANA (inclinación en 20 velas, unidades de ATR14,
cerca de 0) — esto es lo que faltaba en la primera versión, que confundía
un canal con tendencia con mercado lateral real (ver AUDUSD 14/09 06:46 en
ESTRATEGIA_GOLD4_BOLLINGER.md sección 3.4).

Se prueban varios umbrales de "qué tan plana" (0.1, 0.2, 0.3, 0.5 ATR/20v)
con mínimo 2 rebotes, y con y sin martingala.

    py simular_zigzag.py
"""
import sqlite3

import numpy as np
import pandas as pd

from estrategia_gold4_bb import (BE, CRIPTO, FOREX, PAYOUT, cargar_velas,
                                 ciclos_de, indicadores, senal_zigzag)
from simular_bb_tendencia import wilson

UMBRALES = [None, 0.5, 0.3, 0.2, 0.1]
MIN_REBOTES = 2


def etiqueta(u):
    return "sin filtro (como antes)" if u is None else f"|inclinación| <= {u} ATR/20v"


def evaluar(v, compra, venta):
    ts, cl = v.ts.values, v.close.values
    op = v.operable.values
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
        datos = [indicadores(cargar_velas(c, a, operable_forex=not cripto)) for a in activos]
        dias = len({d for v in datos for d in v.dt.dt.normalize()[v.operable].unique()})
        print("=" * 112)
        print(f"{grupo} · {dias} días · ZIGZAG (mín {MIN_REBOTES} rebotes) + cian PLANA · BE {BE:.2f}%")
        print("=" * 112)
        print(f"  {'cian plana':<26}{'señales/día':>12}{'acierto 1ª op':>15}{'IC95':>16}"
              f"   {'sin mart. $/día':>16}   {'martingala:':<15}{'perdidos':>9}{'$/día':>9}")
        for u in UMBRALES:
            g = p = 0
            cs = []
            for v in datos:
                compra, venta = senal_zigzag(v, MIN_REBOTES, u)
                gg, pp, cc = evaluar(v, compra, venta)
                g += gg; p += pp; cs += cc
            n = g + p
            if n < 20:
                print(f"  {etiqueta(u):<26}   solo {n} señales")
                continue
            lo, hi = wilson(g, n)
            flat = g * PAYOUT - p
            netos = np.array([x[1] for x in cs])
            perd = sum(1 for x in cs if not x[3])
            marca = "  <-- supera BE" if lo > BE else ""
            print(f"  {etiqueta(u):<26}{n / dias:>12.1f}{100 * g / n:>14.2f}%"
                  f"  [{lo:5.2f},{hi:5.2f}]   {flat / dias:>+16.2f}   "
                  f"{len(cs):>6} ciclos{perd:>9}{netos.sum() / dias:>+9.2f}{marca}")
        print()

    # detalle por par, forex, el umbral mas exigente con muestra decente (0.3)
    umbral_detalle = 0.3
    print("=" * 112)
    print(f"DETALLE POR PAR · forex · mínimo {MIN_REBOTES} rebotes · cian plana <= {umbral_detalle} ATR/20v")
    print("=" * 112)
    print(f"{'par':<9}{'señales':>8}{'ganadas':>9}{'perdidas':>9}{'%acierto':>10}"
          f"{'$/día sin mart.':>17}   {'ciclos':>7}{'gana ciclo':>11}{'pierde ciclo':>13}{'$/día mart.':>12}")
    tot = dict(g=0, p=0, cg=0, cp=0, neto=0.0, flat=0.0)
    dias_ref = None
    for a in FOREX:
        v = indicadores(cargar_velas(c, a))
        if dias_ref is None:
            dias_ref = len(v.dt.dt.normalize()[v.operable].unique())
        compra, venta = senal_zigzag(v, MIN_REBOTES, umbral_detalle)
        g, p, cs = evaluar(v, compra, venta)
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
    print("-" * 108)
    if tot['g'] + tot['p'] > 0:
        ac = f"{100 * tot['g'] / (tot['g'] + tot['p']):.1f}%"
        print(f"{'TOTAL':<9}{tot['g'] + tot['p']:>8}{tot['g']:>9}{tot['p']:>9}{ac:>10}"
              f"{tot['flat'] / dias_ref:>+17.2f}   {tot['cg'] + tot['cp']:>7}{tot['cg']:>11}{tot['cp']:>13}"
              f"{tot['neto'] / dias_ref:>+12.2f}")


if __name__ == "__main__":
    main()
