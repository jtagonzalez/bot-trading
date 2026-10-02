import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Gold 4.0 + Bandas de Bollinger (14, 2) — el setup de las capturas del usuario.

VENTA (todo en la misma vela):
  1. aviso VENDER del Gold 4.0 (cruce del sistema B)
  2. tendencia bajista: cierre bajo la línea cian (SMA 50 del script)
  3. vela roja (cierre < apertura)
  4. la mecha superior toca la banda superior de Bollinger (máximo >= banda)

COMPRA: el espejo (COMPRAR, sobre la cian, vela verde, mecha inferior toca la
banda inferior).

Variantes para aislar qué aporta cada pieza:
  estricto        las 4 condiciones en la misma vela
  aviso <=3 velas el aviso del Gold en esa vela o en alguna de las 2 anteriores
  sin Gold        solo tendencia + color + toque de banda

Forex (10 pares, 01-17h UTC lun-vie) y cripto (BTC, ETH, 24x7).
Pago: turbo 86% hasta 5 min; binaria 87% a 15 min o más.

    py simular_gold4_bb.py
"""
import math
import sqlite3

import numpy as np
import pandas as pd

from simular_gold4 import senales

FOREX = ["AUDUSD", "EURGBP", "EURJPY", "EURUSD", "GBPJPY",
         "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]
CRIPTO = ["BTCUSD", "ETHUSD"]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def evaluar(c, activos, tf, venc, cripto):
    filas = []
    for a in activos:
        v = pd.read_sql("SELECT ts, open, high, low, close FROM velas WHERE activo=? "
                        "AND timeframe=? ORDER BY ts", c, params=(a, tf))
        if len(v) < 500:
            continue
        v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
        _, _, up_b, dn_b = senales(v)
        cian = v.close.rolling(50).mean()
        mid = v.close.rolling(14).mean()
        sd = v.close.rolling(14).std(ddof=0)
        sup, inf = mid + 2 * sd, mid - 2 * sd
        roja, verde = v.close < v.open, v.close > v.open
        venta_bb = (v.close < cian) & roja & (v.high >= sup)
        compra_bb = (v.close > cian) & verde & (v.low <= inf)
        dn3 = dn_b | dn_b.shift(1, fill_value=False) | dn_b.shift(2, fill_value=False)
        up3 = up_b | up_b.shift(1, fill_value=False) | up_b.shift(2, fill_value=False)
        if cripto:
            operable = pd.Series(True, index=v.index)
        else:
            operable = v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)
        for N in venc:
            fut = v.close.shift(-N)
            ok = operable & ((v.ts.shift(-N) - v.ts) == N * tf) & fut.notna()
            for variante, venta, compra in (
                    ("estricto", venta_bb & dn_b, compra_bb & up_b),
                    ("aviso <=3 velas", venta_bb & dn3, compra_bb & up3),
                    ("sin Gold", venta_bb, compra_bb)):
                for sentido, m in (("venta", venta & ok), ("compra", compra & ok)):
                    d = (fut[m] - v.close[m])
                    d = d[d != 0]
                    gano = (d < 0) if sentido == "venta" else (d > 0)
                    filas.append(pd.DataFrame({"activo": a, "tf": tf, "N": N,
                                               "variante": variante, "sentido": sentido,
                                               "dt": v.dt[d.index], "gano": gano.astype(int)}))
    return pd.concat(filas, ignore_index=True) if filas else pd.DataFrame()


def main():
    c = sqlite3.connect("velas.db")
    for grupo, activos, cripto in (("FOREX (10 pares)", FOREX, False),
                                   ("CRIPTO (BTC, ETH)", CRIPTO, True)):
        todo = pd.concat([evaluar(c, activos, tf, venc, cripto)
                          for tf, venc in ((60, [1, 3, 5]), (300, [1, 3]), (900, [1, 2]))],
                         ignore_index=True)
        print("=" * 104)
        print(f"GOLD 4.0 + BOLLINGER (14,2) · {grupo}")
        print("=" * 104)
        print(f"  {'vela':<5}{'venc':>7}{'variante':>17}{'n':>8}{'acierto':>9}{'IC95':>16}"
              f"{'BE':>8}{'señales/día':>13}{'$/día a $1':>12}")
        for (tf, N, var), g in todo.groupby(["tf", "N", "variante"], sort=False):
            minutos = tf // 60 * N
            payout = 0.86 if minutos <= 5 else 0.87
            be = 100 / (1 + payout)
            n, k = len(g), int(g.gano.sum())
            if n < 30:
                print(f"  {tf // 60:>2}m  {minutos:>4}min{var:>17}   solo {n} señales")
                continue
            dias = todo[todo.tf == tf].dt.dt.normalize().nunique()
            lo, hi = wilson(k, n)
            ev = (k / n) * payout - (1 - k / n)
            marca = "  <-- supera BE" if lo > be else ""
            print(f"  {tf // 60:>2}m  {minutos:>4}min{var:>17}{n:>8,}{100 * k / n:>8.2f}%"
                  f"  [{lo:5.2f},{hi:5.2f}]{be:>7.2f}%{n / dias:>13.1f}{ev * n / dias:>+12.2f}{marca}")
        # solo ventas, que es lo que muestra la captura
        print("\n  Solo VENTAS (el caso de la captura), variante estricta:")
        for (tf, N), g in todo[(todo.variante == "estricto") &
                               (todo.sentido == "venta")].groupby(["tf", "N"]):
            n, k = len(g), int(g.gano.sum())
            if n >= 30:
                lo, hi = wilson(k, n)
                print(f"    {tf // 60:>2}m venc {tf // 60 * N:>3}min  n={n:>5,}  "
                      f"{100 * k / n:6.2f}%  [{lo:5.2f},{hi:5.2f}]")
        print()


if __name__ == "__main__":
    main()
