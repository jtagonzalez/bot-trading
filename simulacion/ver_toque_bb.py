import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Ver el solape de la vela con las Bandas de Bollinger (14,2) en las señales del
setup Gold 4.0 + Bollinger, a 1 minuto.

Genera:
  * toque_bb.csv  una fila por señal (setup completo) con los números del solape:
      - banda: valor de la banda tocada (superior en venta, inferior en compra)
      - mecha_pips: tamaño de la mecha del lado de la banda
      - pasa_pips: cuánto pasó la mecha más allá de la banda (0 = apenas la roza)
      - pct_mecha_fuera: qué parte de esa mecha quedó fuera de la banda
      - cuerpo_fuera: si el cuerpo (apertura/cierre) también quedó fuera
      - resultado de la martingala continua (G ganada, P perdida, E empate por vela)
  * capturas_bb/*.png  un gráfico por señal elegida: velas, bandas, línea cian,
    la vela de la señal marcada y las velas de la martingala con su resultado

    py ver_toque_bb.py                          # todos los ciclos perdidos + 6 ganados
    py ver_toque_bb.py --activo EURUSD --desde 2026-07-23 --hasta 2026-07-24
"""
import argparse
import os
import sqlite3

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from simular_bb_martingala import FOREX
from simular_gold4 import senales

MAX_OPS = 6


def preparar(c, a):
    v = pd.read_sql("SELECT ts, open, high, low, close FROM velas WHERE activo=? "
                    "AND timeframe=60 ORDER BY ts", c, params=(a,))
    v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
    _, _, up_b, dn_b = senales(v)
    v["cian"] = v.close.rolling(50).mean()
    v["mid"] = v.close.rolling(14).mean()
    sd = v.close.rolling(14).std(ddof=0)
    v["sup"], v["inf"] = v.mid + 2 * sd, v.mid - 2 * sd
    venta = (v.close < v.cian) & (v.close < v.open) & (v.high >= v.sup) & dn_b
    compra = (v.close > v.cian) & (v.close > v.open) & (v.low <= v.inf) & up_b
    v["senal"] = np.where(compra, 1, np.where(venta, -1, 0))
    v["operable"] = v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)
    return v


def ciclos(v, pip):
    ts, cl = v.ts.values, v.close.values
    s, op = v.senal.values, v.operable.values
    filas, i, n = [], 0, len(v)
    while i < n - 1:
        if s[i] == 0 or not op[i]:
            i += 1
            continue
        sg, j, res = s[i], i, []
        while len(res) < MAX_OPS and j + 1 < n and ts[j + 1] - ts[j] == 60 and op[j]:
            r = int(np.sign(cl[j + 1] - cl[j]) * sg)
            res.append({1: "G", -1: "P", 0: "E"}[r])
            j += 1
            if r > 0:
                break
        if res:
            f = v.iloc[i]
            if sg < 0:
                banda, extremo, cuerpo = f.sup, f.high, max(f.open, f.close)
                mecha, pasa = extremo - cuerpo, extremo - banda
                cuerpo_fuera = cuerpo > banda
            else:
                banda, extremo, cuerpo = f.inf, f.low, min(f.open, f.close)
                mecha, pasa = cuerpo - extremo, banda - extremo
                cuerpo_fuera = cuerpo < banda
            filas.append({
                "i": i, "hora_utc": f["dt"].strftime("%Y-%m-%d %H:%M"),
                "sentido": "VENTA" if sg < 0 else "COMPRA",
                "banda": round(banda, 6), "extremo": extremo,
                "mecha_pips": round(mecha / pip, 2), "pasa_pips": round(pasa / pip, 2),
                "pct_mecha_fuera": round(100 * min(pasa / mecha, 1), 1) if mecha > 0 else 100.0,
                "cuerpo_fuera": bool(cuerpo_fuera),
                "resultado": "".join(res), "gano": "G" in res})
        i = max(j, i + 1)
    return filas


def grafico(v, fila, a, pip, carpeta):
    i, k = fila["i"], len(fila["resultado"])
    w = v.iloc[max(i - 40, 0): i + k + 6]
    x = np.arange(len(w))
    ix = i - w.index[0]
    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.plot(x, w.sup, color="#d62728", lw=1.4, label="Bollinger superior (14,2)")
    ax.plot(x, w.inf, color="#2ca02c", lw=1.4, label="Bollinger inferior")
    ax.plot(x, w.mid, color="#999999", lw=1, ls="--", label="media 14")
    ax.plot(x, w.cian, color="#17becf", lw=1.8, label="línea cian (SMA 50)")
    ax.fill_between(x, w.inf, w.sup, color="#cccccc", alpha=0.15)
    for xi, (_, r) in zip(x, w.iterrows()):
        col = "#26a69a" if r.close >= r.open else "#ef5350"
        ax.vlines(xi, r.low, r.high, color=col, lw=1)
        ax.bar(xi, abs(r.close - r.open) or pip * 0.05, bottom=min(r.open, r.close),
               color=col, width=0.6)
    venta = fila["sentido"] == "VENTA"
    fs = w.iloc[ix]
    ax.annotate(f"SEÑAL {fila['sentido']}\nmecha {fila['mecha_pips']} pips\n"
                f"pasa la banda {fila['pasa_pips']} pips ({fila['pct_mecha_fuera']}% de la mecha)",
                xy=(ix, fs.high if venta else fs.low),
                xytext=(ix - 14, fs.high + 6 * pip if venta else fs.low - 6 * pip),
                arrowprops=dict(arrowstyle="->", color="black"), fontsize=9,
                bbox=dict(boxstyle="round", fc="#fff8dc", ec="#999"))
    ax.axhline(fila["banda"], xmin=0, xmax=1, color="#d62728" if venta else "#2ca02c",
               lw=0.6, ls=":")
    for n, r in enumerate(fila["resultado"], 1):
        fx = ix + n
        fr = w.iloc[fx]
        col = {"G": "#1b7f1b", "P": "#c62828", "E": "#777"}[r]
        y = fr.high + 1.5 * pip if venta else fr.low - 1.5 * pip
        ax.text(fx, y, f"{n}{r}", ha="center", va="bottom" if venta else "top",
                fontsize=9, fontweight="bold", color=col)
    estado = "GANADO" if fila["gano"] else "PERDIDO"
    ax.set_title(f"{a} · {fila['hora_utc']} UTC · {fila['sentido']} · martingala "
                 f"{fila['resultado']} ({estado})   [1G = ganó en el intento 1, P = perdió, E = empate]",
                 fontsize=10)
    ticks = x[::5]
    ax.set_xticks(ticks, [w["dt"].iloc[t].strftime("%H:%M") for t in ticks], fontsize=8)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    nombre = f"{a}_{fila['hora_utc'].replace(':', '').replace(' ', '_')}_{estado}.png"
    fig.savefig(os.path.join(carpeta, nombre), dpi=110)
    plt.close(fig)
    return nombre


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--activo", help="un solo par, p.ej. EURUSD")
    ap.add_argument("--desde", help="fecha UTC AAAA-MM-DD")
    ap.add_argument("--hasta", help="fecha UTC AAAA-MM-DD (incluida)")
    args = ap.parse_args()
    c = sqlite3.connect("velas.db")
    carpeta = "capturas_bb"
    os.makedirs(carpeta, exist_ok=True)
    todas, graficos = [], []
    for a in ([args.activo] if args.activo else FOREX):
        pip = 0.01 if a.endswith("JPY") else 0.0001
        v = preparar(c, a)
        filas = ciclos(v, pip)
        for f in filas:
            f["activo"] = a
        todas += filas
        if args.desde or args.hasta:
            elegidas = [f for f in filas
                        if (not args.desde or f["hora_utc"][:10] >= args.desde)
                        and (not args.hasta or f["hora_utc"][:10] <= args.hasta)]
        else:
            elegidas = [f for f in filas if not f["gano"]]
        graficos += [grafico(v, f, a, pip, carpeta) for f in elegidas]
    if not (args.desde or args.hasta or args.activo):
        # unos ganados para comparar
        rng = np.random.default_rng(1)
        ganados = [f for f in todas if f["gano"]]
        for f in rng.choice(ganados, 6, replace=False):
            a = f["activo"]
            pip = 0.01 if a.endswith("JPY") else 0.0001
            graficos.append(grafico(preparar(c, a), f, a, pip, carpeta))

    df = pd.DataFrame(todas).drop(columns="i")
    cols = ["activo", "hora_utc", "sentido", "banda", "extremo", "mecha_pips",
            "pasa_pips", "pct_mecha_fuera", "cuerpo_fuera", "resultado", "gano"]
    df[cols].to_csv("toque_bb.csv", index=False)
    print(f"{len(df)} señales -> toque_bb.csv · {len(graficos)} gráficos -> {carpeta}/")

    # ¿el tamaño del solape distingue ganados de perdidos?
    df["primera"] = df.resultado.str[0]
    d = df[df.primera != "E"]
    print("\nAcierto de la PRIMERA operación según cuánto pasó la mecha la banda:")
    d = d.assign(grupo=pd.qcut(d.pasa_pips.rank(method="first"), 4,
                               labels=["roza (25% menor)", "poco", "bastante", "mucho (25% mayor)"]))
    for g, x in d.groupby("grupo", observed=True):
        print(f"  {g:<20} pasa {x.pasa_pips.min():5.2f}-{x.pasa_pips.max():5.2f} pips  "
              f"n={len(x):>4}  acierto {100 * (x.primera == 'G').mean():5.1f}%   "
              f"ciclos perdidos {int((~x.gano).sum())}")
    for cf, x in d.groupby("cuerpo_fuera"):
        print(f"  cuerpo {'FUERA' if cf else 'dentro'} de la banda{'':<5}n={len(x):>4}  "
              f"acierto {100 * (x.primera == 'G').mean():5.1f}%")


if __name__ == "__main__":
    main()
