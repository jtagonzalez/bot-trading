import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Variación de la CONFIGURACIÓN del DeMarker (no solo el umbral de velas por
fuera, que ya se barrió en demarker_reentrada): período y niveles de
sobrecompra/sobreventa, cruzados con el umbral de velas fuera — para ver si
el hallazgo anterior (periodo 13, niveles 80/20, cripto) es un punto
aislado o una zona robusta de parámetros parecidos.

    py demarker_configuraciones.py
"""
import sqlite3

import numpy as np
import pandas as pd

from estrategia_gold4_bb import CRIPTO, FOREX, cargar_velas, indicadores
from estrategias_propias import demarker
from simular_bb_tendencia import wilson

PERIODOS = [7, 10, 13, 17, 21]
NIVELES = [(70, 30), (75, 25), (80, 20), (85, 15), (90, 10)]
MIN_VELAS = [1, 3, 5, 8, 11, 14, 17, 20, 23]


def resultado_1op(v):
    ts, cl = v.ts.values, v.close.values
    return np.r_[np.sign(cl[1:] - cl[:-1]), 0], np.r_[ts[1:] - ts[:-1] == 60, False]


def racha(serie_bool):
    grupo = (serie_bool != serie_bool.shift()).cumsum()
    r = serie_bool.groupby(grupo).cumcount() + 1
    return r.where(serie_bool, 0)


def main():
    c = sqlite3.connect("velas.db")
    datos = {}
    for a in FOREX + CRIPTO:
        cripto = a in CRIPTO
        v = indicadores(cargar_velas(c, a, operable_forex=not cripto))
        r, cont = resultado_1op(v)
        ok = cont & v.operable.values
        dms = {p: demarker(v.high, v.low, p) * 100 for p in PERIODOS}
        datos[a] = (v, r, ok, dms, cripto)

    filas = []
    for periodo in PERIODOS:
        for sobrecompra, sobreventa in NIVELES:
            # precalcular rachas por activo para este periodo+niveles
            rachas = {}
            for a, (v, r, ok, dms, cripto) in datos.items():
                dm = dms[periodo]
                fuera_abajo = dm < sobreventa
                fuera_arriba = dm > sobrecompra
                rachas[a] = (fuera_abajo, racha(fuera_abajo), fuera_arriba, racha(fuera_arriba))
            for min_v in MIN_VELAS:
                for grupo, activos in (("forex", FOREX), ("cripto", CRIPTO)):
                    g = p = 0
                    for a in activos:
                        v, r, ok, dms, cripto = datos[a]
                        fuera_abajo, racha_abajo, fuera_arriba, racha_arriba = rachas[a]
                        reing_ab = fuera_abajo.shift(1, fill_value=False) & ~fuera_abajo
                        reing_ar = fuera_arriba.shift(1, fill_value=False) & ~fuera_arriba
                        compra = (reing_ab & (racha_abajo.shift(1) > min_v)).fillna(False)
                        venta = (reing_ar & (racha_arriba.shift(1) > min_v)).fillna(False)
                        senal = np.where(compra.values, 1, np.where(venta.values, -1, 0))
                        m = ok & (senal != 0)
                        x = r[m] * senal[m]; x = x[x != 0]
                        g += int((x > 0).sum()); p += int((x < 0).sum())
                    n = g + p
                    if n < 20:
                        continue
                    lo, hi = wilson(g, n)
                    filas.append(dict(periodo=periodo, sobrecompra=sobrecompra, sobreventa=sobreventa,
                                      min_velas=min_v, grupo=grupo, n=n, acierto=100 * g / n,
                                      ic_lo=lo, ic_hi=hi))
    df = pd.DataFrame(filas)
    df.to_pickle("demarker_configuraciones.pkl")

    BE = 53.76
    for grupo in ("forex", "cripto"):
        sub = df[(df.grupo == grupo) & (df.n >= 30)].sort_values("acierto", ascending=False)
        print(f"\n{'='*90}\nTOP 15 · {grupo.upper()} (n>=30), ordenado por acierto\n{'='*90}")
        print(f"{'periodo':>8}{'niveles':>12}{'min_velas':>11}{'n':>7}{'acierto':>9}{'IC95':>16}")
        for _, r in sub.head(15).iterrows():
            marca = "  <-- IC supera BE" if r.ic_lo > BE else ""
            print(f"{r.periodo:>8}{f'{r.sobrecompra:.0f}/{r.sobreventa:.0f}':>12}{r.min_velas:>11}"
                  f"{r.n:>7}{r.acierto:>8.2f}%  [{r.ic_lo:5.1f},{r.ic_hi:5.1f}]{marca}")

        # cuantas combinaciones, de las que tienen min_velas>=14, superan 55% y el IC supera BE
        altas = sub[(sub.min_velas >= 14)]
        print(f"\n  de {len(altas)} combinaciones con min_velas>=14 (n>=30):")
        print(f"    acierto >= 55%: {(altas.acierto>=55).sum()}")
        print(f"    IC95 inferior > breakeven ({BE}%): {(altas.ic_lo>BE).sum()}")
        print(f"    acierto medio: {altas.acierto.mean():.2f}%   mediana: {altas.acierto.median():.2f}%")


if __name__ == "__main__":
    main()
