"""
9 ideas propias (no sacadas de internet), pensadas para 1 minuto:

  A) Envolvente (engulfing): vela que envuelve el cuerpo de la anterior -> reversión
  B) Pin bar: mecha larga de un lado (>=2x el cuerpo y >=2x la mecha opuesta) -> rechazo
  C) Doji en extremo de Bollinger: cuerpo chico tocando una banda -> reversión
  D) Inside bar: vela dentro del rango de la anterior -> sigue la tendencia previa
  E) NR7 + micro-tendencia: la vela de rango más chico de las últimas 7 -> sigue
     la dirección de las 3 velas previas (se acumula presión, "resorte")
  F) Agotamiento por momentum: 3 velas seguidas del mismo color con cuerpo
     cada vez MÁS CHICO (se queda sin fuerza) -> apuesta a que revierte
     (justo lo contrario de "3 velas seguidas" ya probado, que apostaba a
     que seguía)
  G) Squeeze de Bollinger: el ancho de las bandas en su mínimo de 20 velas,
     y una vela decisiva rompe el rango de las últimas 10 -> sigue esa ruptura
  H) Pico de volumen + rechazo: volumen > 2x su media de 20, con mecha larga
     -> se apuesta en contra de la mecha (se "vendieron" o "compraron" de más)
  I) Volumen seco + ruptura: 3 velas con volumen bajo (mercado dormido)
     seguidas de una vela que rompe el rango reciente con volumen normal
     -> sigue esa ruptura

Todas devuelven (compra, venta) como Series bool, listas para
busqueda_exhaustiva.py / busqueda_externas.py.
"""
import numpy as np
import pandas as pd


def envolvente(v):
    c, o = v.close, v.open
    cuerpo_prev_max = np.maximum(o.shift(1), c.shift(1))
    cuerpo_prev_min = np.minimum(o.shift(1), c.shift(1))
    verde, roja = c > o, c < o
    envuelve_verde = verde & (o <= cuerpo_prev_min) & (c >= cuerpo_prev_max)
    envuelve_roja = roja & (o >= cuerpo_prev_max) & (c <= cuerpo_prev_min)
    # envolvente alcista tras vela roja anterior -> compra; bajista tras verde -> venta
    compra = envuelve_verde & (c.shift(1) < o.shift(1))
    venta = envuelve_roja & (c.shift(1) > o.shift(1))
    return compra, venta


def pin_bar(v):
    c, o, h, l = v.close, v.open, v.high, v.low
    cuerpo = (c - o).abs().clip(lower=1e-12)
    mecha_sup = h - np.maximum(o, c)
    mecha_inf = np.minimum(o, c) - l
    pin_alcista = (mecha_inf >= 2 * cuerpo) & (mecha_inf >= 2 * mecha_sup)
    pin_bajista = (mecha_sup >= 2 * cuerpo) & (mecha_sup >= 2 * mecha_inf)
    return pin_alcista, pin_bajista    # mecha abajo -> compra; mecha arriba -> venta


def doji_extremo(v):
    c, o, h, l = v.close, v.open, v.high, v.low
    rango = (h - l).clip(lower=1e-12)
    doji = (c - o).abs() / rango <= 0.10
    compra = doji & (l <= v.inf)
    venta = doji & (h >= v.sup)
    return compra, venta


def inside_bar(v):
    dentro = (v.high <= v.high.shift(1)) & (v.low >= v.low.shift(1))
    tendencia_previa = v.close.shift(1) - v.open.shift(1)
    compra = dentro & (tendencia_previa > 0)
    venta = dentro & (tendencia_previa < 0)
    return compra, venta


def nr7_micro_tendencia(v, n=7):
    rango = v.high - v.low
    es_nr = rango == rango.rolling(n).min()
    micro = v.close - v.close.shift(3)
    compra = es_nr & (micro > 0)
    venta = es_nr & (micro < 0)
    return compra, venta


def agotamiento_momentum(v):
    c, o = v.close, v.open
    cuerpo = (c - o).abs()
    verde, roja = c > o, c < o
    tres_verdes = verde & verde.shift(1, fill_value=False) & verde.shift(2, fill_value=False)
    tres_rojas = roja & roja.shift(1, fill_value=False) & roja.shift(2, fill_value=False)
    decreciente = (cuerpo < cuerpo.shift(1)) & (cuerpo.shift(1) < cuerpo.shift(2))
    # se agota la subida -> se espera reversión a la baja (venta), y viceversa
    venta = tres_verdes & decreciente
    compra = tres_rojas & decreciente
    return compra, venta


