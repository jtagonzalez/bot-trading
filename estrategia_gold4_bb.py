import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Módulo consolidado de la familia de estrategias "Gold 4.0 + Bollinger".

Reúne en un solo lugar el indicador, los filtros de tendencia, las 3 formas
de entrada probadas y la gestión de dinero (martingala continua), para que
las próximas pruebas ("otras posibilidades") se construyan importando esto
en vez de copiar y pegar entre scripts sueltos.

Ver ESTRATEGIA_GOLD4_BOLLINGER.md para las reglas explicadas, la tabla de
resultados y las conclusiones. Este archivo es el código; ese es el registro.

Uso típico:

    import sqlite3, pandas as pd
    from estrategia_gold4_bb import cargar_velas, indicadores, FOREX

    c = sqlite3.connect("velas.db")
    v = cargar_velas(c, "EURUSD", operable_forex=True)
    v = indicadores(v)
    compra, venta = senal_extremo(v, nivel=1.0, con_gold=True)
    ciclos = ciclos_de(v, compra, venta, v.operable.values, max_ops=6)
"""
import sqlite3

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constantes de mercado
# ---------------------------------------------------------------------------
FOREX = ["AUDUSD", "EURGBP", "EURJPY", "EURUSD", "GBPJPY",
         "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]
CRIPTO = ["BTCUSD", "ETHUSD"]

PAYOUT = 0.86                          # turbo 1-5 min en -op
BE = 100 / (1 + PAYOUT)                # 53.76 %
MULT_MARTINGALA = (1 + PAYOUT) / PAYOUT  # ≈2.16 — recupera lo perdido + $1
VENTANA_INCLINACION = 20               # velas para medir la pendiente de la cian


def cargar_velas(con, activo, tf=60, operable_forex=True):
    """Trae las velas de 1 min de un activo y arma la columna 'operable'.

    operable_forex: True para pares de forex (horario 01-17h UTC, lun-vie);
    False para cripto (24x7, incluye fines de semana).
    """
    v = pd.read_sql("SELECT ts, open, high, low, close, volume FROM velas WHERE activo=? "
                    "AND timeframe=? ORDER BY ts", con, params=(activo, tf))
    v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
    if operable_forex:
        v["operable"] = (v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)).values
    else:
        v["operable"] = True
    return v


def wma(s, n):
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


# ---------------------------------------------------------------------------
# 1) Indicador Gold 4.0 (traducción del script Lua original del usuario)
# ---------------------------------------------------------------------------
def gold4(v, precio_b="hl2"):
    """Sistema A (flechas Up/Down) y sistema B (aviso COMPRAR/VENDER)."""
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
    up_b = (b1 > b2) & (b1.shift(1) < b2.shift(1))     # aviso COMPRAR
    dn_b = (b1 < b2) & (b1.shift(1) > b2.shift(1))     # aviso VENDER
    return up_a, dn_a, up_b, dn_b


# ---------------------------------------------------------------------------
# 2) Indicadores de apoyo: Bollinger(14,2), línea cian (SMA50), ATR, pendiente
# ---------------------------------------------------------------------------
def indicadores(v):
    """Agrega al DataFrame: mid/sup/inf (Bollinger 14,2), cian (SMA50, la
    "línea de tendencia" lenta), atr (14) y pend (inclinación de la cian en
    VENTANA_INCLINACION velas, en unidades de ATR — >0 sube, <0 baja).
    También agrega up_a/dn_a/up_b/dn_b (Gold 4.0) y verde/roja.
    """
    c, o, h, l = v.close, v.open, v.high, v.low
    v = v.copy()
    v["mid"] = c.rolling(14).mean()                 # línea media / "naranja"
    sd = c.rolling(14).std(ddof=0)
    v["sup"], v["inf"] = v.mid + 2 * sd, v.mid - 2 * sd
    v["cian"] = c.rolling(50).mean()                # línea de tendencia lenta
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    v["atr"] = tr.rolling(14).mean()
    v["pend"] = (v.cian - v.cian.shift(VENTANA_INCLINACION)) / v.atr
    v["verde"], v["roja"] = c > o, c < o
    v["up_a"], v["dn_a"], v["up_b"], v["dn_b"] = gold4(v)
    return v


def _base_tendencia(v, nivel):
    """Piezas comunes a los 3 setups: vela entera del lado correcto de la
    cian + inclinación mínima en la dirección correcta + vela previa del
    mismo color. Devuelve (lado_alcista, lado_bajista) como Series bool.
    """
    vela_entera_arriba = v.low > v.cian
    vela_entera_abajo = v.high < v.cian
    color_prev_compra = v.verde & v.verde.shift(1, fill_value=False)
    color_prev_venta = v.roja & v.roja.shift(1, fill_value=False)
    lado_alcista = vela_entera_arriba & (v.pend > nivel) & color_prev_compra
    lado_bajista = vela_entera_abajo & (v.pend < -nivel) & color_prev_venta
    return lado_alcista, lado_bajista


# ---------------------------------------------------------------------------
# 3) Las 3 formas de entrada probadas (requieren indicadores(v) ya aplicado)
# ---------------------------------------------------------------------------
def senal_extremo(v, nivel=1.0, con_gold=True):
    """Setup ORIGINAL: la mecha toca el extremo de Bollinger (rebote en el
    extremo). VENTA: vela roja, mecha toca la banda superior, tendencia
    bajista. COMPRA: espejo. Resultado: 46.8-53.5% de acierto, todas las
    pruebas por debajo del breakeven o con muestra demasiado chica para
    confirmar ventaja (ver ESTRATEGIA_GOLD4_BOLLINGER.md, sección 3.1)."""
    lado_alcista, lado_bajista = _base_tendencia(v, nivel)
    compra = v.verde & (v.low <= v.inf) & lado_alcista
    venta = v.roja & (v.high >= v.sup) & lado_bajista
    if con_gold:
        return compra & v.up_b, venta & v.dn_b
    return compra, venta


def senal_media(v, nivel=1.0, con_gold=True):
    """Rebote en la MEDIA (14) en vez del extremo: el precio retrocede hasta
    tocar la media dentro de la tendencia y cierra de nuevo del lado
    correcto (rechazo), se opera a favor de la tendencia. Da muchas más
    señales que el extremo, pero el acierto se mantiene ~49% (por debajo
    del breakeven) — ver sección 3.2."""
    lado_alcista, lado_bajista = _base_tendencia(v, nivel)
    toca_media_compra = (v.low <= v.mid) & (v.close > v.mid)
    toca_media_venta = (v.high >= v.mid) & (v.close < v.mid)
    compra = v.verde & toca_media_compra & lado_alcista
    venta = v.roja & toca_media_venta & lado_bajista
    if con_gold:
        return compra & v.up_b, venta & v.dn_b
    return compra, venta


def senal_zigzag(v, min_rebotes=2, max_inclinacion=None):
    """Mercado LATERAL en zigzag entre bandas (lo opuesto a las 3 anteriores,
    que exigían tendencia). Detecta toques alternados de sup/inf ("de
    extremo a extremo"); con min_rebotes confirmados, opera esperando que
    siga rebotando (tocó abajo -> compra, tocó arriba -> venta).

    max_inclinacion: exige |pend| <= max_inclinacion (cian prácticamente
    plana en las VENTANA_INCLINACION velas previas, en unidades de ATR).
    Corrige el defecto encontrado en la 1a versión: "rebotar entre bandas"
    no distinguía mercado lateral real de un CANAL con tendencia (las
    bandas bajando/subiendo juntas mientras el precio zigzaguea dentro) —
    ver ESTRATEGIA_GOLD4_BOLLINGER.md sección 3.4."""
    h, l, sup, inf = v.high.values, v.low.values, v.sup.values, v.inf.values
    n = len(v)
    tipo = np.full(n, "", dtype="<U1")
    ds, di = h - sup, inf - l
    toca_s, toca_i = ds >= 0, di >= 0
    ambos = toca_s & toca_i
    tipo[toca_s & ~ambos] = "S"
    tipo[toca_i & ~ambos] = "I"
    tipo[ambos] = np.where(ds[ambos] >= di[ambos], "S", "I")

    idx = np.flatnonzero(tipo != "")
    tipos = tipo[idx]
    compra, venta = np.zeros(n, bool), np.zeros(n, bool)
    needed = min_rebotes + 1
    for k in range(needed - 1, len(idx)):
        seq = tipos[k - needed + 1: k + 1]
        if all(seq[j] != seq[j - 1] for j in range(1, len(seq))):
            i = idx[k]
            (compra if tipos[k] == "I" else venta)[i] = True
    compra_s = pd.Series(compra, index=v.index)
    venta_s = pd.Series(venta, index=v.index)
    if max_inclinacion is not None:
        plana = v.pend.abs() <= max_inclinacion
        compra_s &= plana
        venta_s &= plana
    return compra_s, venta_s


def senal_3velas(v, con_cian=False):
    """Continuación de tendencia: 3 velas seguidas del mismo color, las 3
    cerrando del lado correcto de la media (14). No usa aviso del Gold 4.0
    (la regla del usuario no lo pedía). Es la que más señales genera con
    diferencia (~1.350/día en 10 pares) y el acierto es ~49% — sección 3.3.
    con_cian=True agrega el filtro extra "cian también a favor" (no mejora)."""
    tres_verdes = v.verde & v.verde.shift(1, fill_value=False) & v.verde.shift(2, fill_value=False)
    tres_rojas = v.roja & v.roja.shift(1, fill_value=False) & v.roja.shift(2, fill_value=False)
    arriba_3 = ((v.close > v.mid) & (v.close.shift(1) > v.mid.shift(1)) &
                (v.close.shift(2) > v.mid.shift(2)))
    abajo_3 = ((v.close < v.mid) & (v.close.shift(1) < v.mid.shift(1)) &
               (v.close.shift(2) < v.mid.shift(2)))
    compra = tres_verdes & arriba_3
    venta = tres_rojas & abajo_3
    if con_cian:
        compra &= v.cian > v.cian.shift(10)
        venta &= v.cian < v.cian.shift(10)
    return compra, venta


# ---------------------------------------------------------------------------
# 4) Gestión de dinero: martingala continua a 1 minuto
# ---------------------------------------------------------------------------
def ciclos_de(v, compra, venta, operable, max_ops=6):
    """Simula la martingala continua: tras perder, la SIGUIENTE vela de 1
    min abre otra operación en el mismo sentido, monto x MULT_MARTINGALA,
    hasta max_ops intentos. Empate = se devuelve el monto y se repite en la
    vela siguiente (consume un intento). Devuelve lista de tuplas
    (ts_inicio, neto_del_ciclo, intentos_usados, gano: bool)."""
    ts, cl = v.ts.values, v.close.values
    n = len(v)
    senal = np.where(compra.values, 1, np.where(venta.values, -1, 0))
    op = np.asarray(operable)
    out = []
    i = 0
    while i < n - 1:
        if senal[i] == 0 or not op[i]:
            i += 1
            continue
        sentido, apuesta, neto, k, j, gano = senal[i], 1.0, 0.0, 0, i, False
        while k < max_ops and j + 1 < n and ts[j + 1] - ts[j] == 60 and op[j]:
            r = np.sign(cl[j + 1] - cl[j]) * sentido
            k += 1
            if r > 0:
                neto += apuesta * PAYOUT
                gano = True
                j += 1
                break
            if r < 0:
                neto -= apuesta
                apuesta *= MULT_MARTINGALA
            j += 1
        if k:
            out.append((ts[i], neto, k, gano))
        i = max(j, i + 1)          # no se abren ciclos nuevos con uno abierto
    return out


# ---------------------------------------------------------------------------
# Ayuda rápida para evaluar una señal (acierto sin martingala + con)
# ---------------------------------------------------------------------------
def resumen(v, compra, venta, max_ops=6):
    """(ganadas, perdidas, acierto%, $/dia_sin_martingala_aprox, ciclos)"""
    ts, cl = v.ts.values, v.close.values
    op = v.operable.values
    ok = np.r_[ts[1:] - ts[:-1] == 60, False] & op
    r = np.sign(np.r_[cl[1:] - cl[:-1], 0])
    g = p = 0
    for m, sg in ((compra.values & ok, 1), (venta.values & ok, -1)):
        x = r[m] * sg
        g += int((x > 0).sum()); p += int((x < 0).sum())
    ciclos = ciclos_de(v, compra, venta, op, max_ops)
    acierto = 100 * g / (g + p) if (g + p) else float("nan")
    return g, p, acierto, g * PAYOUT - p, ciclos


if __name__ == "__main__":
    # demo rápida: EURUSD, los 3 setups, nivel 1.0, con Gold donde aplica
    con = sqlite3.connect("velas.db")
    v = indicadores(cargar_velas(con, "EURUSD"))
    print(f"BE turbo 86%: {BE:.2f}%\n")
    for nombre, (compra, venta) in (
            ("extremo",  senal_extremo(v, 1.0, con_gold=True)),
            ("media",    senal_media(v, 1.0, con_gold=True)),
            ("3 velas",  senal_3velas(v)),
            ("zigzag",   senal_zigzag(v, 2)),
    ):
        g, p, acierto, flat, ciclos = resumen(v, compra, venta)
        perd = sum(1 for x in ciclos if not x[3])
        print(f"{nombre:<10} señales={g + p:<6} acierto={acierto:5.2f}%  "
              f"ciclos={len(ciclos):<6} perdidos={perd}")
