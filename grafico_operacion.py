"""
Genera, bajo demanda, el grafico de UNA operacion de live.db y lo devuelve como
PNG en memoria. Lo usa panel.py cuando haces clic en un punto de la curva.

De donde salen los datos (en este orden de preferencia):
  1. Velas descargadas de IQ Option en el momento — es lo unico que cubre los
     pasos de martingala, que ocurren DESPUES de la vela de la señal.
  2. Si la descarga falla (sin conexion, activo cerrado, histórico vencido), se
     dibuja con lo que el propio bot guardo al decidir: `ventana_ohlc` (20 velas
     de contexto) y `vela_resultado`. Siempre hay algo que mostrar.

Sirve para los dos tipos de bot:
  - operacion simple (modelos ML): entrada, salida, Bollinger y el panel con los
    indicadores REALES que vio el modelo.
  - ciclo de martingala (bot_weis_bollinger): marca cada paso con su monto y su
    resultado, mas el neto del ciclo.

Se cachea en disco (capturas_live/panel/) para que el segundo clic sea instantaneo.
"""
import io
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["text.parse_math"] = False
import matplotlib.pyplot as plt
import numpy as np

FONDO, TEXTO, REJILLA = "#131722", "#d1d4dc", "#2a2e39"
VERDE, ROJA = "#26a69a", "#ef5350"
COL_WIN, COL_LOSS, COL_TIE = "#4caf50", "#f44336", "#9e9e9e"
AMARILLO = "#ffeb3b"

CACHE_DIR = os.path.join("capturas_live", "panel")
_lock = threading.Lock()          # matplotlib no es seguro entre hilos

# etiquetas lindas para los indicadores que guardan los bots
ETIQUETAS = [
    ("rsi7", "RSI 7"), ("rsi14", "RSI 14"), ("rsi21", "RSI 21"),
    ("stoch_k", "Estocástico %K"), ("stoch_d", "Estocástico %D"),
    ("williams_r", "Williams %R"), ("cci20", "CCI(20)"),
    ("adx14", "ADX(14)"), ("plus_di", "+DI"), ("minus_di", "-DI"),
    ("macd_hist", "MACD hist."), ("bb_pct", "Bollinger %B"), ("bb_width", "Bollinger ancho"),
    ("atr_pct", "ATR %"), ("racha", "racha misma dir."),
    ("demarker", "DeMarker"), ("racha_previa", "racha previa"),
    ("wwv_vol_acum", "WWV acumulado"), ("wwv_umbral_onda_anterior", "umbral onda ant."),
    ("wwv_color_onda", "color onda"), ("atr_mult", "ATR mult"), ("racha_min", "racha mínima"),
    ("periodo", "periodo"), ("min_velas", "min velas"), ("max_pasos", "máx pasos"),
]


def _leer(db, op_id):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    f = con.execute("""
        SELECT o.id, o.precio_entrada, o.precio_salida, o.resultado, o.ganancia,
               o.monto, o.vela_resultado, o.error_detalle,
               p.activo, p.direccion, p.ts_vela, p.timeframe, p.confianza,
               p.ventana_ohlc, p.indicadores, r.modelo_nombre
        FROM operaciones o
        JOIN predicciones p ON p.id = o.prediccion_id
        JOIN runs r ON r.id = o.run_id
        WHERE o.id = ?""", (op_id,)).fetchone()
    con.close()
    return dict(f) if f else None


def _pasos_de(detalle):
    if not detalle:
        return None
    try:
        d = json.loads(detalle)
    except Exception:
        return None
    pasos = d.get("ciclo_pasos")
    return pasos if isinstance(pasos, list) and pasos else None


def _velas_descargadas(api, activo, tf, ts_vela, n_pasos):
    """Velas que cubren contexto + señal + todos los pasos del ciclo."""
    if api is None:
        return None
    try:
        hasta = ts_vela + (n_pasos + 3) * tf
        cuantas = 22 + n_pasos + 4
        v = api.get_candles(activo, tf, cuantas, hasta)
        if not v:
            return None
        v = sorted(v, key=lambda c: c["from"])
        return [{"ts": int(c["from"]), "o": float(c["open"]), "h": float(c["max"]),
                 "l": float(c["min"]), "c": float(c["close"])} for c in v]
    except Exception:
        return None


