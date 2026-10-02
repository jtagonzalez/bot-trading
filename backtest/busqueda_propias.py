import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Prueba las estrategias buscadas en internet (estrategias_externas.py) contra
el historial real, con el mismo embudo estadístico que busqueda_exhaustiva.py:
BH (q=0.10), control aleatorio, y estabilidad en las 2 mitades del historial.

Primero global (todo el horario operable, sin cortar por hora) para ver si
alguna estrategia "tal cual viene de internet" ya funciona. Si nada
sobrevive ahí, se repite cruzando por hora del día, igual que antes.

    py busqueda_externas.py
"""
import sqlite3

import numpy as np
import pandas as pd
from scipy.stats import binomtest

from estrategia_gold4_bb import BE, CRIPTO, FOREX, cargar_velas, indicadores
from estrategias_propias import ESTRATEGIAS_PROPIAS as ESTRATEGIAS

Q_BH = 0.10
N_MIN = 30
rng = np.random.default_rng(23)


def resultado_1op(v):
    ts, cl = v.ts.values, v.close.values
    return np.r_[np.sign(cl[1:] - cl[:-1]), 0], np.r_[ts[1:] - ts[:-1] == 60, False]


def bh(pvals, q):
    ps = np.sort(np.asarray(pvals))
    m = len(ps)
    ok = ps <= (np.arange(1, m + 1) / m) * q
    return ps[ok].max() if ok.any() else None


def evaluar_celda(r, senal, m):
    x = r[m] * senal[m]
    x = x[x != 0]
    n = len(x)
    if n < N_MIN:
        return None
    k = int((x > 0).sum())
    p = binomtest(k, n, BE / 100, alternative="greater").pvalue
    return n, k, 100 * k / n, p


def main():
    c = sqlite3.connect("velas.db")
    activos = [(a, False) for a in FOREX] + [(a, True) for a in CRIPTO]
    datos = {}
    for a, cripto in activos:
        v = indicadores(cargar_velas(c, a, operable_forex=not cripto))
        r, cont = resultado_1op(v)
        datos[a] = (v, r, cont & v.operable.values)

    estrategias = dict(ESTRATEGIAS)
    # 9 controles aleatorios independientes (mismo numero que estrategias
    # reales) para que la comparacion de "mejor p-valor" sea pareja: si se
    # comparan 1956 celdas reales contra solo 218 aleatorias, el minimo de
    # las 1956 sale mas chico solo por tener mas intentos, no por ser mejor.
    def hacer_aleatoria(seed):
        r = np.random.default_rng(seed)
        return lambda v: (pd.Series(r.random(len(v)) < 0.03, index=v.index),
                          pd.Series(r.random(len(v)) < 0.03, index=v.index))
    for j in range(len(ESTRATEGIAS)):
        estrategias[f"ALEATORIO_{j}"] = hacer_aleatoria(100 + j)

    # ---- 1) GLOBAL: toda la sesion operable junta, sin cortar por hora ----
    print("=" * 100)
    print("PASO 1 · cada estrategia completa, TODO el horario operable junto (por activo)")
    print("=" * 100)
    filas_global = []
    for nombre, fn in estrategias.items():
        g_tot = p_tot = 0
        for a, (v, r, ok) in datos.items():
            compra, venta = fn(v)
            senal = np.where(compra.values, 1, np.where(venta.values, -1, 0))
            m = ok & (senal != 0)
            x = r[m] * senal[m]
            x = x[x != 0]
            g_tot += int((x > 0).sum()); p_tot += int((x < 0).sum())
        n = g_tot + p_tot
        if n < N_MIN:
            print(f"  {nombre:<26}  solo {n} señales")
            continue
        p = binomtest(g_tot, n, BE / 100, alternative="greater").pvalue
        filas_global.append((nombre, n, 100 * g_tot / n, p))
    filas_global.sort(key=lambda x: x[3])
    for nombre, n, acc, p in filas_global:
        print(f"  {nombre:<26}  n={n:>7,}  acierto={acc:6.2f}%  p={p:.4f}")
    umbral = bh([x[3] for x in filas_global], Q_BH)
    print(f"\n  umbral BH (q={Q_BH}): {umbral if umbral else 'nada sobrevive'}")

    # ---- 2) por hora del dia, igual que busqueda_exhaustiva.py ----
    print("\n" + "=" * 100)
    print("PASO 2 · cruzado por activo x hora del día (igual rigor que busqueda_exhaustiva.py)")
    print("=" * 100)
    filas = []
    for a, (v, r, ok) in datos.items():
        cripto = a in CRIPTO
        horas = range(24) if cripto else range(1, 18)
        mitad = v.dt.dt.normalize() < v.dt.dt.normalize().median()
        for nombre, fn in estrategias.items():
            compra, venta = fn(v)
            senal = np.where(compra.values, 1, np.where(venta.values, -1, 0))
            for h in horas:
                m = ok & (v.dt.dt.hour.values == h) & (senal != 0)
                res = evaluar_celda(r, senal, m)
                if res is None:
                    continue
                n, k, acc, p = res
                m1, m2 = m & mitad.values, m & (~mitad.values)
                def sub_acc(mm):
                    xx = r[mm] * senal[mm]; xx = xx[xx != 0]
                    return (100 * (xx > 0).sum() / len(xx)) if len(xx) else np.nan, len(xx)
                a1, n1 = sub_acc(m1)
                a2, n2 = sub_acc(m2)
                filas.append(dict(activo=a, estrategia=nombre, hora=h, n=n, acierto=acc,
                                  p=p, acc_mitad1=a1, n1=n1, acc_mitad2=a2, n2=n2))
    df = pd.DataFrame(filas)
    real = df[~df.estrategia.str.startswith("ALEATORIO")]
    azar = df[df.estrategia.str.startswith("ALEATORIO")]
    print(f"  celdas reales: {len(real)}   celdas de control aleatorio: {len(azar)}")
    for nombre, sub in (("REAL", real), ("ALEATORIO", azar)):
        u = bh(sub.p.values, Q_BH)
        sobrevive = sub[sub.p <= u] if u is not None else sub.iloc[0:0]
        print(f"\n  {nombre}: umbral BH = {u if u is not None else 'nada sobrevive'}   "
              f"celdas que sobreviven: {len(sobrevive)}")
        if len(sobrevive):
            estables = sobrevive[(sobrevive.acc_mitad1 > BE) & (sobrevive.acc_mitad2 > BE)]
            cols = ["activo", "estrategia", "hora", "n", "acierto", "p", "acc_mitad1", "n1", "acc_mitad2", "n2"]
            print(sobrevive.sort_values("p")[cols].to_string(index=False, float_format=lambda x: f"{x:.2f}"))
            print(f"  -> y ademas estables en las 2 mitades: {len(estables)}")

    print("\n  Top 8 reales sin corregir (para ver que tan cerca llegaron):")
    cols = ["activo", "estrategia", "hora", "n", "acierto", "p", "acc_mitad1", "n1", "acc_mitad2", "n2"]
    print(real.sort_values("p")[cols].head(8).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("\n  Top 3 del control aleatorio (para comparar):")
    print(azar.sort_values("p")[cols].head(3).to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    df.to_pickle("busqueda_propias.pkl")


if __name__ == "__main__":
    main()
