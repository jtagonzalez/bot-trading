import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Igual que simular_bb_tendencia.py, con una condición extra que pidió el usuario:

  la vela ANTERIOR a la vela que toca la banda de Bollinger debe ser del MISMO
  color que ella.

Es decir: para COMPRAR, la vela de la señal es verde Y la vela anterior
también es verde (dos velas verdes seguidas antes de tocar la banda inferior).
Para VENDER, la vela de la señal es roja Y la anterior también es roja.

Motivo: en los ciclos que se perdieron, la vela de la señal solía venir
precedida de una vela del color contrario (p.ej. verde que rompe hacia arriba
justo después de una roja) — un cambio de color en la última vela, no un
empuje sostenido. Esto exige que YA hubiera dos velas seguidas en la misma
dirección antes de la señal.

Se aplica sobre las mismas 5 definiciones de tendencia (T0-T4) y con/sin el
aviso del Gold 4.0, todo lo demás exactamente igual: velas y operación de
1 min, martingala continua de 6 pasos, pago 86%.

    py simular_bb_color_previo.py
"""
import sqlite3

import numpy as np
import pandas as pd

from simular_bb_martingala import CRIPTO, FOREX, ciclos_de
from simular_bb_tendencia import BE, PAYOUT, variantes, wilson


def con_color_previo(v, variantes_dict):
    verde, roja = v.close > v.open, v.close < v.open
    compra_ok = verde & verde.shift(1, fill_value=False)
    venta_ok = roja & roja.shift(1, fill_value=False)
    out = {}
    for clave, (compra, venta) in variantes_dict.items():
        out[clave] = (compra & compra_ok, venta & venta_ok)
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
            datos.append((v, con_color_previo(v, variantes(v)), op))
        dias = len({d for v, _, op in datos for d in v.dt.dt.normalize()[op].unique()})
        print("=" * 116)
        print(f"{grupo} · {dias} días · vela previa MISMO COLOR que la vela señal · BE {BE:.2f}%")
        print("=" * 116)
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