def _velas_guardadas(op):
    """Lo que el bot guardo: o bien las velas de TODO el ciclo (las deja
    bot_weis_bollinger al cerrar, en `vela_resultado`), o bien la ventana de
    contexto de 20 velas mas la que resolvio."""
    try:
        vr = json.loads(op["vela_resultado"]) if op["vela_resultado"] else None
    except Exception:
        vr = None
    if isinstance(vr, dict) and isinstance(vr.get("velas"), list) and vr["velas"]:
        return [{"ts": int(v[0]), "o": float(v[1]), "h": float(v[2]),
                 "l": float(v[3]), "c": float(v[4])} for v in vr["velas"]]

    try:
        ventana = json.loads(op["ventana_ohlc"]) if op["ventana_ohlc"] else None
    except Exception:
        ventana = None
    if not ventana:
        return None
    tf, ts_vela = op["timeframe"], op["ts_vela"]
    ts0 = ts_vela - (len(ventana) - 1) * tf
    velas = [{"ts": ts0 + k * tf, "o": float(v[0]), "h": float(v[1]),
              "l": float(v[2]), "c": float(v[3])} for k, v in enumerate(ventana)]
    if isinstance(vr, dict) and "o" in vr:
        velas.append({"ts": ts_vela + tf, "o": float(vr["o"]), "h": float(vr["h"]),
                      "l": float(vr["l"]), "c": float(vr["c"])})
    return velas


def _bollinger(cierres, periodo, desv):
    if len(cierres) < periodo:
        return None
    ult = np.array(cierres[-periodo:], dtype=float)
    m, s = ult.mean(), ult.std(ddof=1)
    return m, m + desv * s, m - desv * s


