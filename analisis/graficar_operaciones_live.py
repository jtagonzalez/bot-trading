import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Genera una imagen por cada operación real del bot, usando los datos que el
propio bot guardó en live.db al momento de decidir: las 20 velas de contexto
(`ventana_ohlc`) y la vela que resolvió el resultado (`vela_resultado`). No
hace falta descargar nada — es exactamente lo que el bot vio.

    py graficar_operaciones_live.py --ids 68,67,63,62,61,59
    py graficar_operaciones_live.py --run 8 --resultado loss --n 6
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["text.parse_math"] = False
import matplotlib.pyplot as plt
import numpy as np

FONDO, TEXTO, REJILLA = "#131722", "#d1d4dc", "#2a2e39"
VERDE, ROJA = "#26a69a", "#ef5350"
PAYOUT = 0.86


def bollinger_desde_ventana(ventana, cierre_decision):
    """Bandas de Bollinger(20,2) EXACTAS a partir de las 20 velas guardadas:
    una media/desviación de 20 periodos solo necesita esas últimas 20 velas,
    así que da el mismo número que calculó el modelo (verificado contra
    bb_pct/bb_width guardados). Solo vale en la vela de la señal — no se
    puede dibujar la banda completa hacia atrás porque no hay más historial
    guardado que esas 20 velas."""
    closes = np.array([v[3] for v in ventana])
    m20, std = closes.mean(), closes.std(ddof=1)
    return m20, m20 + 2 * std, m20 - 2 * std


