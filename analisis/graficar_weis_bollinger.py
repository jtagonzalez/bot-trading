import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Genera una imagen de ejemplo (ganada o perdida) de la estrategia Weis Wave
Volume acumulativo + Bollinger(14,2) descartada en backtest_weis_bollinger.py,
mostrando los dos indicadores en posicion tal como los definio esa regla:
panel de precio con Bollinger(14,2) arriba, histograma WWV acumulativo abajo.

Reutiliza directamente detectar_senales/resultados_de/cargar de
backtest_weis_bollinger.py para que el ejemplo sea exactamente fiel al
backtest (misma logica, mismos parametros por defecto).

    py graficar_weis_bollinger.py --activo BTCUSD --resultado loss
"""
import argparse
import os

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["text.parse_math"] = False
import matplotlib.pyplot as plt
import numpy as np

from backtest_weis_bollinger import (ATR_MULT_DEF, BB_PERIOD, BB_STD, BE,
                                      PAYOUT, RACHA_MIN_DEF, cargar,
                                      detectar_senales, resultados_de)

FONDO, TEXTO, REJILLA = "#131722", "#d1d4dc", "#2a2e39"
VERDE, ROJA = "#26a69a", "#ef5350"
VERDE_OSC, ROJA_OSC = "#1b5e20", "#4a1414"


def calcular_wwv_serie(df, atr_mult=ATR_MULT_DEF, racha_min=RACHA_MIN_DEF):
    """Repite la logica de detectar_senales pero guardando, vela a vela, el
    color de la onda en curso y su acumulado -- lo que hace falta para
    DIBUJAR el histograma (detectar_senales solo devuelve los eventos)."""
    c = df.close.values.astype(float)
    n = len(df)
    tr_close = np.empty(n)
    tr_close[0] = np.nan
    tr_close[1:] = np.abs(np.diff(c))
    import pandas as pd
    atr = pd.Series(tr_close).ewm(alpha=1 / 14, adjust=False).mean().values
    brick_size = atr_mult * atr
    v = df.volume.values.astype(float)

    color_serie = np.zeros(n, dtype=int)
    vol_serie = np.zeros(n, dtype=float)

    extremo = c[0]
    direccion = 0
    onda_inicio = 0
    vol_acum = 0.0
    ultima_onda_color, ultima_onda_vol_total = 0, None
    umbral_serie = np.full(n, np.nan)

    for i in range(1, n):
        b = brick_size[i]
        if np.isnan(b):
            continue
        reversion = False
        inicio_absoluto = False
        if direccion == 0:
            if c[i] - c[onda_inicio] >= b:
                direccion, inicio_absoluto = 1, True
            elif c[onda_inicio] - c[i] >= b:
                direccion, inicio_absoluto = -1, True
            else:
                continue
        elif direccion == 1:
            if c[i] >= extremo:
                extremo = c[i]
            elif extremo - c[i] >= b:
                reversion = True
        else:
            if c[i] <= extremo:
                extremo = c[i]
            elif c[i] - extremo >= b:
                reversion = True

        if reversion:
            dur_onda = i - onda_inicio
            vol_onda_total = vol_acum
            color_onda = direccion
            if dur_onda >= racha_min:
                ultima_onda_color, ultima_onda_vol_total = color_onda, vol_onda_total
            else:
                ultima_onda_color = 0
            direccion = -color_onda
            extremo = c[i]
            onda_inicio = i
            vol_acum = 0.0
        elif inicio_absoluto:
            onda_inicio = i
            vol_acum = 0.0

        vol_acum += v[i]
        color_serie[i] = direccion
        vol_serie[i] = vol_acum
        if ultima_onda_color != 0 and direccion == -ultima_onda_color:
            umbral_serie[i] = ultima_onda_vol_total

    return color_serie, vol_serie, umbral_serie


def graficar_idx(activo, df, idx_senal, direccion, carpeta, color_serie, vol_serie, umbral_serie):

    ANTES, DESPUES = 22, 6
    i0, i1 = max(0, idx_senal - ANTES), min(len(df), idx_senal + DESPUES + 1)
    sub = df.iloc[i0:i1].reset_index(drop=True)
    j_senal = idx_senal - i0
    j_entra = j_senal + 1

    mid = df.close.rolling(BB_PERIOD).mean().values
    std = df.close.rolling(BB_PERIOD).std().values
    sup = (mid + BB_STD * std)[i0:i1]
    inf = (mid - BB_STD * std)[i0:i1]

    o, h, l, c = sub.open.values, sub.high.values, sub.low.values, sub.close.values
    n = len(sub)
    x = np.arange(n)

    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(15, 8.5), facecolor=FONDO,
                                   gridspec_kw={"height_ratios": [2.3, 1]}, sharex=True)
    fig.subplots_adjust(right=0.78, hspace=0.05)
    for a in (ax, ax2):
        a.set_facecolor(FONDO)
        a.tick_params(colors=TEXTO, labelsize=8)
        for s in a.spines.values():
            s.set_color(REJILLA)

    altos, bajos = h, l
    for xi, (oo, hh, ll, cc) in enumerate(zip(o, h, l, c)):
        col = VERDE if cc >= oo else ROJA
        ax.vlines(xi, ll, hh, color=col, lw=1)
        ax.bar(xi, max(abs(cc - oo), (altos.max() - bajos.min()) * 0.002), bottom=min(oo, cc),
               color=col, width=0.65, zorder=3)

    ax.plot(x, sup, color="#f23645", lw=1.3, alpha=0.8)
    ax.plot(x, mid[i0:i1], color="#787b86", lw=0.9, ls="--", alpha=0.8)
    ax.plot(x, inf, color="#f23645", lw=1.3, alpha=0.8)
    xlab = int(n * 0.55)
    ax.text(xlab, sup[xlab], "BB sup (14,2)", color="#f23645", fontsize=7.5,
            va="bottom", ha="center", zorder=8,
            bbox=dict(boxstyle="round,pad=0.1", fc=FONDO, ec="none", alpha=0.85))
    ax.text(xlab, inf[xlab], "BB inf (14,2)", color="#f23645", fontsize=7.5,
            va="top", ha="center", zorder=8,
            bbox=dict(boxstyle="round,pad=0.1", fc=FONDO, ec="none", alpha=0.85))

    rango = altos.max() - bajos.min()
    ymax = max(altos.max(), np.nanmax(sup)) + rango * 0.12
    ymin = min(bajos.min(), np.nanmin(inf)) - rango * 0.08
    ax.set_ylim(ymin, ymax)
    ax.set_xlim(-1, n)

    venta = direccion == -1
    ax.annotate(("VENDER (PUT)" if venta else "COMPRAR (CALL)"),
                xy=(j_senal, l[j_senal] if not venta else h[j_senal]),
                xytext=(j_senal, ymin + (ymax - ymin) * 0.04 if not venta else ymax - (ymax - ymin) * 0.04),
                ha="center", color="#ffeb3b", fontsize=10, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color="#ffeb3b"), zorder=6)

    entrada = c[j_senal]
    salida = c[j_entra] if j_entra < n else c[-1]
    gano = (salida > entrada) if direccion == 1 else (salida < entrada)
    resultado = "win" if gano else "loss"
    col_res = "#4caf50" if gano else "#f44336"
    icono = "GANA" if gano else "PIERDE"
    ax.axhline(entrada, color="#ffeb3b", lw=0.8, ls=":", xmin=(j_senal + 1) / n)
    ax.scatter([j_entra], [salida], color=col_res, s=50, zorder=6)
    mov = salida - entrada
    esquina_y = 0.97 if not gano else 0.08
    ax.text(0.015, esquina_y, f"{icono}\nentrada {entrada:.5f} -> salida {salida:.5f}  ({mov:+.5f})",
            transform=ax.transAxes, color=col_res, fontsize=9.5, fontweight="bold",
            ha="left", va="top" if esquina_y > 0.5 else "bottom",
            bbox=dict(boxstyle="round", fc=FONDO, ec=col_res), zorder=7)

    # ---- panel inferior: Weis Wave Volume acumulativo ----
    colores_barras = [VERDE if color_serie[i0 + k] == 1 else (ROJA if color_serie[i0 + k] == -1 else "#444")
                      for k in range(n)]
    ax2.bar(x, vol_serie[i0:i1], color=colores_barras, width=0.7, zorder=3)
    umb = umbral_serie[i0:i1]
    if np.any(np.isfinite(umb)):
        valor_umbral = umb[np.isfinite(umb)][0]
        ax2.axhline(valor_umbral, color="#ffeb3b", lw=1.1, ls="--", zorder=2)
        ax2.text(n - 1, valor_umbral, " total onda anterior", color="#ffeb3b", fontsize=7.5,
                 va="bottom", ha="right")
    ax2.axvline(j_senal, color="#ffeb3b", lw=0.8, ls=":")
    ax2.set_ylabel("WWV acumulado\n(volumen)", color=TEXTO, fontsize=8)
    ax2.set_facecolor(FONDO)
    ax2.grid(color=REJILLA, lw=0.5, axis="y")

    ticks = list(range(0, n, 3))
    ax2.set_xticks(ticks, [sub.dt.iloc[k].strftime("%H:%M") for k in ticks], rotation=0)
    ax.grid(color=REJILLA, lw=0.6)
    ax.yaxis.tick_right()
    ax2.yaxis.tick_right()

    texto_regla = (
        "REGLA (backtest_weis_bollinger.py):\n"
        f"  racha minima           >= {RACHA_MIN_DEF} velas\n"
        f"  ATR mult (ladrillo)    {ATR_MULT_DEF} x ATR(14)\n"
        "  onda = vol. ACUMULADO\n"
        "  creciente vela a vela,\n"
        "  reinicia en cada giro\n\n"
        "  disparo: la onda nueva\n"
        "  cruza (vela exacta) el\n"
        "  acumulado final de la\n"
        "  onda anterior, Y esa\n"
        "  vela de precio toca la\n"
        "  banda Bollinger(14,2)\n"
        "  del mismo lado\n\n"
        f"  payout turbo 86%\n"
        f"  breakeven {BE:.2f}%\n\n"
        "  resultado del backtest:\n"
        "  n=7.022  WR=47.6%\n"
        "  (por debajo de azar\n"
        "  y de breakeven) ->\n"
        "  estrategia DESCARTADA"
    )
    ax.text(1.015, 0.98, texto_regla, transform=ax.transAxes, color=TEXTO, fontsize=7.6,
            va="top", ha="left", family="monospace",
            bbox=dict(boxstyle="round", fc="#1a1a19", ec=REJILLA))

    titulo = (f"{activo} · EJEMPLO de {icono} real del backtest · vela de 1 min · "
              f"señal {sub.dt.iloc[j_senal]:%Y-%m-%d %H:%M} UTC\n"
              f"Weis Wave Volume (acumulativo, ATR_mult={ATR_MULT_DEF}, racha_min={RACHA_MIN_DEF}) "
              f"+ Bollinger(14,2) · dirección: {'ALZA' if direccion==1 else 'BAJA'}")
    ax.set_title(titulo, color=TEXTO, fontsize=10, loc="left")

    os.makedirs(carpeta, exist_ok=True)
    nombre = f"weis_{activo}_{sub.dt.iloc[j_senal]:%Y%m%d_%H%M}_{resultado}.png"
    ruta = os.path.join(carpeta, nombre)
    fig.savefig(ruta, dpi=110, facecolor=FONDO)
    plt.close(fig)
    return ruta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--activo", default="BTCUSD")
    ap.add_argument("--resultado", default=None, choices=["loss", "win"])
    ap.add_argument("--indice", type=int, default=0)
    ap.add_argument("--ultimas", type=int, default=None,
                     help="en vez de --resultado/--indice, grafica las ultimas N señales (gane o pierda)")
    ap.add_argument("--carpeta", default="capturas_weis")
    args = ap.parse_args()

    df = cargar(args.activo)
    eventos = detectar_senales(df)
    r = resultados_de(df, eventos)
    if r.empty:
        print(f"no hay señales para {args.activo}")
        return
    color_serie, vol_serie, umbral_serie = calcular_wwv_serie(df)

    if args.ultimas:
        filas = r.tail(args.ultimas)
        print(f"ultimas {len(filas)} señales de {args.activo}  (WR={100*filas.gano.mean():.1f}%, "
              f"{int(filas.gano.sum())} de {len(filas)}):")
        for _, f in filas.iterrows():
            ruta = graficar_idx(args.activo, df, int(f["idx"]), int(f["direccion"]), args.carpeta,
                                 color_serie, vol_serie, umbral_serie)
            dirtxt = "CALL" if f["direccion"] == 1 else "PUT"
            res = "GANA" if f["gano"] == 1 else "PIERDE"
            print(f"  {f['dt']} | {dirtxt:4s} | {res:7s} -> {ruta}")
    else:
        resultado_buscado = args.resultado or "loss"
        rf = r[r.gano == (0 if resultado_buscado == "loss" else 1)]
        if rf.empty:
            print(f"no hay ejemplos de '{resultado_buscado}' para {args.activo}")
            return
        fila = rf.iloc[args.indice]
        ruta = graficar_idx(args.activo, df, int(fila["idx"]), int(fila["direccion"]), args.carpeta,
                             color_serie, vol_serie, umbral_serie)
        print(f"-> {ruta}")


if __name__ == "__main__":
    main()
