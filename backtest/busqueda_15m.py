import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Repite TODA la batería de señales construidas hasta ahora (Gold4+Bollinger
en sus 4 formas, las 9 de internet, las 9 propias) pero en velas de 15
MINUTOS en vez de 1 — con 2,5 años de historia (abril 2024 a hoy) en vez de
51 días. Mismo motivo que encontró el único candidato real del proyecto
(EURUSD hora 07 UTC, 15m): con más datos, un efecto chico pero real se
puede confirmar; con los 51 días de 1 minuto, ni siquiera se podría ver.

Mismo embudo de siempre: BH (q=0.10) + control aleatorio del mismo tamaño +
estabilidad en las 2 mitades del historial.

    py busqueda_15m.py
"""
import sqlite3

import numpy as np
import pandas as pd
from scipy.stats import binomtest

from estrategia_gold4_bb import (BE, FOREX, cargar_velas, indicadores,
                                 senal_extremo, senal_media, senal_3velas,
                                 senal_zigzag)
from estrategias_externas import ESTRATEGIAS
from estrategias_propias import ESTRATEGIAS_PROPIAS

TF = 900          # 15 minutos
Q_BH = 0.10
N_MIN = 30
rng = np.random.default_rng(31)


def todas_las_senales():
    d = dict(ESTRATEGIAS)
    d.update(ESTRATEGIAS_PROPIAS)
    d["extremo con Gold"] = lambda v: senal_extremo(v, 1.0, con_gold=True)
    d["extremo sin Gold"] = lambda v: senal_extremo(v, 1.0, con_gold=False)
    d["media con Gold"] = lambda v: senal_media(v, 1.0, con_gold=True)
    d["media sin Gold"] = lambda v: senal_media(v, 1.0, con_gold=False)
    d["3velas literal"] = lambda v: senal_3velas(v, con_cian=False)
    d["zigzag min2"] = lambda v: senal_zigzag(v, 2, None)
    return d


def resultado_1op(v, tf):
    ts, cl = v.ts.values, v.close.values
    return np.r_[np.sign(cl[1:] - cl[:-1]), 0], np.r_[ts[1:] - ts[:-1] == tf, False]


def bh(pvals, q):
    ps = np.sort(np.asarray(pvals))
    m = len(ps)
    ok = ps <= (np.arange(1, m + 1) / m) * q
    return ps[ok].max() if ok.any() else None


def main():
    c = sqlite3.connect("velas.db")
    datos = {}
    for a in FOREX:
        v = indicadores(cargar_velas(c, a, TF, operable_forex=True))
        r, cont = resultado_1op(v, TF)
        datos[a] = (v, r, cont & v.operable.values)
    dias = len({d for a, (v, r, ok) in datos.items() for d in v.dt.dt.normalize()[ok].unique()})
    print(f"Datos: {len(datos)} pares forex, 15m, {dias} días operables (01-17h UTC lun-vie)\n")

    estrategias = todas_las_senales()
    for j in range(len(estrategias)):
        r_ = np.random.default_rng(300 + j)
        estrategias[f"ALEATORIO_{j}"] = lambda v, r_=r_: (
            pd.Series(r_.random(len(v)) < 0.05, index=v.index),
            pd.Series(r_.random(len(v)) < 0.05, index=v.index))

    # PASO 1: cada señal completa, sin cortar por hora
    print("=" * 90)
    print("PASO 1 · cada señal completa, todo el horario operable (10 pares juntos)")
    print("=" * 90)
    global_rows = []
    for nombre, fn in estrategias.items():
        g = p = 0
        for a, (v, r, ok) in datos.items():
            compra, venta = fn(v)
            senal = np.where(compra.values, 1, np.where(venta.values, -1, 0))
            m = ok & (senal != 0)
            x = r[m] * senal[m]; x = x[x != 0]
            g += int((x > 0).sum()); p += int((x < 0).sum())
        n = g + p
        if n < N_MIN:
            continue
        pv = binomtest(g, n, BE / 100, alternative="greater").pvalue
        global_rows.append((nombre, n, 100 * g / n, pv))
    global_rows.sort(key=lambda x: x[3])
    for nombre, n, acc, pv in global_rows:
        marca = "  <-- ALEATORIO" if nombre.startswith("ALEATORIO") else ""
        print(f"  {nombre:<26}n={n:>7,}  acierto={acc:6.2f}%  p={pv:.4f}{marca}")
    reales = [x for x in global_rows if not x[0].startswith("ALEATORIO")]
    u = bh([x[3] for x in reales], Q_BH)
    print(f"\n  umbral BH sobre las {len(reales)} reales: {u if u else 'nada sobrevive'}")

    # PASO 2: cruzado por hora
    print("\n" + "=" * 90)
    print("PASO 2 · cruzado por par x hora (igual rigor de siempre)")
    print("=" * 90)
    filas = []
    for a, (v, r, ok) in datos.items():
        mitad = v.dt.dt.normalize() < v.dt.dt.normalize().median()
        for nombre, fn in estrategias.items():
            compra, venta = fn(v)
            senal = np.where(compra.values, 1, np.where(venta.values, -1, 0))
            for h in range(1, 18):
                m = ok & (v.dt.dt.hour.values == h) & (senal != 0)
                x = r[m] * senal[m]; x = x[x != 0]
                n = len(x)
                if n < N_MIN:
                    continue
                k = int((x > 0).sum())
                pv = binomtest(k, n, BE / 100, alternative="greater").pvalue
                m1, m2 = m & mitad.values, m & (~mitad.values)
                def sub(mm):
                    xx = r[mm] * senal[mm]; xx = xx[xx != 0]
                    return (100 * (xx > 0).sum() / len(xx)) if len(xx) else np.nan, len(xx)
                a1, n1 = sub(m1); a2, n2 = sub(m2)
                filas.append(dict(activo=a, estrategia=nombre, hora=h, n=n, acierto=100 * k / n,
                                  p=pv, acc_mitad1=a1, n1=n1, acc_mitad2=a2, n2=n2))
    df = pd.DataFrame(filas)
    real = df[~df.estrategia.str.startswith("ALEATORIO")]
    azar = df[df.estrategia.str.startswith("ALEATORIO")]
    print(f"  celdas reales: {len(real)}   celdas aleatorias: {len(azar)}")
    for nombre, sub in (("REAL", real), ("ALEATORIO", azar)):
        u = bh(sub.p.values, Q_BH)
        sobrevive = sub[sub.p <= u] if u is not None else sub.iloc[0:0]
        print(f"\n  {nombre}: umbral BH = {u if u is not None else 'nada sobrevive'}   "
              f"sobreviven: {len(sobrevive)}")
        if len(sobrevive):
            estables = sobrevive[(sobrevive.acc_mitad1 > BE) & (sobrevive.acc_mitad2 > BE)]
            cols = ["activo", "estrategia", "hora", "n", "acierto", "p", "acc_mitad1", "n1", "acc_mitad2", "n2"]
            print(sobrevive.sort_values("p")[cols].to_string(index=False, float_format=lambda x: f"{x:.2f}"))
            print(f"  y ademas estables en las 2 mitades: {len(estables)}")
    print("\n  Top 10 reales sin corregir:")
    cols = ["activo", "estrategia", "hora", "n", "acierto", "p", "acc_mitad1", "n1", "acc_mitad2", "n2"]
    print(real.sort_values("p")[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("\n  Top 5 aleatorias:")
    print(azar.sort_values("p")[cols].head(5).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    df.to_pickle("busqueda_15m.pkl")


if __name__ == "__main__":
    main()
