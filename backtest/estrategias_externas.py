import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Estrategias encontradas buscando en internet ("mejores estrategias 1 minuto
binarias/forex 2026"), traducidas a reglas verificables. Fuentes: ver
ESTRATEGIA_GOLD4_BOLLINGER.md sección 11.

  - EMA triple (11/21/34): tendencia, precio sobre/bajo las 3 medias alineadas
  - RSI(4) rebote: RSI rápido, vuelve desde sobrecompra/sobreventa (>80/<20)
  - RSI(14) clásico 30/70
  - Estocástico (12,3,20) cruzando 20/80
  - MACD + Estocástico combinados (regla citada: MACD a favor + estocástico
    saliendo de la zona extrema)
  - Bollinger Bounce + RSI (la que un artículo de marketing dice que da 70%
    de acierto — con solo 50 operaciones, hay que verificarlo en serio)
  - ZigZag + DeMarker (toque de banda + oscilador DeMarker extremo)
  - Soporte/Resistencia: rebote en el máximo/mínimo de las 20 velas previas
  - Cruce de medias simple SMA(5)/SMA(20)

Todas devuelven (compra, venta) como Series bool, mismo formato que
estrategia_gold4_bb.py, para conectarlas directo a busqueda_exhaustiva.py.
"""
import numpy as np
import pandas as pd


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    p = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / p.replace(0, np.nan))


def estocastico(h, l, c, n=14, d=3):
    k = 100 * (c - l.rolling(n).min()) / (h.rolling(n).max() - l.rolling(n).min()).replace(0, np.nan)
    return k, k.rolling(d).mean()


def macd(c, fast=12, slow=26, señal=9):
    linea = ema(c, fast) - ema(c, slow)
    return linea, ema(linea, señal)


def demarker(h, l, n=14):
    demax = (h - h.shift(1)).clip(lower=0)
    demin = (l.shift(1) - l).clip(lower=0)
    ma_max, ma_min = demax.rolling(n).mean(), demin.rolling(n).mean()
    return ma_max / (ma_max + ma_min).replace(0, np.nan)


def ema_triple(v, r1=11, r2=21, r3=34):
    e1, e2, e3 = ema(v.close, r1), ema(v.close, r2), ema(v.close, r3)
    alineadas_alza = (e1 > e2) & (e2 > e3)
    alineadas_baja = (e1 < e2) & (e2 < e3)
    cruce_alza = (e1.shift(1) <= e2.shift(1)) & (e1 > e2)
    cruce_baja = (e1.shift(1) >= e2.shift(1)) & (e1 < e2)
    compra = alineadas_alza & cruce_alza & (v.close > e1)
    venta = alineadas_baja & cruce_baja & (v.close < e1)
    return compra, venta


def rsi_rebote(v, n=4, sobreventa=20, sobrecompra=80):
    r = rsi(v.close, n)
    compra = (r.shift(1) < sobreventa) & (r >= sobreventa)
    venta = (r.shift(1) > sobrecompra) & (r <= sobrecompra)
    return compra, venta


def rsi_clasico(v, n=14, sobreventa=30, sobrecompra=70):
    r = rsi(v.close, n)
    compra = (r.shift(1) < sobreventa) & (r >= sobreventa)
    venta = (r.shift(1) > sobrecompra) & (r <= sobrecompra)
    return compra, venta


def estocastico_cruce(v, n=12, d=3, sobreventa=20, sobrecompra=80):
    k, _ = estocastico(v.high, v.low, v.close, n, d)
    compra = (k.shift(1) < sobreventa) & (k >= sobreventa)
    venta = (k.shift(1) > sobrecompra) & (k <= sobrecompra)
    return compra, venta


def macd_estocastico(v):
    linea, _ = macd(v.close)
    k, _ = estocastico(v.high, v.low, v.close, 14, 3)
    compra = (linea > 0) & (k.shift(1) < 20) & (k >= 20)
    venta = (linea < 0) & (k.shift(1) > 80) & (k <= 80)
    return compra, venta


def bollinger_bounce_rsi(v):
    r = rsi(v.close, 14)
    compra = (v.low <= v.inf) & (r < 30)
    venta = (v.high >= v.sup) & (r > 70)
    return compra, venta


def zigzag_demarker(v):
    dm = demarker(v.high, v.low, 14)
    compra = (v.low <= v.inf) & (dm < 0.3)
    venta = (v.high >= v.sup) & (dm > 0.7)
    return compra, venta


def soporte_resistencia(v, n=20):
    soporte = v.low.rolling(n).min().shift(1)
    resistencia = v.high.rolling(n).max().shift(1)
    compra = (v.low <= soporte) & (v.close > v.open)
    venta = (v.high >= resistencia) & (v.close < v.open)
    return compra, venta


def sma_cruce(v, r1=5, r2=20):
    s1, s2 = v.close.rolling(r1).mean(), v.close.rolling(r2).mean()
    compra = (s1.shift(1) <= s2.shift(1)) & (s1 > s2)
    venta = (s1.shift(1) >= s2.shift(1)) & (s1 < s2)
    return compra, venta


ESTRATEGIAS = {
    "EMA triple 11/21/34":       ema_triple,
    "RSI(4) rebote":             rsi_rebote,
    "RSI(14) clasico 30/70":     rsi_clasico,
    "Estocastico(12,3) 20/80":   estocastico_cruce,
    "MACD + Estocastico":        macd_estocastico,
    "Bollinger Bounce + RSI":    bollinger_bounce_rsi,
    "ZigZag + DeMarker":         zigzag_demarker,
    "Soporte/Resistencia 20v":   soporte_resistencia,
    "SMA cruce 5/20":            sma_cruce,
}
