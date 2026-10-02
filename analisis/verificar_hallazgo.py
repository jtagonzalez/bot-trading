import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
?EL HALLAZGO ES REAL O ES EL MEJOR DE MUCHOS INTENTOS?

El escaneo de la ventana operable saco dos celdas por encima del breakeven:
EURUSD a 15m (55.33%) y la hora 07 UTC (55.39%). Ninguna de las dos significa
nada por si sola: se han mirado 10 activos y 17 horas, asi que la mejor de
cada lista esta alta *por construccion*. Con 17 pruebas independientes al 5%,
la probabilidad de que al menos una salga "significativa" por puro azar ronda
el 58%.

Este script aplica las tres pruebas que separan un edge de un espejismo:

  1. CORRECCION POR COMPARACIONES MULTIPLES (Benjamini-Hochberg). Controla la
     proporcion de falsos descubrimientos entre todas las celdas miradas, no
     solo la que mas gusto.

  2. ESTABILIDAD EN EL TIEMPO. Un edge real aparece en la primera mitad de la
     muestra y en la segunda. Uno inventado vive en un periodo concreto. Es
     la prueba mas dura y la que mas candidatos mata.

  3. VALIDACION CRUZADA DE LA INTERSECCION. Si el edge esta en EURUSD y
     tambien en la hora 07, ?siguen estando cuando se miran por separado, o
     es la misma muestra contada dos veces?

Uso:
    py verificar_hallazgo.py                 # 15m, payout 90%
    py verificar_hallazgo.py --npz oos_30m.npz --payout 87
