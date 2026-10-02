import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Versión corregida del setup Gold 4.0 + Bollinger, con las dos correcciones
del usuario sobre el caso GBPUSD 2026-07-28 02:45 (venta con la cian ya
subiendo — mercado lateral / contra la tendencia):

  1. INCLINACIÓN MÍNIMA de la cian, medida en 20 velas (no solo "distinta",
     sino que tenga que moverse un mínimo, en unidades de ATR(14), para
     descartar mercado lateral).
  2. La inclinación debe ir EN LA DIRECCIÓN de la operación: para vender la
     cian tiene que estar BAJANDO en esas 20 velas; para comprar, SUBIENDO.
     (antes "T2" solo pedía que la cian fuera distinta a 10 velas atrás, sin
     comprobar bien el signo en todos los casos según de dónde viniera la señal)

Se mantiene todo lo anterior: vela entera al lado correcto de la cian, vela
roja/verde que toca la banda de Bollinger, vela anterior del mismo color
("vela previa mismo color"), y opcional aviso del Gold 4.0.

Se prueban 5 niveles de exigencia de la inclinación (en múltiplos del ATR(14)
movido en 20 velas): 0.0 (solo dirección correcta, sin mínimo), 0.5, 1.0, 1.5, 2.0.

Velas y operación de 1 min, martingala continua de 6 pasos, pago 86%.

    py simular_bb_v2.py
"""
import sqlite3

import numpy as np
import pandas as pd

from simular_bb_martingala import CRIPTO, FOREX, ciclos_de
from simular_bb_tendencia import BE, PAYOUT, wilson
from simular_gold4 import senales

VENTANA = 20
NIVELES = [0.0, 0.5, 1.0, 1.5, 2.0]


def preparar(v):
    _, _, up_b, dn_b = senales(v)
    c, o, h, l = v.close, v.open, v.high, v.low
    cian = c.rolling(50).mean()
    mid = c.rolling(14).mean()
    sd = c.rolling(14).std(ddof=0)
    sup, inf = mid + 2 * sd, mid - 2 * sd
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    pend = (cian - cian.shift(VENTANA)) / atr          # >0 cian sube, <0 cian baja

    verde, roja = c > o, c < o
    vela_entera_arriba, vela_entera_abajo = l > cian, h < cian
    color_prev_ok_compra = verde & verde.shift(1, fill_value=False)
    color_prev_ok_venta = roja & roja.shift(1, fill_value=False)
    toca_inf, toca_sup = l <= inf, h >= sup

    base_compra = verde & toca_inf & vela_entera_arriba & color_prev_ok_compra
    base_venta = roja & toca_sup & vela_entera_abajo & color_prev_ok_venta

    out = {}
    for u in NIVELES:
        compra = base_compra & (pend > u)
        venta = base_venta & (pend < -u)
        out[(f"inclinación > {u} ATR/20v", "con Gold")] = (compra & up_b, venta & dn_b)
        out[(f"inclinación > {u} ATR/20v", "sin Gold")] = (compra, venta)
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
            datos.append((v, preparar(v), op))
        dias = len({d for v, _, op in datos for d in v.dt.dt.normalize()[op].unique()})
        print("=" * 118)
        print(f"{grupo} · {dias} días · cian en dirección + inclinación mínima en {VENTANA} velas "
              f"+ vela previa mismo color · BE {BE:.2f}%")
        print("=" * 118)
        print(f"  {'exigencia de la inclinación':<26}{'Gold':<10}{'señales/día':>12}"
              f"{'acierto 1ª op':>15}{'IC95':>16}   {'martingala 6 ops:':<18}{'perdidos':>9}{'$/día':>9}")
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
                print(f"  {clave[0]:<26}{clave[1]:<10}   solo {n} señales")
                continue
            lo, hi = wilson(g, n)
            netos = np.array([x[1] for x in cs])
            perd = sum(1 for x in cs if not x[3])
            marca = "  <-- supera BE" if lo > BE else ""
            print(f"  {clave[0]:<26}{clave[1]:<10}{n / dias:>12.1f}{100 * g / n:>14.2f}%"
                  f"  [{lo:5.2f},{hi:5.2f}]   {len(cs):>8} ciclos{perd:>9}{netos.sum() / dias:>+9.2f}{marca}")
        print()


if __name__ == "__main__":
    main()
