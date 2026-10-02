import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Variante nueva que pidió el usuario: en vez de tocar los EXTREMOS de las
Bandas de Bollinger (banda superior/inferior), tocar la LÍNEA INTERMEDIA
(la media de 14, el "verde" de la captura) — la entrada clásica de "rebote
a la media" dentro de una tendencia, en vez de "rebote en el extremo".

VENTA: en tendencia bajista (vela entera bajo la cian, cian bajando más de
un mínimo en 20 velas, vela anterior también roja), el precio SUBE a tocar
la línea intermedia (high >= media) y CIERRA de nuevo por debajo de ella
(rechazo) — la vela es roja. Se opera esperando que siga la tendencia.

COMPRA: el espejo — tendencia alcista, el precio baja a tocar la media desde
arriba y cierra de nuevo por encima (vela verde).

Todo lo demás igual: velas y operación de 1 min, martingala continua de 6
pasos, pago 86%, aviso opcional del Gold 4.0.

    py simular_bb_media.py                 # tabla resumen (como simular_bb_v2.py)
    py simular_bb_media.py --por-par        # detalle por par, con y sin martingala
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd

from simular_bb_martingala import CRIPTO, FOREX, ciclos_de
from simular_bb_tendencia import BE, PAYOUT, wilson
from simular_gold4 import senales

VENTANA = 20
NIVELES = [0.0, 0.5, 1.0, 1.5, 2.0]


def preparar(v):
    _, _, up_b, dn_b = senales(v)
    c, o, h, l = v.close, v.open, v.high, v.low
    cian = c.rolling(50).mean()
    mid = c.rolling(14).mean()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    pend = (cian - cian.shift(VENTANA)) / atr

    verde, roja = c > o, c < o
    vela_entera_arriba, vela_entera_abajo = l > cian, h < cian
    color_prev_ok_compra = verde & verde.shift(1, fill_value=False)
    color_prev_ok_venta = roja & roja.shift(1, fill_value=False)
    # toca la media y cierra de nuevo del lado de la tendencia (rechazo)
    toca_media_compra = (l <= mid) & (c > mid)
    toca_media_venta = (h >= mid) & (c < mid)

    base_compra = verde & toca_media_compra & vela_entera_arriba & color_prev_ok_compra
    base_venta = roja & toca_media_venta & vela_entera_abajo & color_prev_ok_venta

    out = {}
    for u in NIVELES:
        compra = base_compra & (pend > u)
        venta = base_venta & (pend < -u)
        out[(f"inclinación > {u} ATR/20v", "con Gold")] = (compra & up_b, venta & dn_b)
        out[(f"inclinación > {u} ATR/20v", "sin Gold")] = (compra, venta)
    return out


def cargar(c, activos, cripto):
    datos = []
    for a in activos:
        v = pd.read_sql("SELECT ts, open, high, low, close FROM velas WHERE activo=? "
                        "AND timeframe=60 ORDER BY ts", c, params=(a,))
        v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
        op = (np.ones(len(v), bool) if cripto else
              (v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)).values)
        datos.append((v, preparar(v), op))
    return datos


def tabla_niveles():
    c = sqlite3.connect("velas.db")
    for grupo, activos, cripto in (("FOREX (10 pares, 01-17h UTC)", FOREX, False),
                                   ("CRIPTO (BTC, ETH, 24x7)", CRIPTO, True)):
        datos = cargar(c, activos, cripto)
        dias = len({d for v, _, op in datos for d in v.dt.dt.normalize()[op].unique()})
        print("=" * 118)
        print(f"{grupo} · {dias} días · TOCA LA MEDIA (14) en vez del extremo · cian en dirección "
              f"+ inclinación mínima en {VENTANA} velas + vela previa mismo color · BE {BE:.2f}%")
        print("=" * 118)
        print(f"  {'exigencia de la inclinación':<26}{'Gold':<10}{'señales/día':>12}"
              f"{'acierto 1ª op':>15}{'IC95':>16}   {'martingala 6 ops:':<18}{'perdidos':>9}{'$/día':>9}")
        for clave in datos[0][1]:
            g = p = 0
            cs = []
            for v, var, op in datos:
                compra, venta = var[clave]
                ts, cl = v.ts.values, v.close.values
                ok = np.r_[ts[1:] - ts[:-1] == 60, False] & op
                r = np.sign(np.r_[cl[1:] - cl[:-1], 0])
                for m, sg in ((compra.values & ok, 1), (venta.values & ok, -1)):
                    x = r[m] * sg
                    g += int((x > 0).sum()); p += int((x < 0).sum())
                cs += ciclos_de(v, compra, venta, op, 6)
            n = g + p
            if n < 20:
                print(f"  {clave[0]:<26}{clave[1]:<10}   solo {n} señales")
                continue
            lo, hi = wilson(g, n)
            netos = np.array([x[1] for x in cs])
            perd = sum(1 for x in cs if not x[3])
            marca = "  <-- supera BE" if lo > BE else ""
            print(f"  {clave[0]:<26}{clave[1]:<10}{n / dias:>12.1f}{100 * g / n:>14.2f}%"
                  f"  [{lo:5.2f},{hi:5.2f}]   {len(cs):>8} ciclos{perd:>9}{netos.sum() / dias:>+9.2f}{marca}")
        print()