"""
import argparse

import numpy as np
from scipy import stats

import simulador as S


def bh(pvals, alfa=0.05):
    """Benjamini-Hochberg: devuelve el umbral de p que sigue siendo aceptable."""
    p = np.sort(np.asarray(pvals))
    m = len(p)
    if m == 0:
        return 0.0
    umbrales = alfa * np.arange(1, m + 1) / m
    pasa = p <= umbrales
    return float(p[pasa].max()) if pasa.any() else 0.0


def prueba(ac, be):
    """p de que el acierto observado supere el breakeven por azar."""
    n, k = len(ac), int(ac.sum())
    if n == 0:
        return 1.0, 0.0, (0.0, 0.0), 0
    p = stats.binomtest(k, n, be / 100, alternative="greater").pvalue
    return p, 100 * k / n, S.wilson(k, n), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="oos_15m.npz")
    ap.add_argument("--payout", type=float, default=90.0)
    ap.add_argument("--pct", type=float, default=10.0,
                    help="fraccion mas confiable que se opera")
    ap.add_argument("--minimo", type=int, default=300,
                    help="tamano minimo de celda para tomarsela en serio")
    a = ap.parse_args()

    be = 100 / (1 + a.payout / 100)
    d = np.load(a.npz, allow_pickle=True)
    p, y, hora, activo = d["p"], d["y"], d["hora"], d["activo"]
    dt = d["dt"].astype("datetime64[s]")

    conf = np.abs(p - 0.5)
    u = np.quantile(conf, 1 - a.pct / 100)
    sel = conf >= u
    ac_all = np.where(p > 0.5, y, 1 - y)

    print(f"Archivo: {a.npz}   payout {a.payout}%  ->  breakeven {be:.2f}%")
    print(f"Operando el {a.pct:.0f}% mas confiable: {sel.sum():,} de {len(p):,} "
          f"predicciones out-of-sample")
    print(f"Periodo: {dt.min()} a {dt.max()}")

    # ── 1. todas las celdas, con correccion ──
    print(f"\n{'=' * 78}")
    print("1. TODAS LAS CELDAS MIRADAS, CON CORRECCION POR COMPARACIONES MULTIPLES")
    print(f"{'=' * 78}")
    celdas = []
    for h in sorted(set(hora[sel])):
        m = sel & (hora == h)
        if m.sum() >= a.minimo:
            celdas.append((f"hora {int(h):02d}", m))
    for act in sorted(set(activo[sel])):
        m = sel & (activo == act)
        if m.sum() >= a.minimo:
            celdas.append((str(act), m))

    res = []
    for nombre, m in celdas:
        pv, wr, ic, n = prueba(ac_all[m], be)
        res.append((nombre, pv, wr, ic, n))
    pvals = [r[1] for r in res]
    corte = bh(pvals, 0.05)
    bonf = 0.05 / len(res) if res else 0

    print(f"  celdas evaluadas: {len(res)}")
    print(f"  umbral Bonferroni: p < {bonf:.5f}")
    print(f"  umbral Benjamini-Hochberg: p <= {corte:.5f}"
          f"{'  (ninguna celda lo pasa)' if corte == 0 else ''}")
    print(f"\n  {'celda':<12}{'n':>8}{'WR':>8}{'IC95':>16}{'p':>10}   veredicto")
    print("  " + "-" * 72)
    for nombre, pv, wr, ic, n in sorted(res, key=lambda r: r[1]):
        if pv < bonf:
            v = "PASA Bonferroni"
        elif corte and pv <= corte:
            v = "pasa BH"
        elif wr > be:
            v = "sobre BE, no significativa"
        else:
            v = "no"
        print(f"  {nombre:<12}{n:>8,}{wr:>7.2f}%  [{ic[0]:5.2f},{ic[1]:5.2f}]{pv:>10.5f}   {v}")

    # ── 2. estabilidad en el tiempo ──
    print(f"\n{'=' * 78}")
    print("2. ESTABILIDAD EN EL TIEMPO — ?esta el edge en las dos mitades?")
    print(f"{'=' * 78}")
    print("  Un edge real no elige epoca. Si solo aparece en una mitad, lo que")
    print("  encontramos fue un periodo, no una regla.\n")
    corte_t = np.median(dt[sel].astype("int64"))
    ti = dt.astype("int64")
    sobrevivientes = []
    for nombre, pv, wr, ic, n in sorted(res, key=lambda r: r[1])[:8]:
        m = dict(celdas)[nombre]
        m1, m2 = m & (ti <= corte_t), m & (ti > corte_t)
        if m1.sum() < 100 or m2.sum() < 100:
            continue
        p1, wr1, ic1, n1 = prueba(ac_all[m1], be)
        p2, wr2, ic2, n2 = prueba(ac_all[m2], be)
        ok = wr1 > be and wr2 > be
        print(f"  {nombre:<12} 1a mitad: {wr1:6.2f}% (n={n1:,})   "
              f"2a mitad: {wr2:6.2f}% (n={n2:,})   "
              f"{'ambas sobre BE' if ok else 'se cae en una mitad'}")
        if ok:
            sobrevivientes.append((nombre, wr1, wr2, n1, n2))

    # ── 3. ?son el mismo hallazgo contado dos veces? ──
    print(f"\n{'=' * 78}")
    print("3. INDEPENDENCIA — ?son dos hallazgos o uno solo visto de dos formas?")
    print(f"{'=' * 78}")
    finalistas = [r[0] for r in sorted(res, key=lambda r: r[1])[:2]]
    if len(finalistas) == 2:
        A, B = (dict(celdas)[finalistas[0]], dict(celdas)[finalistas[1]])
        inter, soloA, soloB = A & B, A & ~B, B & ~A
        print(f"  solapamiento: {int(inter.sum())} predicciones en comun\n")
        for etiq, m in ((f"{finalistas[0]} SIN {finalistas[1]}", soloA),
                        (f"{finalistas[1]} SIN {finalistas[0]}", soloB),
                        (f"{finalistas[0]} Y {finalistas[1]} a la vez", inter)):
            if m.sum() < 50:
                print(f"  {etiq:<34} muestra demasiado pequena (n={int(m.sum())})")
                continue
            pv, wr, ic, n = prueba(ac_all[m], be)
            v = "aguanta solo" if ic[0] > be else ("sobre BE" if wr > be else "se cae")
            print(f"  {etiq:<34} n={n:>6,}  WR={wr:6.2f}%  "
                  f"[{ic[0]:5.2f},{ic[1]:5.2f}]  {v}")

    # ── 4. veredicto ──
    print(f"\n{'=' * 78}")
    print("4. VEREDICTO")
    print(f"{'=' * 78}")
    # Bonferroni controla que no haya NI UN falso positivo y es demasiado duro
    # cuando se miran decenas de celdas correlacionadas entre si. Benjamini-
    # Hochberg controla la PROPORCION de falsos hallazgos, que es lo que
    # interesa aqui: si de dos candidatos uno es falso, lo dira la validacion
    # en vivo. El criterio duro es el otro: aguantar partido en dos mitades.
    pasan_bh = [r for r in res if corte and r[1] <= corte]
    nombres_ok = {s[0] for s in sobrevivientes}
    dobles = [r for r in pasan_bh if r[0] in nombres_ok]

    if not pasan_bh:
        print("  Ninguna celda supera la correccion por comparaciones multiples.")
        print(f"  El 55% que parecia un edge es el maximo de {len(res)} celdas:")
        print("  exactamente lo que produce el azar.")
    elif not dobles:
        print("  Hay celdas que pasan la correccion, pero ninguna aguanta")
        print("  partida en dos mitades. Eso es un periodo favorable, no una regla.")
    else:
        print(f"  {len(dobles)} de {len(res)} celdas pasan LAS DOS pruebas")
        print("  (correccion por comparaciones multiples + estabilidad temporal):\n")
        for nombre, pv, wr, ic, n in dobles:
            wr1, wr2 = next((s[1], s[2]) for s in sobrevivientes if s[0] == nombre)
            ev = (wr / 100) * (a.payout / 100) - (1 - wr / 100)
            print(f"    {nombre:<12} {wr:.2f}%  (n={n:,})  p={pv:.5f}")
            print(f"    {'':<12} mitades: {wr1:.2f}% / {wr2:.2f}%   EV ${ev:+.4f} por $1")
        print("\n  Esto NO prueba que sea rentable. Prueba que no hemos podido")
        print("  matarlo con los datos que tenemos. Lo unico que lo decide es")
        print("  operarlo en PRACTICE y contar.")
        if len(dobles) > 1:
            print("\n  Antes de operar: mirar el apartado 3. Si los candidatos se")
            print("  solapan, es un hallazgo, no dos, y hay que operar la interseccion.")


if __name__ == "__main__":
    main()

