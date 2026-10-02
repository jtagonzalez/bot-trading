import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Simula el indicador "INDICADOR GOLD 4.0" (script de IQ Option) sobre el histórico.

Traducción fiel del script, con sus valores por defecto:

  Sistema A — flechas "Up" / "Down"
    mad   = cierre - SMA(cierre, 12)
    Up    : mad >= 0 y mad sube,  SMA3(cierre) > SMA50(cierre),
            SMA3(apertura) < SMA50(apertura)   <- es el instante del cruce
            y cierre >= EMA100
    Down  : el espejo

  Sistema B — "COMPRAR" / "VENDER"
    buffer1 = precio - SMA(precio, 34)        (SMA de 1 = el propio precio)
    buffer2 = WMA(buffer1, 5)
    COMPRAR cuando buffer1 cruza buffer2 hacia arriba; VENDER hacia abajo
    precio = HL/2 (entrada 5 por defecto); se prueba también con el cierre

Cada señal se evalúa como opción binaria: entrada al cierre de la vela de la
señal, vencimiento N velas después. Los empates se excluyen (el broker devuelve
lo apostado). Solo forex real, 01-17h UTC de lunes a viernes: el horario en
que la cuenta puede operar esos pares.

Breakeven según el tipo de opción que exige cada vencimiento:
  hasta 5 min  -> turbo, paga 86%  -> 53,76%
  15 min o más -> binary, paga 90% -> 52,63%

    py simular_gold4.py
"""
import math
import sqlite3
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

PARES = ["AUDUSD", "EURGBP", "EURJPY", "EURUSD", "GBPJPY",
         "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def wma(s, n):
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def senales(v, precio_b="hl2"):
    c, o = v.close, v.open
    mad = c - c.rolling(12).mean()
    smaa, smab = c.rolling(3).mean(), c.rolling(50).mean()
    smaao, smabo = o.rolling(3).mean(), o.rolling(50).mean()
    emaa = c.ewm(span=100, adjust=False).mean()
    up_a = (mad >= 0) & (mad > mad.shift(1)) & (smaa > smab) & (c >= emaa) & (smaao < smabo)
    dn_a = (mad <= 0) & (mad.shift(1) > mad) & (smaa < smab) & (smaao > smabo) & (c <= emaa)

    p = (v.high + v.low) / 2 if precio_b == "hl2" else c
    b1 = p - p.rolling(34).mean()
    b2 = wma(b1, 5)
    up_b = (b1 > b2) & (b1.shift(1) < b2.shift(1))
    dn_b = (b1 < b2) & (b1.shift(1) > b2.shift(1))
    return up_a, dn_a, up_b, dn_b


def cargar(c, par, tf):
    v = pd.read_sql("SELECT ts, open, high, low, close FROM velas WHERE activo=? "
                    "AND timeframe=? ORDER BY ts", c, params=(par, tf))
    v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
    return v


def evaluar(c, tf, vencimientos, precio_b="hl2"):
    filas = []
    for par in PARES:
        v = cargar(c, par, tf)
        if len(v) < 500:
            continue
        up_a, dn_a, up_b, dn_b = senales(v, precio_b)
        operable = v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)
        for N in vencimientos:
            futuro = v.close.shift(-N)
            # el vencimiento tiene que caer N velas despues, sin huecos
            contiguo = (v.ts.shift(-N) - v.ts) == N * tf
            for sistema, up, dn in (("A flechas", up_a, dn_a), ("B cruce", up_b, dn_b)):
                for direccion, m in (("call", up), ("put", dn)):
                    m = m & operable & contiguo & futuro.notna()
                    d = futuro[m] - v.close[m]
                    d = d[d != 0]
                    gano = (d > 0) if direccion == "call" else (d < 0)
                    filas.append(pd.DataFrame({
                        "par": par, "tf": tf, "N": N, "sistema": sistema,
                        "dir": direccion, "dt": v.dt[d.index], "gano": gano.astype(int)}))
    return pd.concat(filas, ignore_index=True)


def main():
    c = sqlite3.connect("velas.db")
    config = [(60, [1, 2, 3, 5]), (300, [1, 3]), (900, [1, 2])]
    todo = pd.concat([evaluar(c, tf, Ns) for tf, Ns in config], ignore_index=True)

    print("=" * 100)
    print("INDICADOR GOLD 4.0 — todos los pares, horario operable, empates excluidos")
    print("=" * 100)
    print(f"  {'vela':<5}{'venc.':>7}{'sistema':>12}{'n':>9}{'WR':>8}{'IC95':>16}"
          f"{'BE':>8}{'señales/día':>13}{'$/día a $1':>12}")
    for (tf, N, sis), g in todo.groupby(["tf", "N", "sistema"]):
        minutos = tf // 60 * N
        payout = 0.86 if minutos <= 5 else 0.90
        be = 100 / (1 + payout)
        dias = g.dt.dt.normalize().nunique()
        n, k = len(g), int(g.gano.sum())
        lo, hi = wilson(k, n)
        ev = (k / n) * payout - (1 - k / n)
        print(f"  {tf // 60:>2}m  {minutos:>4}min{sis:>12}{n:>9,}{100 * k / n:>7.2f}%"
              f"  [{lo:5.2f},{hi:5.2f}]{be:>7.2f}%{n / dias:>13.1f}{ev * n / dias:>12.2f}")

    print("\n" + "=" * 100)
    print("¿ALGÚN PAR SUELTO SUPERA SU BREAKEVEN?  (mejor combinación por par, con IC95)")
    print("=" * 100)
    mejores = []
    for (par, tf, N, sis), g in todo.groupby(["par", "tf", "N", "sistema"]):
        minutos = tf // 60 * N
        be = 100 / (1 + (0.86 if minutos <= 5 else 0.90))
        n, k = len(g), int(g.gano.sum())
        if n < 200:
            continue
        lo, hi = wilson(k, n)
        mejores.append((100 * k / n, lo, par, tf // 60, minutos, sis, n, be))
    mejores.sort(reverse=True)
    for wr, lo, par, tfm, minutos, sis, n, be in mejores[:10]:
        v = "SUPERA" if lo > be else "no concluyente" if wr > be else "no"
        print(f"  {par:<8}{tfm:>3}m {minutos:>3}min {sis:<10} n={n:>6,}  {wr:6.2f}%  "
              f"IC_inf {lo:5.2f}%  BE {be:.2f}%  {v}")
    print(f"\n  ({len(mejores)} combinaciones par × vela × vencimiento × sistema; "
          f"la mejor de tantas sale alta por azar)")

    print("\n" + "=" * 100)
    print("SENSIBILIDAD: sistema B con el CIERRE en vez de HL/2 (1m, venc. 1 min)")
    print("=" * 100)
    alt = evaluar(c, 60, [1], "close")
    g = alt[alt.sistema == "B cruce"]
    n, k = len(g), int(g.gano.sum())
    lo, hi = wilson(k, n)
    print(f"  n={n:,}  {100 * k / n:.2f}%  [{lo:.2f},{hi:.2f}]")


if __name__ == "__main__":
    main()