def _dibujar(op, velas, pasos):
    try:
        op["_ind"] = json.loads(op["indicadores"]) if op["indicadores"] else {}
    except Exception:
        op["_ind"] = {}
    tf, ts_vela = op["timeframe"], op["ts_vela"]
    activo, direccion = op["activo"], op["direccion"]
    venta = direccion == "put"
    n_pasos = len(pasos) if pasos else 1

    i_sen_full = next((k for k, v in enumerate(velas) if v["ts"] == ts_vela), None)
    if i_sen_full is None:
        i_sen_full = min(range(len(velas)), key=lambda k: abs(velas[k]["ts"] - ts_vela))

    # Bollinger se calcula con TODAS las velas previas disponibles (necesita 14
    # o 20), aunque despues no se muestren todas en pantalla.
    periodo_bb = 14 if "wwv_vol_acum" in (op.get("_ind") or {}) else 20
    cierres_previos = [v["c"] for v in velas[:i_sen_full + 1]]

    # Encuadre: poco contexto previo y TODO el ciclo. Lo que importa son las
    # velas donde se jugo cada paso, no las 20 anteriores a la señal.
    cubiertos = sum(1 for v in velas if v["ts"] > ts_vela)
    desde = max(0, i_sen_full - 10)
    hasta = min(len(velas), i_sen_full + n_pasos + 3)
    velas = velas[desde:hasta]
    i_sen = i_sen_full - desde
    n = len(velas)
    faltan = max(0, n_pasos - cubiertos)

    o = [v["o"] for v in velas]; h = [v["h"] for v in velas]
    l = [v["l"] for v in velas]; c = [v["c"] for v in velas]
    tiempos = [datetime.fromtimestamp(v["ts"], tz=timezone.utc) for v in velas]

    ind = op.get("_ind") or {}

    fig, ax = plt.subplots(figsize=(15.5, 7.2), facecolor=FONDO)
    fig.subplots_adjust(left=0.045, right=0.745, top=0.87, bottom=0.11)
    ax.set_facecolor(FONDO)
    ax.tick_params(colors=TEXTO, labelsize=8)
    for s in ax.spines.values():
        s.set_color(REJILLA)

    rango = (max(h) - min(l)) or 1

    # zona del ciclo: desde la vela de entrada del paso 1 hasta la del ultimo
    # paso. Es donde se jugo el dinero — queda sombreada para que salte a la
    # vista frente al contexto previo.
    if n_pasos and i_sen + 1 < n:
        fin = min(n - 1, i_sen + n_pasos)
        ax.axvspan(i_sen + 0.5, fin + 0.5, color="#ffffff", alpha=0.045, zorder=0)
        ax.axvspan(i_sen + 0.5, fin + 0.5, facecolor="none", edgecolor=REJILLA,
                   lw=0.8, ls="--", zorder=0)

    for xi in range(n):
        col = VERDE if c[xi] >= o[xi] else ROJA
        ax.vlines(xi, l[xi], h[xi], color=col, lw=1)
        ax.bar(xi, max(abs(c[xi] - o[xi]), rango * 0.003), bottom=min(o[xi], c[xi]),
               color=col, width=0.62, zorder=3)

    bb = _bollinger(cierres_previos, periodo_bb, 2)
    ys = list(h) + list(l)
    if bb:
        m20, sup, inf = bb
        ys += [sup, inf]
        ax.hlines([sup, m20, inf], -0.6, n - 0.4,
                  color=["#f23645", "#787b86", "#f23645"], lw=[1.2, 0.9, 1.2],
                  linestyles=["-", "--", "-"], alpha=0.75, zorder=1)
        xt = n * 0.30
        for yv, et in ((sup, f"BB sup ({periodo_bb},2)"), (inf, f"BB inf ({periodo_bb},2)")):
            ax.text(xt, yv, et, color="#f23645", fontsize=7.5, ha="center",
                    va="bottom" if yv == sup else "top", zorder=6,
                    bbox=dict(boxstyle="round,pad=0.12", fc=FONDO, ec="none", alpha=0.85))

    ymin, ymax = min(ys) - rango * 0.10, max(ys) + rango * 0.26
    ax.set_ylim(ymin, ymax)
    ax.set_xlim(-0.8, n - 0.2)

    ax.annotate(("SEÑAL\nVENDER (PUT)" if venta else "SEÑAL\nCOMPRAR (CALL)"),
                xy=(i_sen, h[i_sen] if venta else l[i_sen]),
                xytext=(i_sen, ymax - rango * 0.05 if venta else ymin + rango * 0.05),
                ha="center", color=AMARILLO, fontsize=9, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=AMARILLO), zorder=7)

    # marcas por paso (martingala) o marca unica de entrada/salida
    if pasos:
        for k, p in enumerate(pasos):
            idx = i_sen + 1 + k
            if idx >= n:
                break
            res = p.get("resultado")
            col = COL_WIN if res == "win" else COL_LOSS if res == "loss" else COL_TIE
            etq = {"win": "GANA", "loss": "PIERDE", "tie": "EMPATA"}.get(res, str(res))
            ax.axvline(idx, color=col, lw=0.7, ls=":", alpha=0.55)
            ax.scatter([idx], [c[idx]], color=col, s=34, zorder=6)
            ax.text(idx + 0.12, ymax - rango * (0.09 + 0.055 * k),
                    f"P{p.get('paso', k+1)}  ${p.get('monto', 0):.2f}  {etq}  "
                    f"{p.get('ganancia', 0):+.2f}",
                    color=col, fontsize=8, fontweight="bold", va="center", zorder=7,
                    bbox=dict(boxstyle="round,pad=0.14", fc=FONDO, ec=col, alpha=0.92))
    else:
        if op["precio_entrada"]:
            ax.axhline(op["precio_entrada"], color=AMARILLO, lw=0.8, ls=":",
                       xmin=(i_sen + 1) / n)
        if op["precio_salida"] is not None and i_sen + 1 < n:
            col = COL_WIN if op["resultado"] == "win" else COL_LOSS if op["resultado"] == "loss" else COL_TIE
            ax.scatter([i_sen + 1], [op["precio_salida"]], color=col, s=48, zorder=6)
            ax.axhline(op["precio_salida"], color=col, lw=0.8, ls=":")

    g = op["ganancia"]
    res_txt = "GANA" if (g or 0) > 0 else "PIERDE" if (g or 0) < 0 else "EMPATE"
    col_res = COL_WIN if (g or 0) > 0 else COL_LOSS if (g or 0) < 0 else COL_TIE
    caja = f"{res_txt}   P&L ${g:+.2f}" if g is not None else res_txt
    if pasos:
        arriesgado = sum(p.get("monto", 0) for p in pasos)
        caja += f"\n{len(pasos)} paso(s) de martingala · arriesgado ${arriesgado:.2f}"
    if op["precio_entrada"] and op["precio_salida"]:
        caja += f"\nentrada {op['precio_entrada']:.5f} → salida {op['precio_salida']:.5f}"
    ax.text(0.012, 0.975 if (g or 0) <= 0 else 0.03, caja, transform=ax.transAxes,
            color=col_res, fontsize=9.5, fontweight="bold", ha="left",
            va="top" if (g or 0) <= 0 else "bottom", zorder=8,
            bbox=dict(boxstyle="round", fc=FONDO, ec=col_res))

    paso_x = max(1, round(n / 7))
    ticks = list(range(0, n, paso_x))
    ax.set_xticks(ticks, [f"{tiempos[k]:%H:%M} UTC\n{(tiempos[k].hour-5)%24:02d}:{tiempos[k]:%M} CO"
                          for k in ticks], fontsize=8)
    ax.grid(color=REJILLA, lw=0.6)
    ax.yaxis.tick_right()

    if faltan:
        ax.text(0.5, 0.5,
                f"faltan las velas de {faltan} paso(s) del ciclo\n"
                f"(no estaban guardadas y no se pudieron descargar)",
                transform=ax.transAxes, color="#ffb74d", fontsize=11,
                ha="center", va="center", zorder=9,
                bbox=dict(boxstyle="round", fc=FONDO, ec="#ffb74d"))

    if ind:
        filas = []
        for clave, etq in ETIQUETAS:
            if clave in ind and ind[clave] is not None:
                v = ind[clave]
                filas.append(f"  {etq:<20} {v:>10.3f}" if isinstance(v, (int, float))
                             else f"  {etq:<20} {v:>10}")
        if filas:
            ax.text(1.015, 0.985, "LO QUE VIO EL BOT AL DECIDIR:\n" + "\n".join(filas),
                    transform=ax.transAxes, color=TEXTO, fontsize=7.6, va="top",
                    ha="left", family="monospace",
                    bbox=dict(boxstyle="round", fc="#1a1a19", ec=REJILLA))

    minutos = tf // 60
    ax.set_title(
        f"{activo} · operación #{op['id']} · {op['modelo_nombre']} · vela de {minutos} min\n"
        f"señal {tiempos[i_sen]:%Y-%m-%d %H:%M} UTC "
        f"({(tiempos[i_sen].hour-5)%24:02d}:{tiempos[i_sen]:%M} hora Colombia) · "
        f"dirección {'VENTA' if venta else 'COMPRA'}"
        + (f" · confianza {op['confianza']:.4f}" if op["confianza"] is not None else ""),
        color=TEXTO, fontsize=10.5, loc="left")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=104, facecolor=FONDO)
    plt.close(fig)
    return buf.getvalue()


def png_operacion(db, op_id, api=None, usar_cache=True):
    """PNG (bytes) de la operacion, o None si no hay datos para dibujarla."""
    op = _leer(db, op_id)
    if not op:
        return None
    pasos = _pasos_de(op["error_detalle"])
    cerrada = op["resultado"] in ("win", "loss", "tie")
    ruta = os.path.join(CACHE_DIR, f"op{op_id}.png")
    if usar_cache and cerrada and os.path.exists(ruta):
        with open(ruta, "rb") as fh:
            return fh.read()

    velas = _velas_descargadas(api, op["activo"], op["timeframe"],
                               op["ts_vela"], len(pasos) if pasos else 1)
    if not velas:
        velas = _velas_guardadas(op)
    if not velas:
        return None

    with _lock:
        png = _dibujar(op, velas, pasos)
    if cerrada:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(ruta, "wb") as fh:
                fh.write(png)
        except Exception:
            pass
    return png
