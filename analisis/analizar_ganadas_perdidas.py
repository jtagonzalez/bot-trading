import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
GANADAS vs PERDIDAS — que distingue a unas de otras, y si la diferencia es real.

Compara las señales con desenlace conocido (operadas o no: el desenlace virtual
dice que habria pasado) en todas las variables disponibles:
  * los 35 indicadores que se guardan en cada evaluacion
  * la vela de entrada, sacada de la ventana OHLC: tamaño, cuerpo, color, y si
    la señal va a favor o en contra de esa vela
  * hora, activo, direccion, confianza del modelo

El analisis se hace tres veces: todas juntas, solo CALL y solo PUT. Un RSI
bajo en una CALL es comprar en sobreventa; en una PUT es vender en sobreventa.
Mezclarlas cancelaria el efecto.

El riesgo de este ejercicio es conocido: con cientos de comparaciones, algunas
salen "significativas" por azar. Por eso:
  1. correccion de Benjamini-Hochberg sobre TODAS las pruebas hechas
  2. division temporal: los tramos se descubren en la primera mitad de los
     datos y se comprueban en la segunda, que no participo en la busqueda

    py analizar_ganadas_perdidas.py
"""
import json
import math
import sqlite3

import numpy as np
import pandas as pd
from scipy import stats

BE = 52.63   # breakeven con payout 90%


def cargar():
    c = sqlite3.connect("file:live.db?mode=ro", uri=True)
    df = pd.read_sql("""SELECT id, ts_vela, activo, direccion, confianza, prob,
                               hora_utc, indicadores, ventana_ohlc, resultado_virtual
                        FROM predicciones
                        WHERE run_id IN (6,7) AND resultado_virtual IN ('win','loss')
                        ORDER BY ts_vela""", c)
    ind = pd.DataFrame([json.loads(x) for x in df.indicadores])
    ind = ind.drop(columns=[k for k in ("hora", "dow") if k in ind.columns])
    df = pd.concat([df.drop(columns=["indicadores"]), ind], axis=1)

    filas = []
    for v in df.ventana_ohlc:
        w = np.array(json.loads(v), dtype=float)
        o, h, l, cc = w[-1]
        rng = w[:, 1] - w[:, 2]
        media = rng[:-1].mean()
        filas.append({
            "vela_tam_bps": (h - l) / cc * 1e4,
            "vela_cuerpo_bps": abs(cc - o) / cc * 1e4,
            "vela_tam_rel": (h - l) / media if media > 0 else np.nan,
            "vela_color": 1 if cc > o else (-1 if cc < o else 0),
        })
    df = pd.concat([df.drop(columns=["ventana_ohlc"]), pd.DataFrame(filas)], axis=1)
    sig = np.where(df.direccion == "call", 1, -1)
    df["a_favor_vela"] = (df.vela_color == sig).astype(int)
    df["gano"] = (df.resultado_virtual == "win").astype(int)
    return df


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def bh(p):
    p = np.asarray(p, dtype=float)
    orden = np.argsort(p)
    m = len(p)
    q = np.empty(m)
    prev = 1.0
    for rank in range(m, 0, -1):
        i = orden[rank - 1]
        prev = min(prev, p[i] * m / rank)
        q[i] = prev
    return q


def main():
    df = cargar()
    excluir = {"id", "ts_vela", "activo", "direccion", "resultado_virtual", "gano"}
    num = [c for c in df.columns
           if c not in excluir and pd.api.types.is_numeric_dtype(df[c])]
    mitad = len(df) // 2
    print(f"{len(df)} señales ({df.gano.sum()} ganadas / {len(df) - df.gano.sum()} perdidas)")
    print(f"descubrimiento: primeras {mitad} · comprobacion: ultimas {len(df) - mitad}\n")

    # 1. distribuciones
    pruebas = []
    for grupo, sub in (("todas", df), ("call", df[df.direccion == "call"]),
                       ("put", df[df.direccion == "put"])):
        for f in num:
            g = sub.loc[sub.gano == 1, f].dropna()
            pe = sub.loc[sub.gano == 0, f].dropna()
            if len(g) < 30 or len(pe) < 30 or g.nunique() < 3:
                continue
            _, pv = stats.mannwhitneyu(g, pe, alternative="two-sided")
            pruebas.append((grupo, f, g.median(), pe.median(), pv))
    q = bh([x[4] for x in pruebas])
    res = sorted(zip(pruebas, q), key=lambda x: x[0][4])

    print("=" * 92)
    print("1. ¿DIFIEREN LAS GANADAS DE LAS PERDIDAS?  (las 15 mayores diferencias)")
    print("=" * 92)
    print(f"  {'grupo':<6}{'variable':<18}{'med. ganadas':>14}{'med. perdidas':>15}"
          f"{'p':>9}{'q(BH)':>8}")
    for (grupo, f, mg, mp, pv), qv in res[:15]:
        marca = "  <-- sobrevive BH" if qv < 0.05 else ""
        print(f"  {grupo:<6}{f:<18}{mg:>14.4f}{mp:>15.4f}{pv:>9.4f}{qv:>8.3f}{marca}")
    n05 = sum(1 for x in pruebas if x[4] < 0.05)
    print(f"\n  pruebas: {len(pruebas)} · con p<0.05: {n05} "
          f"(el azar solo daria ~{len(pruebas) * 0.05:.0f}) · sobreviven BH: "
          f"{sum(1 for x in q if x < 0.05)}")

    # 2. tramos descubiertos en la 1a mitad, comprobados en la 2a
    print("\n" + "=" * 92)
    print("2. ¿HAY UN TRAMO GANADOR?  (quintiles fijados en la 1a mitad, comprobados en la 2a)")
    print("=" * 92)
    A, B = df.iloc[:mitad], df.iloc[mitad:]
    cand = []
    for grupo, fa, fb in (("todas", A, B),
                          ("call", A[A.direccion == "call"], B[B.direccion == "call"]),
                          ("put", A[A.direccion == "put"], B[B.direccion == "put"])):
        for f in num:
            xa = fa[f].dropna()
            if xa.nunique() < 5:
                continue
            cortes = np.unique(np.quantile(xa, [0, .2, .4, .6, .8, 1]))
            if len(cortes) < 4:
                continue
            for i in range(len(cortes) - 1):
                lo, hi = cortes[i], cortes[i + 1]
                ma = (fa[f] >= lo) & (fa[f] <= hi)
                mb = (fb[f] >= lo) & (fb[f] <= hi)
                na, ka = int(ma.sum()), int(fa.gano[ma].sum())
                nb, kb = int(mb.sum()), int(fb.gano[mb].sum())
                if na < 25 or nb < 25:
                    continue
                cand.append((grupo, f, lo, hi, na, 100 * ka / na, nb, 100 * kb / nb, kb))
    cand.sort(key=lambda x: -x[5])
    print(f"  {'grupo':<6}{'variable':<17}{'tramo':>22}{'1a mitad':>14}{'2a mitad':>15}  ¿se repite?")
    for grupo, f, lo, hi, na, wa, nb, wb, kb in cand[:20]:
        l2, _ = wilson(kb, nb)
        rep = "SI, sobre BE" if l2 > BE else ("se mantiene >BE" if wb > BE else "NO, se cae")
        print(f"  {grupo:<6}{f:<17}{lo:>10.3f} a {hi:<9.3f}{wa:>6.1f}% n={na:<4}"
              f"{wb:>7.1f}% n={nb:<4}  {rep}")
    top = cand[:20]
    media_a = np.mean([c[5] for c in top])
    media_b = np.mean([c[7] for c in top])
    print(f"\n  los 20 mejores tramos: {media_a:.1f}% al descubrirlos -> {media_b:.1f}% "
          f"en datos nuevos")
    print(f"  siguen sobre el breakeven en la 2a mitad: {sum(1 for c in top if c[7] > BE)} de 20")

    # 3. las preguntas concretas
    print("\n" + "=" * 92)
    print("3. LAS PREGUNTAS CONCRETAS")
    print("=" * 92)

    def tabla(col, nombre, bins=None, etiquetas=None):
        d = df.copy()
        if bins is not None:
            d[col] = pd.cut(d[col], bins, labels=etiquetas)
        print(f"\n  {nombre}:")
        for k, g in d.groupby(col, observed=True):
            n, kk = len(g), int(g.gano.sum())
            if n < 20:
                continue
            l, h = wilson(kk, n)
            marca = "  <-- sobre BE" if l > BE else ""
            print(f"    {str(k):<22} n={n:>4}  {100 * kk / n:6.2f}%  [{l:5.1f},{h:5.1f}]{marca}")

    tabla("direccion", "direccion de la señal")
    tabla("a_favor_vela", "¿la señal sigue el color de la vela de entrada? (1 = si)")
    tabla("vela_tam_rel", "tamaño de la vela de entrada vs las 19 anteriores",
          [0, .5, .8, 1.2, 2, 99],
          ["muy chica <0.5x", "chica .5-.8x", "normal .8-1.2x", "grande 1.2-2x", "muy grande >2x"])
    tabla("hora_utc", "hora UTC")
    tabla("activo", "activo")


if __name__ == "__main__":
    main()
