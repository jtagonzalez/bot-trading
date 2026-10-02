import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Búsqueda exhaustiva de una ventana rentable, usando TODO lo construido en
esta conversación (extremo, media, 3 velas, zigzag) cruzado con cada par y
cada hora del día — cientos de pruebas — pero con la misma disciplina
estadística que ya se usó en este proyecto para no autoengañarse
(validar_pares.py, validar_filtros_historico.py):

  1. Cientos de celdas (setup x par x hora) evaluadas de una.
  2. Corrección de comparaciones múltiples (Benjamini-Hochberg, q=0.10) —
     sin esto, probar cientos de combinaciones garantiza "encontrar" varias
     que superan el breakeven por puro azar (ver optimizar-es-contraproducente.md).
  3. De lo que sobrevive la corrección, se exige que la ventaja se repita
     de forma independiente en la 1a Y la 2a mitad del historial (partición
     temporal) — si solo aparece en una mitad, es ruido, no una ventaja real.
  4. Un control 100% ALEATORIO pasa por el mismo embudo, para comprobar que
     el filtro funciona (si el azar también "sobrevive", algo está mal).

Setups evaluados (8): extremo y media (nivel 1.0, con/sin Gold), 3 velas
(con/sin filtro cian), zigzag (con/sin filtro de cian plana 0.3).
Franja horaria: cada hora UTC dentro del horario operable.
Si algo sobrevive TODO el embudo, se prueba además con martingala continua
de 6 pasos y se muestra una imagen.

    py busqueda_exhaustiva.py
"""
import sqlite3

import numpy as np
import pandas as pd
from scipy.stats import binomtest

from estrategia_gold4_bb import (BE, CRIPTO, FOREX, PAYOUT, cargar_velas,
                                 ciclos_de, indicadores, senal_extremo,
                                 senal_media, senal_3velas, senal_zigzag)

Q_BH = 0.10
N_MIN = 30
rng = np.random.default_rng(7)


def setups():
    return {
        "extremo con Gold":  lambda v: senal_extremo(v, 1.0, con_gold=True),
        "extremo sin Gold":  lambda v: senal_extremo(v, 1.0, con_gold=False),
        "media con Gold":    lambda v: senal_media(v, 1.0, con_gold=True),
        "media sin Gold":    lambda v: senal_media(v, 1.0, con_gold=False),
        "3velas literal":    lambda v: senal_3velas(v, con_cian=False),
        "3velas +cian":      lambda v: senal_3velas(v, con_cian=True),
        "zigzag sin filtro": lambda v: senal_zigzag(v, 2, None),
        "zigzag cian plana": lambda v: senal_zigzag(v, 2, 0.3),
        "ALEATORIO (control)": lambda v: (
            pd.Series(rng.random(len(v)) < 0.02, index=v.index),
            pd.Series(rng.random(len(v)) < 0.02, index=v.index)),
    }


def resultado_1op(v):
    ts, cl = v.ts.values, v.close.values
    return np.r_[np.sign(cl[1:] - cl[:-1]), 0], np.r_[ts[1:] - ts[:-1] == 60, False]


def bh(pvals, q):
    """Benjamini-Hochberg: devuelve el umbral de p-valor que sobrevive, o
    None si nada sobrevive."""
    ps = np.sort(np.asarray(pvals))
    m = len(ps)
    ok = ps <= (np.arange(1, m + 1) / m) * q
    return ps[ok].max() if ok.any() else None


def main():
    c = sqlite3.connect("velas.db")
    filas = []
    for a in FOREX + CRIPTO:
        cripto = a in CRIPTO
        v = indicadores(cargar_velas(c, a, operable_forex=not cripto))
        r, cont = resultado_1op(v)
        ok_base = cont & v.operable.values
        horas = range(24) if cripto else range(1, 18)
        mitad = v.dt.dt.normalize() < v.dt.dt.normalize().median()
        for nombre_s, fn in setups().items():
            compra, venta = fn(v)
            senal = np.where(compra.values, 1, np.where(venta.values, -1, 0))
            for h in horas:
                m = ok_base & (v.dt.dt.hour.values == h) & (senal != 0)
                n = int(m.sum())
                if n < N_MIN:
                    continue
                x = (r[m] * senal[m])
                x = x[x != 0]
                k = int((x > 0).sum())
                n_efec = len(x)
                if n_efec < N_MIN:
                    continue
                p = binomtest(k, n_efec, BE / 100, alternative="greater").pvalue
                # estabilidad: la misma celda en cada mitad del historico
                m1 = m & mitad.values
                m2 = m & ~mitad.values
                def acc(mm):
                    xx = r[mm] * senal[mm]; xx = xx[xx != 0]
                    return (100 * (xx > 0).sum() / len(xx)) if len(xx) else np.nan, len(xx)
                a1, n1 = acc(m1)
                a2, n2 = acc(m2)
                filas.append(dict(activo=a, setup=nombre_s, hora=h, n=n_efec, k=k,
                                  acierto=100 * k / n_efec, p=p, acc_mitad1=a1, n1=n1,
                                  acc_mitad2=a2, n2=n2))
    df = pd.DataFrame(filas)
    print(f"Total de celdas evaluadas (setup x par x hora, n>={N_MIN}): {len(df)}\n")

    for grupo in ["real", "ALEATORIO (control)"]:
        sub = df[df.setup == "ALEATORIO (control)"] if grupo != "real" else df[df.setup != "ALEATORIO (control)"]
        umbral = bh(sub.p.values, Q_BH)
        sobrevive = sub[sub.p <= umbral] if umbral is not None else sub.iloc[0:0]
        print(f"=== {grupo.upper()}: {len(sub)} celdas probadas, "
              f"umbral BH (q={Q_BH}) = {umbral if umbral is not None else 'ninguna sobrevive'} ===")
        if len(sobrevive):
            estables = sobrevive[(sobrevive.acc_mitad1 > BE) & (sobrevive.acc_mitad2 > BE)]
            print(f"  sobreviven la corrección: {len(sobrevive)}   "
                  f"y ADEMÁS son establesen las 2 mitades: {len(estables)}\n")
            cols = ["activo", "setup", "hora", "n", "acierto", "p", "acc_mitad1", "n1", "acc_mitad2", "n2"]
            print(sobrevive.sort_values("p")[cols].to_string(index=False,
                  float_format=lambda x: f"{x:.2f}"))
        else:
            print("  ninguna celda sobrevive la corrección.\n")
        print()

    return df


if __name__ == "__main__":
    df = main()
    df.to_pickle("busqueda_exhaustiva.pkl")
