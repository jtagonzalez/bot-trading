import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Simulación en imágenes de un ciclo de martingala del setup Gold 4.0 + Bollinger,
vela a vela, con datos reales de velas.db (velas de 1 min, Bollinger 14,2,
línea cian = SMA 50).

Cuadro 0: aparece la vela de la señal (aviso VENDER/COMPRAR, bajo/sobre la
cian, color correcto, mecha tocando la banda) y se abre la operación 1.
Cuadros 1..6: se revela la vela siguiente; si cierra en contra se pierde y se
abre la siguiente operación en esa misma vela, con el monto x2,16.
Último cuadro: resumen del ciclo.

Genera capturas_sim/<par>_<hora>/cuadro_XX.png y simulacion.gif

    py animar_perdida.py                                   # EURUSD 2026-07-23 15:56 UTC
    py animar_perdida.py --activo GBPUSD --hora "2026-07-28 02:45"
"""
import argparse
import os
import sqlite3

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["text.parse_math"] = False   # los "$" son dólares, no fórmulas
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.animation import PillowWriter

from simular_bb_tendencia import variantes
from simular_bb_v2 import preparar as preparar_v2
from simular_bb_media import preparar as preparar_media
from estrategia_gold4_bb import cargar_velas, indicadores as indicadores_ez
from simular_zigzag import detectar_zigzag
from ver_toque_bb import preparar

SIN_GOLD = False
MEDIA = False   # True: la señal toca la línea intermedia (14) en vez del extremo

PAYOUT = 0.86
MULT = (1 + PAYOUT) / PAYOUT
MAX_OPS = 6
FONDO, TEXTO, REJILLA = "#131722", "#d1d4dc", "#2a2e39"
VERDE, ROJA = "#26a69a", "#ef5350"


def dibujar(ax, axs, w, hasta, i_sen, sentido, pasos, pip, par, titulo):
    ax.clear()
    axs.clear()
    for a in (ax, axs):
        a.set_facecolor(FONDO)
        a.tick_params(colors=TEXTO, labelsize=8)
        for s in a.spines.values():
            s.set_color(REJILLA)
    vis = w.iloc[:hasta + 1]
    x = np.arange(len(vis))
    # rango fijo de todo el ciclo, para que la escala no salte entre cuadros
    lo = min(w.low.min(), w.inf.min()) - 3 * pip
    hi = max(w.high.max(), w.sup.max()) + 3 * pip
    ax.set_xlim(-1, len(w) + 1)
    ax.set_ylim(lo, hi)
    ax.fill_between(x, vis.inf, vis.sup, color="#2962ff", alpha=0.06)
    ax.plot(x, vis.sup, color="#f23645", lw=1.3, label="Bollinger superior (14,2)")
    ax.plot(x, vis.inf, color="#f23645", lw=1.3, label="Bollinger inferior")
    if MEDIA:
        ax.plot(x, vis.mid, color="#4caf50", lw=1.8, label="línea intermedia (media 14)")
    else:
        ax.plot(x, vis.mid, color="#787b86", lw=0.9, ls="--", label="media 14")
    ax.plot(x, vis.cian, color="#00e5ff", lw=1.8, label="tendencia: línea cian (SMA 50)")
    for xi, (_, r) in zip(x, vis.iterrows()):
        col = VERDE if r.close >= r.open else ROJA
        ax.vlines(xi, r.low, r.high, color=col, lw=1)
        ax.bar(xi, max(abs(r.close - r.open), pip * 0.04), bottom=min(r.open, r.close),
               color=col, width=0.65)
    # señal
    s = w.iloc[i_sen]
    venta = sentido < 0
    y_flecha = s.high + 2.2 * pip if venta else s.low - 2.2 * pip
    txt = ("VENDER" if venta else "COMPRAR") + (" (sin aviso Gold)" if SIN_GOLD else "")
    ax.annotate(txt, xy=(i_sen, s.high if venta else s.low),
                xytext=(i_sen, y_flecha), ha="center", color="#ffeb3b", fontsize=9,
                fontweight="bold", arrowprops=dict(arrowstyle="->", color="#ffeb3b"))
    banda = s.mid if MEDIA else (s.sup if venta else s.inf)
    ax.plot([i_sen - 1.2, i_sen + 1.2], [banda, banda], color="#ffeb3b", lw=2)
    # operaciones
    for n, p in enumerate(pasos, 1):
        xe = i_sen + n - 1
        col = {"G": "#4caf50", "P": "#f44336", "E": "#9e9e9e", "?": "#ffeb3b"}[p["res"]]
        ax.plot([xe, xe + 1], [p["entrada"], p["entrada"]], color=col, lw=1.2, ls=":")
        ax.scatter([xe], [p["entrada"]], color=col, s=18, zorder=5)
        # del lado contrario al movimiento en contra (ahí hay espacio), escalonadas
        sep = (1.5 + 2.4 * (n % 2 == 0)) * pip
        y = (min(w.iloc[xe + 1].low, p["entrada"]) - sep if venta
             else max(w.iloc[xe + 1].high, p["entrada"]) + sep)
        etiqueta = f"op{n} ${p['monto']:.2f}" + ("" if p["res"] == "?" else f" {p['res']}")
        ax.text(xe + 1, y, etiqueta, ha="center", va="top" if venta else "bottom",
                fontsize=7.5, fontweight="bold", color=col,
                bbox=dict(boxstyle="round,pad=0.15", fc=FONDO, ec="none", alpha=0.85))
    ts = w["dt"].iloc[::5]
    ax.set_xticks(np.arange(len(w))[::5], [t.strftime("%H:%M") for t in ts])
    ax.grid(color=REJILLA, lw=0.6)
    ax.yaxis.tick_right()
    ax.legend(loc="upper left", fontsize=7.5, facecolor=FONDO, edgecolor=REJILLA,
              labelcolor=TEXTO)
    ax.set_title(titulo, color=TEXTO, fontsize=10.5, loc="left")
    # panel de saldo
    axs.set_xlim(0, MAX_OPS + 0.8)
    saldo, arr = 0.0, []
    for p in pasos:
        if p["res"] == "P":
            saldo -= p["monto"]
        elif p["res"] == "G":
            saldo += p["monto"] * PAYOUT
        arr.append(saldo)
    xs = np.arange(1, len(arr) + 1)
    cols = [{"G": "#4caf50", "P": "#f44336", "E": "#9e9e9e", "?": "#ffeb3b"}[p["res"]]
            for p in pasos]
    axs.bar(xs, arr, color=cols, width=0.55)
    axs.axhline(0, color=TEXTO, lw=0.6)
    for xx, yy in zip(xs, arr):
        axs.text(xx, yy, f"${yy:+.2f}", ha="center", va="top" if yy < 0 else "bottom",
                 color=TEXTO, fontsize=8)
    axs.set_xticks(range(1, MAX_OPS + 1), [f"op {k}" for k in range(1, MAX_OPS + 1)])
    axs.set_ylim(-95, 12)
    axs.set_ylabel("saldo del ciclo ($)", color=TEXTO, fontsize=8)
    axs.grid(color=REJILLA, lw=0.6, axis="y")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--activo", default="EURUSD")
    ap.add_argument("--hora", default="2026-07-23 15:56", help="vela de la señal, UTC")
    ap.add_argument("--regla", choices=["original", "T2", "v2", "media", "zigzag"], default="original",
                    help="original = cierre al otro lado de la cian; T2 = vela entera "
                         "al otro lado y la cian en la dirección de la operación; "
                         "v2 = T2 + inclinación mínima en 20 velas + vela previa mismo color; "
                         "media = v2 pero tocando la línea intermedia (14) en vez del extremo; "
                         "zigzag = rebote confirmado entre bandas en mercado lateral")
    ap.add_argument("--min-rebotes", type=int, default=2,
                    help="con --regla zigzag: rebotes alternados mínimos exigidos antes de operar")
    ap.add_argument("--sin-gold", action="store_true", help="no exigir el aviso del Gold 4.0")
    ap.add_argument("--nivel", type=float, default=1.0,
                    help="con --regla v2: nivel de inclinación mínima exigido (0.0 a 2.0)")
    ap.add_argument("--color-previo", action="store_true",
                    help="exige que la vela anterior a la señal sea del mismo color "
                         "(dos verdes seguidas para comprar, dos rojas para vender)")
    ap.add_argument("--pendiente", type=float,
                    help="con --regla T2: la cian debe moverse en 10 velas más de N veces "
                         "el ATR(14) en la dirección de la operación (descarta la cian plana)")
    args = ap.parse_args()
    global SIN_GOLD, MEDIA
    SIN_GOLD = args.sin_gold
    MEDIA = args.regla == "media"
    pip = 0.01 if args.activo.endswith("JPY") else 0.0001
    v = preparar(sqlite3.connect("velas.db"), args.activo)
    if args.regla == "zigzag":
        vz = indicadores_ez(cargar_velas(sqlite3.connect("velas.db"), args.activo))
        compra, venta = detectar_zigzag(vz, args.min_rebotes)
        v["senal"] = np.where(compra, 1, np.where(venta, -1, 0))
    elif args.regla in ("v2", "media"):
        fn = preparar_media if MEDIA else preparar_v2
        compra, venta = fn(v)[(f"inclinación > {args.nivel} ATR/20v",
                              "sin Gold" if args.sin_gold else "con Gold")]
        v["senal"] = np.where(compra, 1, np.where(venta, -1, 0))
    elif args.regla == "T2" or args.sin_gold:
        nombre = "T2 + cian en dirección" if args.regla == "T2" else "T0 original"
        compra, venta = variantes(v)[(nombre, "sin Gold" if args.sin_gold else "con Gold")]
        if args.color_previo:
            verde, roja = v.close > v.open, v.close < v.open
            compra &= verde & verde.shift(1, fill_value=False)
            venta &= roja & roja.shift(1, fill_value=False)
        if args.pendiente is not None:
            tr = pd.concat([v.high - v.low, (v.high - v.close.shift()).abs(),
                            (v.low - v.close.shift()).abs()], axis=1).max(axis=1)
            pend = (v.cian - v.cian.shift(10)) / tr.rolling(14).mean()
            compra, venta = compra & (pend > args.pendiente), venta & (pend < -args.pendiente)
        v["senal"] = np.where(compra, 1, np.where(venta, -1, 0))
    objetivo = pd.Timestamp(args.hora, tz="UTC")
    fila = v.index[v["dt"] == objetivo]
    if not len(fila) or v.senal[fila[0]] == 0:
        raise SystemExit(f"no hay señal del setup en {args.activo} a las {args.hora} UTC")
    i = fila[0]
    sentido = int(v.senal[i])
    ini = i - 35
    w = v.iloc[ini: i + MAX_OPS + 4].reset_index(drop=True)
    i_sen = i - ini

    carpeta = os.path.join("capturas_sim", f"{args.activo}_{objetivo:%Y%m%d_%H%M}"
                           f"{'_zigzag' if args.regla == 'zigzag' else ('_media' if args.regla == 'media' else ('_v2' if args.regla == 'v2' else ('_T2' if args.regla == 'T2' else '')))}"
                           f"{'_colorprev' if args.color_previo else ''}"
                           f"{f'_pend{args.pendiente}' if args.pendiente else ''}"
                           f"{'_singold' if args.sin_gold else ''}")
    os.makedirs(carpeta, exist_ok=True)
    fig = plt.figure(figsize=(13, 8), facecolor=FONDO)
    gs = fig.add_gridspec(2, 1, height_ratios=[3.2, 1], hspace=0.28)
    ax, axs = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    lado = "VENTA (PUT)" if sentido < 0 else "COMPRA (CALL)"
    s = w.iloc[i_sen]
    if MEDIA:
        toque = (s.high - s.mid) if sentido < 0 else (s.mid - s.low)
    else:
        toque = (s.high - s.sup) if sentido < 0 else (s.inf - s.low)
    cuadros = []

    pasos = [{"entrada": s.close, "monto": 1.0, "res": "?"}]
    if args.regla == "zigzag":
        tendencia = (f"ZIGZAG confirmado: >= {args.min_rebotes} rebotes alternados entre las "
                     f"bandas antes de esta señal (mercado lateral)")
    elif MEDIA:
        tendencia = (f"vela ENTERA {'bajo' if sentido < 0 else 'sobre'} la cian · cian "
                     f"{'bajando' if sentido < 0 else 'subiendo'} > {args.nivel} ATR en 20 velas · "
                     f"vela previa mismo color · TOCA LA MEDIA (14), no el extremo")
    elif args.regla == "v2":
        tendencia = (f"vela ENTERA {'bajo' if sentido < 0 else 'sobre'} la cian · cian "
                     f"{'bajando' if sentido < 0 else 'subiendo'} > {args.nivel} ATR en 20 velas · "
                     f"vela previa mismo color")
    elif args.regla == "T2":
        tendencia = (f"vela ENTERA {'bajo' if sentido < 0 else 'sobre'} la cian y cian "
                     f"{'bajando' if sentido < 0 else 'subiendo'}"
                     + (f" fuerte (>{args.pendiente} ATR en 10 velas)" if args.pendiente else ""))
    else:
        tendencia = f"cierre {'bajo' if sentido < 0 else 'sobre'} la cian"
    banda_txt = "toca la media" if MEDIA else "pasa la banda"
    titulo = (f"{args.activo} 1m · {objetivo:%Y-%m-%d %H:%M} UTC · SEÑAL {lado}"
              f"{' sin aviso Gold' if args.sin_gold else ''}\n{tendencia} · vela "
              f"{'roja' if sentido < 0 else 'verde'} · la mecha {banda_txt} "
              f"{toque / pip:.2f} pips · se abre la op 1 de $1,00 a 1 minuto")
    dibujar(ax, axs, w, i_sen, i_sen, sentido, pasos, pip, args.activo, titulo)
    cuadros.append(os.path.join(carpeta, "cuadro_00.png"))
    fig.savefig(cuadros[-1], dpi=100, facecolor=FONDO)

    monto, saldo = 1.0, 0.0
    for n in range(1, MAX_OPS + 1):
        j = i_sen + n
        entrada, cierre = w.close[j - 1], w.close[j]
        r = np.sign(cierre - entrada) * sentido
        res = "G" if r > 0 else ("P" if r < 0 else "E")
        pasos[-1]["res"] = res
        if res == "P":
            saldo -= monto
        elif res == "G":
            saldo += monto * PAYOUT
        mov = (cierre - entrada) / pip
        texto = {"G": "GANA", "P": "PIERDE", "E": "EMPATE (devuelve)"}[res]
        titulo = (f"{args.activo} · op {n} de {MAX_OPS}: ${monto:.2f} · entrada {entrada:.5f} "
                  f"-> cierre {cierre:.5f} ({mov:+.1f} pips) · {texto}\n"
                  f"saldo del ciclo: ${saldo:+.2f}")
        fin = res == "G" or n == MAX_OPS
        if res == "P":
            monto *= MULT
        if not fin:
            pasos.append({"entrada": cierre, "monto": monto, "res": "?"})
            titulo += f" · se abre la op {n + 1} de ${monto:.2f} en la misma dirección"
        dibujar(ax, axs, w, j, i_sen, sentido, pasos, pip, args.activo, titulo)
        cuadros.append(os.path.join(carpeta, f"cuadro_{n:02d}.png"))
        fig.savefig(cuadros[-1], dpi=100, facecolor=FONDO)
        if fin:
            break

    # resumen
    arriesgado = sum(p["monto"] for p in pasos)
    titulo = (f"RESUMEN · {args.activo} {objetivo:%Y-%m-%d %H:%M} UTC · {lado}\n"
              f"{len(pasos)} operaciones: {' '.join(p['res'] for p in pasos)} · "
              f"total apostado ${arriesgado:.2f} · resultado del ciclo ${saldo:+.2f} "
              f"(= {abs(saldo) / 0.86:.0f} ciclos ganados de $0,86)" if saldo < 0 else
              f"RESUMEN · resultado del ciclo ${saldo:+.2f}")
    dibujar(ax, axs, w, len(w) - 1, i_sen, sentido, pasos, pip, args.activo, titulo)
    cuadros.append(os.path.join(carpeta, "cuadro_99_resumen.png"))
    fig.savefig(cuadros[-1], dpi=100, facecolor=FONDO)
    plt.close(fig)

    from PIL import Image
    imgs = [Image.open(c).convert("P", palette=Image.ADAPTIVE) for c in cuadros]
    dur = [3000] + [1800] * (len(imgs) - 2) + [5000]
    imgs[0].save(os.path.join(carpeta, "simulacion.gif"), save_all=True,
                 append_images=imgs[1:], duration=dur, loop=0)
    print(f"{len(cuadros)} cuadros + simulacion.gif en {carpeta}/  · resultado ${saldo:+.2f}")


if __name__ == "__main__":
    main()