def graficar(op, carpeta):
    (oid, activo, direccion, tf, ts_vela, entrada, salida, resultado, ganancia,
     ventana_json, vela_res_json, confianza, indicadores_json) = op
    ventana = json.loads(ventana_json)
    vr = json.loads(vela_res_json) if vela_res_json else None
    ind = json.loads(indicadores_json) if indicadores_json else {}
    velas = list(ventana)
    if vr:
        velas.append([vr["o"], vr["h"], vr["l"], vr["c"]])
    n = len(velas)
    i_sen = len(ventana) - 1          # indice de la vela de la señal (penultima si hay resultado)
    ts0 = ts_vela - i_sen * tf
    tiempos = [datetime.fromtimestamp(ts0 + k * tf, tz=timezone.utc) for k in range(n)]
    pip = 0.01 if "JPY" in activo.upper() else (1.0 if any(c in activo.upper() for c in ("BTC", "ETH")) else 0.0001)

    fig, ax = plt.subplots(figsize=(15.5, 6.5), facecolor=FONDO)
    fig.subplots_adjust(right=0.78)
    ax.set_facecolor(FONDO)
    ax.tick_params(colors=TEXTO, labelsize=8)
    for s in ax.spines.values():
        s.set_color(REJILLA)

    x = np.arange(n)
    altos = [v[1] for v in velas]
    bajos = [v[2] for v in velas]
    for xi, (o, h, l, c) in zip(x, velas):
        col = VERDE if c >= o else ROJA
        ax.vlines(xi, l, h, color=col, lw=1)
        ax.bar(xi, max(abs(c - o), (max(altos) - min(bajos)) * 0.002), bottom=min(o, c),
               color=col, width=0.65)

    rango = max(altos) - min(bajos)
    ax.set_xlim(-1, n + 1)

    # Bollinger(20,2) exacto en la vela de señal (ver nota en la función).
    # Se dibuja a lo ANCHO de todo el grafico (de referencia), con la
    # etiqueta pegada al eje IZQUIERDO para no chocar con las anotaciones de
    # entrada/salida, que viven del lado derecho.
    m20, sup, inf = bollinger_desde_ventana(ventana, ventana[-1][3])
    ax.hlines([sup, m20, inf], -1, n + 1,
              color=["#f23645", "#787b86", "#f23645"], lw=[1.3, 0.9, 1.3],
              linestyles=["-", "--", "-"], zorder=1, alpha=0.8)
    for y, etq in ((sup, "BB sup"), (m20, "media 20"), (inf, "BB inf")):
        ax.text(n * 0.42, y, etq, color="#f23645" if etq != "media 20" else "#999",
                fontsize=7.5, va="bottom", ha="center", zorder=6,
                bbox=dict(boxstyle="round,pad=0.1", fc=FONDO, ec="none", alpha=0.85))
    ax.text(0.5, 0.995, "Bollinger(20,2) EXACTO en la vela de señal — no hay más historial "
            "guardado para dibujar la banda completa hacia atrás",
            transform=ax.transAxes, color="#787b86", fontsize=7.3, style="italic",
            ha="center", va="top")

    ymax = max(altos + [sup]) + rango * 0.08
    ymin = min(bajos + [inf]) - rango * 0.08
    ax.set_ylim(ymin, ymax)

    venta = direccion == "put"
    s = velas[i_sen]
    ax.annotate(("VENDER (PUT)" if venta else "COMPRAR (CALL)") + f"  conf={confianza:.4f}",
                xy=(i_sen, s[1] if venta else s[2]),
                xytext=(i_sen, ymax - (ymax - ymin) * 0.04 if venta else ymin + (ymax - ymin) * 0.04),
                ha="center", color="#ffeb3b", fontsize=9, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color="#ffeb3b"))
    ax.axhline(entrada, color="#ffeb3b", lw=0.8, ls=":", xmin=(i_sen + 1) / (n + 2))
    ax.text(i_sen + 1.1, entrada, f" entrada {entrada:.5f}", color="#ffeb3b", fontsize=8, va="bottom")

    if vr:
        j = n - 1
        col = {"win": "#4caf50", "loss": "#f44336", "tie": "#9e9e9e"}.get(resultado, "#9e9e9e")
        icono = {"win": "GANA", "loss": "PIERDE", "tie": "EMPATE"}.get(resultado, resultado)
        ax.scatter([j], [salida], color=col, s=40, zorder=5)
        ax.axhline(salida, color=col, lw=0.8, ls=":")
        mov = (salida - entrada) / pip
        # cuadro de resultado: SIEMPRE en una esquina fija (arriba-izquierda si
        # pierde, abajo-izquierda si gana), para que nunca choque con las
        # bandas ni se corte en el borde de la figura
        esquina_y = 0.97 if resultado != "win" else 0.08
        ax.text(0.015, esquina_y,
                f"{icono}\nsalida {salida:.5f}  ({mov:+.1f} unid. de precio)\nP&L ${ganancia:+.2f}",
                transform=ax.transAxes, color=col, fontsize=9.5, fontweight="bold",
                ha="left", va="top" if esquina_y > 0.5 else "bottom",
                bbox=dict(boxstyle="round", fc=FONDO, ec=col))

    ticks = list(range(0, n, 3))
    ax.set_xticks(ticks, [tiempos[k].strftime("%H:%M") for k in ticks])
    ax.grid(color=REJILLA, lw=0.6)
    ax.yaxis.tick_right()

    # panel con los indicadores REALES que calculó el modelo en esta señal
    # (guardados tal cual en predicciones.indicadores; no son una aproximación)
    if ind:
        filas = [
            ("RSI 7/14/21", f"{ind.get('rsi7', float('nan')):.1f} / {ind.get('rsi14', float('nan')):.1f} / {ind.get('rsi21', float('nan')):.1f}"),
            ("Estocástico %K/%D", f"{ind.get('stoch_k', float('nan')):.1f} / {ind.get('stoch_d', float('nan')):.1f}"),
            ("Williams %R", f"{ind.get('williams_r', float('nan')):.1f}"),
            ("CCI(20)", f"{ind.get('cci20', float('nan')):.1f}"),
            ("ADX(14)  +DI/-DI", f"{ind.get('adx14', float('nan')):.1f}  {ind.get('plus_di', float('nan')):.1f}/{ind.get('minus_di', float('nan')):.1f}"),
            ("MACD hist.", f"{ind.get('macd_hist', float('nan')):+.3f}"),
            ("Bollinger %B / ancho", f"{ind.get('bb_pct', float('nan')):.3f} / {ind.get('bb_width', float('nan')):.4f}"),
            ("ATR %", f"{ind.get('atr_pct', float('nan')):.3f}%"),
            ("racha misma dirección", f"{ind.get('racha', float('nan')):.0f} velas"),
        ]
        texto = "LO QUE VIO EL MODELO (valores reales guardados):\n" + "\n".join(
            f"  {k:<22} {v}" for k, v in filas)
        ax.text(1.015, 0.98, texto, transform=ax.transAxes, color=TEXTO, fontsize=7.8,
                va="top", ha="left", family="monospace",
                bbox=dict(boxstyle="round", fc="#1a1a19", ec=REJILLA))
    minutos = tf // 60
    texto_res = {"win": "GANA", "loss": "PIERDE", "tie": "EMPATE"}.get(resultado, resultado)
    hora_entrada = tiempos[i_sen + 1] if i_sen + 1 < n else tiempos[i_sen]  # cierre de la vela señal = entrada
    titulo = (f"{activo} · operación REAL (PRACTICE) · vela de {minutos} min · "
              f"entrada {hora_entrada:%Y-%m-%d %H:%M} UTC\n"
              f"modelo ML (gb_15m) · confianza {confianza:.4f} · resultado: {texto_res}")
    ax.set_title(titulo, color=TEXTO, fontsize=10, loc="left")
    nombre = f"op{oid}_{activo}_{hora_entrada:%Y%m%d_%H%M}_{resultado}.png"
    ruta = os.path.join(carpeta, nombre)
    fig.savefig(ruta, dpi=110, facecolor=FONDO)
    plt.close(fig)
    return ruta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default=None, help="ids de operaciones.id separados por coma")
    ap.add_argument("--run", type=int, default=8)
    ap.add_argument("--resultado", default="loss")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--db", default="live.db")
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    if args.ids:
        ids = [int(x) for x in args.ids.split(",")]
        filtro = f"o.id IN ({','.join('?' * len(ids))})"
        params = ids
    else:
        filtro = "p.run_id=? AND o.resultado=?"
        params = [args.run, args.resultado]

    filas = c.execute(f"""
        SELECT o.id, p.activo, p.direccion, p.timeframe, p.ts_vela, o.precio_entrada,
               o.precio_salida, o.resultado, o.ganancia, p.ventana_ohlc, o.vela_resultado, p.confianza,
               p.indicadores
        FROM operaciones o JOIN predicciones p ON p.id = o.prediccion_id
        WHERE {filtro} ORDER BY o.id DESC LIMIT ?
    """, params + [args.n]).fetchall()

    if not filas:
        print("no se encontraron operaciones con esos criterios")
        return

    carpeta = "capturas_live"
    os.makedirs(carpeta, exist_ok=True)
    for op in filas:
        if not op[9]:
            print(f"  op {op[0]}: sin ventana_ohlc guardada, se omite")
            continue
        ruta = graficar(op, carpeta)
        print(f"  op {op[0]} {op[1]} {op[2].upper()} {op[7]} -> {ruta}")


if __name__ == "__main__":
    main()