def squeeze_bollinger(v, ventana=20, pct=0.20):
    ancho = (v.sup - v.inf) / v.mid
    umbral = ancho.rolling(ventana).quantile(pct)
    apretado = ancho.shift(1) <= umbral.shift(1)
    techo10 = v.high.rolling(10).max().shift(1)
    piso10 = v.low.rolling(10).min().shift(1)
    compra = apretado & (v.close > techo10) & (v.close > v.open)
    venta = apretado & (v.close < piso10) & (v.close < v.open)
    return compra, venta


def volumen_pico_rechazo(v, n=20, mult=2.0):
    vol_media = v.volume.rolling(n).mean()
    pico = v.volume > mult * vol_media
    c, o, h, l = v.close, v.open, v.high, v.low
    cuerpo = (c - o).abs().clip(lower=1e-12)
    mecha_sup = h - np.maximum(o, c)
    mecha_inf = np.minimum(o, c) - l
    rechazo_arriba = pico & (mecha_sup >= 1.5 * cuerpo) & (mecha_sup > mecha_inf)
    rechazo_abajo = pico & (mecha_inf >= 1.5 * cuerpo) & (mecha_inf > mecha_sup)
    return rechazo_abajo, rechazo_arriba   # rechazo abajo -> compra; arriba -> venta


def volumen_seco_ruptura(v, n=20, mult=0.5):
    vol_media = v.volume.rolling(n).mean()
    seco = ((v.volume < mult * vol_media) & (v.volume.shift(1) < mult * vol_media.shift(1)) &
            (v.volume.shift(2) < mult * vol_media.shift(2)))
    techo5 = v.high.rolling(5).max().shift(1)
    piso5 = v.low.rolling(5).min().shift(1)
    compra = seco.shift(1, fill_value=False) & (v.close > techo5)
    venta = seco.shift(1, fill_value=False) & (v.close < piso5)
    return compra, venta


ESTRATEGIAS_PROPIAS = {
    "Envolvente":              envolvente,
    "Pin bar":                 pin_bar,
    "Doji en extremo":         doji_extremo,
    "Inside bar":              inside_bar,
    "NR7 + microtendencia":    nr7_micro_tendencia,
    "Agotamiento momentum":    agotamiento_momentum,
    "Squeeze Bollinger":       squeeze_bollinger,
    "Volumen pico+rechazo":    volumen_pico_rechazo,
    "Volumen seco+ruptura":    volumen_seco_ruptura,
}


def demarker(h, l, n=13):
    demax = (h - h.shift(1)).clip(lower=0)
    demin = (l.shift(1) - l).clip(lower=0)
    ma_max, ma_min = demax.rolling(n).mean(), demin.rolling(n).mean()
    return ma_max / (ma_max + ma_min).replace(0, np.nan)


def demarker_reentrada(v, n=13, sobreventa=20, sobrecompra=80, min_velas=5):
    """DeMarker(n): si sale de la banda [sobreventa,sobrecompra] por MÁS de
    min_velas seguidas, operar justo al reingresar (cruzar de vuelta hacia
    adentro). Reingresa desde abajo -> compra; desde arriba -> venta.
    Idea del usuario, sobre el indicador DeMarker de su plataforma."""
    dm = demarker(v.high, v.low, n) * 100
    fuera_abajo = dm < sobreventa
    fuera_arriba = dm > sobrecompra

    def racha(serie_bool):
        grupo = (serie_bool != serie_bool.shift()).cumsum()
        r = serie_bool.groupby(grupo).cumcount() + 1
        return r.where(serie_bool, 0)

    racha_abajo = racha(fuera_abajo)
    racha_arriba = racha(fuera_arriba)
    reingresa_desde_abajo = fuera_abajo.shift(1, fill_value=False) & ~fuera_abajo
    reingresa_desde_arriba = fuera_arriba.shift(1, fill_value=False) & ~fuera_arriba
    compra = reingresa_desde_abajo & (racha_abajo.shift(1) > min_velas)
    venta = reingresa_desde_arriba & (racha_arriba.shift(1) > min_velas)
    return compra.fillna(False), venta.fillna(False)


ESTRATEGIAS_PROPIAS["DeMarker(13) reentrada >5v"] = demarker_reentrada
