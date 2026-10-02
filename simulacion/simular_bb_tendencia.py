import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Setup Gold 4.0 + Bollinger con la tendencia bien definida (corrección del usuario):

  * para VENDER la vela tiene que estar POR DEBAJO de la línea cian, no solo
    cerrar apenas debajo (y para COMPRAR, por encima)
  * nada de mercado lateral: la cian tiene que ir en la dirección de la operación

Niveles de exigencia (VENTA; COMPRA es el espejo):
  T0 original       cierre bajo la cian (lo que se probó antes)
  T1 vela entera    toda la vela bajo la cian (máximo < cian)
  T2 + cian baja    T1 y la cian más baja que hace 10 velas
  T3 + venía abajo  T2 y las 10 velas anteriores cerraron bajo la cian
  T4 + banda abajo  T3 y la banda superior de Bollinger también bajo la cian
                    (tendencia tan clara que el canal entero va por debajo)

En todos: vela roja y mecha que toca la banda superior; "con Gold" añade el
aviso VENDER del Gold 4.0 en esa vela.

Velas de 1 min; operación a 1 min, pago 86%. Se mide el acierto por operación
(empates fuera) y la martingala continua de 6 operaciones (x2,16).

    py simular_bb_tendencia.py
"""
import math
import sqlite3

import numpy as np
import pandas as pd

from simular_bb_martingala import CRIPTO, FOREX, ciclos_de
from simular_gold4 import senales

PAYOUT = 0.86
BE = 100 / (1 + PAYOUT)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def variantes(v):
    _, _, up_b, dn_b = senales(v)
    c = v.close
    cian = c.rolling(50).mean()
    mid = c.rolling(14).mean()
    sd = c.rolling(14).std(ddof=0)
    sup, inf = mid + 2 * sd, mid - 2 * sd
    venta_bb = (c < v.open) & (v.high >= sup)
    compra_bb = (c > v.open) & (v.low <= inf)
    bajo10 = (c < cian).astype(int).rolling(10).sum().shift(1) == 10
    sobre10 = (c > cian).astype(int).rolling(10).sum().shift(1) == 10
    t = {}
    t["T0 original"] = (c > cian, c < cian)
    t["T1 vela entera"] = (v.low > cian, v.high < cian)
    t["T2 + cian en dirección"] = (t["T1 vela entera"][0] & (cian > cian.shift(10)),
                                   t["T1 vela entera"][1] & (cian < cian.shift(10)))
    t["T3 + 10 velas previas"] = (t["T2 + cian en dirección"][0] & sobre10,
                                  t["T2 + cian en dirección"][1] & bajo10)
    t["T4 + banda del lado"] = (t["T3 + 10 velas previas"][0] & (inf > cian),
                                t["T3 + 10 velas previas"][1] & (sup < cian))
    out = {}
    for nombre, (tc, tv) in t.items():
        out[(nombre, "con Gold")] = (compra_bb & tc & up_b, venta_bb & tv & dn_b)
        out[(nombre, "sin Gold")] = (compra_bb & tc, venta_bb & tv)
    return out


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
            datos.append((v, variantes(v), op))
        dias = len({d for v, _, op in datos for d in v.dt.dt.normalize()[op].unique()})
        print("=" * 112)
        print(f"{grupo} · {dias} días · velas y operaciones de 1 min · breakeven {BE:.2f}%")
        print("=" * 112)
        print(f"  {'tendencia':<24}{'Gold':<10}{'señales/día':>12}{'acierto 1ª op':>15}{'IC95':>16}"
              f"   {'martingala 6 ops:':<18}{'perdidos':>9}{'$/día':>9}")
        for clave in datos[0][1]:
            g = p = 0
            cs = []
            for v, var, op in datos:
                compra, venta = var[clave]
                ts, cl = v.ts.values, v.close.values
                ok = np.r_[ts[1:] - ts[:-1] == 60, False] & op
                r = np.sign(np.r_[cl[1:] - cl[:-1], 0])
                for m, sg in ((compra.values & ok, 1), (venta.values & ok, -1)):
                    x = r[m] * sg
                    g += int((x > 0).sum()); p += int((x < 0).sum())
                cs += ciclos_de(v, compra, venta, op, 6)
            n = g + p
            if n < 20:
                print(f"  {clave[0]:<24}{clave[1]:<10}   solo {n} señales")
                continue
            lo, hi = wilson(g, n)
            netos = np.array([x[1] for x in cs])
            perd = sum(1 for x in cs if not x[3])
            marca = "  <-- supera BE" if lo > BE else ""
            print(f"  {clave[0]:<24}{clave[1]:<10}{n / dias:>12.1f}{100 * g / n:>14.2f}%"
                  f"  [{lo:5.2f},{hi:5.2f}]   {len(cs):>8} ciclos{perd:>9}{netos.sum() / dias:>+9.2f}{marca}")
        print()


if __name__ == "__main__":
    main()