def tabla_por_par(nivel=1.0):
    c = sqlite3.connect("velas.db")
    for gold in ("sin Gold", "con Gold"):
        clave = (f"inclinación > {nivel} ATR/20v", gold)
        print(f"\n=== TOCA LA MEDIA · {clave[0]} · {gold} · por par (43 días, sesión 01-17h UTC) ===")
        print(f"{'par':<9}{'señales':>8}{'ganadas':>9}{'perdidas':>9}{'empates':>9}{'%acierto':>10}"
              f"{'$/día sin mart.':>17}   {'ciclos':>7}{'gana ciclo':>11}{'pierde ciclo':>13}{'$/día mart.':>12}")
        tot = dict(g=0, p=0, e=0, cg=0, cp=0, neto=0.0, n=0, flat=0.0)
        for a in FOREX:
            v = pd.read_sql("SELECT ts,open,high,low,close FROM velas WHERE activo=? "
                            "AND timeframe=60 ORDER BY ts", c, params=(a,))
            v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
            op = (v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)).values
            compra, venta = preparar(v)[clave]
            ts, cl = v.ts.values, v.close.values
            ok = np.r_[ts[1:] - ts[:-1] == 60, False] & op
            r = np.sign(np.r_[cl[1:] - cl[:-1], 0])
            g = p = e = 0
            for m, sg in ((compra.values & ok, 1), (venta.values & ok, -1)):
                x = r[m] * sg
                g += int((x > 0).sum()); p += int((x < 0).sum()); e += int((x == 0).sum())
            flat = g * PAYOUT - p                      # $1 fijo, sin martingala
            cs = ciclos_de(v, compra, venta, op, 6)
            cg = sum(1 for x in cs if x[3]); cp = len(cs) - cg
            neto = sum(x[1] for x in cs)
            n = g + p
            acierto = f"{100 * g / n:.1f}%" if n else "-"
            print(f"{a:<9}{g + p + e:>8}{g:>9}{p:>9}{e:>9}{acierto:>10}{flat / 43:>+17.2f}   "
                  f"{len(cs):>7}{cg:>11}{cp:>13}{neto / 43:>+12.2f}")
            for k, v2 in zip(("g", "p", "e", "cg", "cp"), (g, p, e, cg, cp)):
                tot[k] += v2
            tot["neto"] += neto; tot["n"] += n; tot["flat"] += flat
        print("-" * 108)
        ac = f"{100 * tot['g'] / tot['n']:.1f}%" if tot["n"] else "-"
        print(f"{'TOTAL':<9}{tot['n'] + tot['e']:>8}{tot['g']:>9}{tot['p']:>9}{tot['e']:>9}{ac:>10}"
              f"{tot['flat'] / 43:>+17.2f}   {tot['cg'] + tot['cp']:>7}{tot['cg']:>11}{tot['cp']:>13}"
              f"{tot['neto'] / 43:>+12.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--por-par", action="store_true")
    ap.add_argument("--nivel", type=float, default=1.0)
    args = ap.parse_args()
    if args.por_par:
        tabla_por_par(args.nivel)
    else:
        tabla_niveles()
