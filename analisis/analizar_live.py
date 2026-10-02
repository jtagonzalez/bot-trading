import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
ANALISIS DE LA VALIDACION EN VIVO — responde "?funciona?" y "?en que se equivoca?".

Se apoya en live.db, que llena el bot. Cuatro secciones:

  1. VEREDICTO       -> el WR en vivo, contra el esperado y contra el breakeven,
                        con un test secuencial que dice si ya hay datos suficientes
                        para concluir algo o todavia no.

  2. DONDE FALLA     -> desglose por activo, hora, nivel de confianza, direccion
                        y volatilidad. Aqui se ve si el problema es general o
                        esta concentrado en un sitio concreto.

  3. POR QUE FALLA   -> separa "el modelo se equivoco" de "el modelo acerto pero
                        la ejecucion lo estropeo". Mide deslizamiento, latencia y
                        payout real. Esta es la seccion que el bot anterior no
                        podia producir por no guardar el precio de salida.

  4. QUE DESCARTAMOS -> las senales no operadas, para saber si el umbral de
                        confianza esta bien puesto.

Uso:
    py analizar_live.py
    py analizar_live.py --run 3
    py analizar_live.py --db live.db --esperado 59.67
"""
import argparse
import json
import sqlite3

import numpy as np
import pandas as pd
from scipy import stats

PAYOUT = 0.84
BREAKEVEN = 100 / (1 + PAYOUT)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    import math
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def cargar(db, run=None):
    c = sqlite3.connect(db)
    q = "SELECT * FROM v_operaciones WHERE resultado IN ('win','loss')"
    if run:
        q += f" AND run_id={int(run)}"
    ops = pd.read_sql(q + " ORDER BY ts_vela", c)
    q2 = "SELECT * FROM v_predicciones"
    if run:
        q2 += f" WHERE run_id={int(run)}"
    pred = pd.read_sql(q2, c)
    runs = pd.read_sql("SELECT * FROM runs", c)
    c.close()
    return ops, pred, runs


def bloque(titulo):
    print(f"\n{'=' * 84}\n{titulo}\n{'=' * 84}")


def veredicto(ops, esperado):
    bloque("1. VEREDICTO")
    n, k = len(ops), int(ops.acierto.sum())
    if n == 0:
        print("  Sin operaciones cerradas todavia.")
        return
    wr = 100 * k / n
    lo, hi = wilson(k, n)
    ev = (k / n) * PAYOUT - (1 - k / n)
    print(f"  operaciones cerradas : {n:,}")
    print(f"  acierto en vivo      : {wr:.2f}%   IC95=[{lo:.2f}, {hi:.2f}]")
    print(f"  esperado (backtest)  : {esperado:.2f}%")
    print(f"  breakeven            : {BREAKEVEN:.2f}%")
    print(f"  EV por operacion     : ${ev:+.4f}")
    print(f"  ganancia acumulada   : ${ops.ganancia.sum():+.2f}")

    # ?ya se puede concluir?
    p_be = stats.binomtest(k, n, 1 / (1 + PAYOUT), alternative="greater").pvalue
    p_esp = stats.binomtest(k, n, esperado / 100, alternative="less").pvalue
    print(f"\n  ?supera el breakeven?  p={p_be:.4f}  "
          f"{'SI, con significancia' if p_be < 0.05 else 'aun no concluyente'}")
    print(f"  ?rinde menos de lo esperado?  p={p_esp:.4f}  "
          f"{'SI, degradado' if p_esp < 0.05 else 'compatible con lo esperado'}")

    # cuantas operaciones faltan para concluir
    d = abs(esperado / 100 - 1 / (1 + PAYOUT))
    if d > 0:
        n_nec = int((1.96 * np.sqrt(0.5 * 0.5) / d) ** 2)
        if n < n_nec:
            print(f"\n  Faltan ~{n_nec - n:,} operaciones para tener potencia estadistica")
            print(f"  suficiente (objetivo ~{n_nec:,}).")
        else:
            print(f"\n  Muestra suficiente: el resultado ya es concluyente.")

    if lo > BREAKEVEN:
        print("\n  -> RENTABLE con significancia estadistica.")
    elif hi < BREAKEVEN:
        print("\n  -> NO rentable: el intervalo entero esta por debajo del breakeven.")
    else:
        print("\n  -> Todavia indeterminado: el intervalo cruza el breakeven.")


def donde_falla(ops):
    bloque("2. DONDE FALLA")
    if len(ops) < 20:
        print("  Muestra insuficiente para desglosar.")
        return

    def tabla(col, etiqueta, bins=None):
        d = ops.copy()
        if bins is not None:
            d[col] = pd.cut(d[col], bins)
        g = d.groupby(col, observed=True).agg(n=("acierto", "size"),
                                              wr=("acierto", "mean"),
                                              pnl=("ganancia", "sum"))
        g = g[g.n >= 5]
        if g.empty:
            return
        print(f"\n  por {etiqueta}:")
        for k_, r in g.iterrows():
            w = 100 * r.wr
            marca = "ok" if w > BREAKEVEN else "--"
            print(f"    {str(k_):<22} n={int(r.n):>5}  WR={w:6.2f}%  "
                  f"P&L=${r.pnl:+8.2f}  {marca}")

    tabla("activo", "activo")
    tabla("hora_utc", "hora UTC")
    tabla("direccion", "direccion")
    q = ops.confianza.quantile([0, .25, .5, .75, 1.0]).values
    if len(set(q)) > 2:
        tabla("confianza", "nivel de confianza", bins=np.unique(q))


def por_que_falla(ops):
    bloque("3. POR QUE FALLA — ejecucion vs modelo")
    if len(ops) < 10:
        print("  Muestra insuficiente.")
        return

    # acierto real vs el que habria dado entrar al cierre exacto de vela
    cmp = ops.dropna(subset=["acierto_teorico"])
    if len(cmp):
        real = 100 * cmp.acierto.mean()
        teo = 100 * cmp.acierto_teorico.mean()
        difs = int((cmp.acierto != cmp.acierto_teorico).sum())
        print(f"  acierto REAL (precio de entrada efectivo) : {real:.2f}%")
        print(f"  acierto TEORICO (entrando al cierre exacto): {teo:.2f}%")
        print(f"  coste de la ejecucion                      : {real - teo:+.2f} puntos")
        print(f"  operaciones donde el desenlace cambio      : {difs} de {len(cmp)}")
        if abs(real - teo) > 1.0:
            print("\n  -> La ejecucion esta afectando al resultado. Revisar latencia.")
        else:
            print("\n  -> La ejecucion NO explica diferencias: el backtest era realista.")

    if ops.latencia_ms.notna().any():
        print(f"\n  latencia desde el cierre de vela hasta la orden:")
        l = ops.latencia_ms.dropna()
        print(f"    mediana {l.median():.0f} ms   p90 {l.quantile(.9):.0f} ms   max {l.max():.0f} ms")
        if len(ops) >= 40:
            alto = ops[ops.latencia_ms > l.median()]
            bajo = ops[ops.latencia_ms <= l.median()]
            print(f"    WR con latencia alta: {100 * alto.acierto.mean():.2f}% (n={len(alto)})")
            print(f"    WR con latencia baja: {100 * bajo.acierto.mean():.2f}% (n={len(bajo)})")

    if ops.payout_real.notna().any():
        pr = ops.payout_real.dropna()
        print(f"\n  payout realmente cobrado: mediana {100 * pr.median():.2f}%  "
              f"min {100 * pr.min():.2f}%")
        be_real = 100 / (1 + pr.median())
        print(f"    -> breakeven con ese payout: {be_real:.2f}%")

    if ops.desliz_bruto.notna().any():
        d = ops.desliz_bruto.abs()
        print(f"\n  deslizamiento |entrada - cierre de vela|: "
              f"mediana {d.median():.6f}  p90 {d.quantile(.9):.6f}")


def descartadas(pred):
    bloque("4. QUE DESCARTAMOS — ?esta bien puesto el umbral?")
    no = pred[pred.operada == 0]
    si = pred[pred.operada == 1]
    print(f"  evaluadas: {len(pred):,}   operadas: {len(si):,}   descartadas: {len(no):,}")
    if len(no):
        print("\n  motivos:")
        for m, g in no.groupby("motivo_no_operada"):
            print(f"    {str(m):<22} {len(g):>6,}   confianza media {g.confianza.mean():.4f}")
    if len(si):
        print(f"\n  confianza de las operadas: mediana {si.confianza.median():.4f}  "
              f"min {si.confianza.min():.4f}")

    # ── desenlace virtual: que HABRIA pasado con las que no se operaron ──
    if "resultado_virtual" not in pred.columns or pred.resultado_virtual.isna().all():
        print("\n  Sin desenlaces virtuales todavia. Rellenalos con:")
        print("    py resolver_predicciones.py")
        return

    r = pred[pred.resultado_virtual.isin(["win", "loss"])].copy()
    r["ok"] = (r.resultado_virtual == "win").astype(int)
    empates = int((pred.resultado_virtual == "tie").sum())
    print(f"\n  DESENLACE VIRTUAL — resueltas {len(r):,} de {len(pred):,}"
          + (f"  (+{empates} empates, excluidos)" if empates else ""))
    print("  Que habria pasado con cada evaluacion, se operara o no. Multiplica")
    print("  la muestra: en el run 3 son 160 etiquetas en vez de 8.\n")
    print(f"  {'grupo':<14}{'n':>7}{'acierto':>10}{'IC95':>17}   confianza media")
    print("  " + "-" * 62)
    grupos = {}
    for etiq, g in (("operadas", r[r.operada == 1]), ("descartadas", r[r.operada == 0])):
        if not len(g):
            continue
        n, k = len(g), int(g.ok.sum())
        lo, hi = wilson(k, n)
        grupos[etiq] = (n, k)
        print(f"  {etiq:<14}{n:>7,}{100 * k / n:>9.2f}%  [{lo:5.2f},{hi:5.2f}]"
              f"{g.confianza.mean():>16.5f}")

    if len(grupos) == 2:
        (n1, k1), (n0, k0) = grupos["operadas"], grupos["descartadas"]
        wr1, wr0 = 100 * k1 / n1, 100 * k0 / n0
        try:
            _, p_sep = stats.fisher_exact([[k1, n1 - k1], [k0, n0 - k0]],
                                          alternative="greater")
        except Exception:
            p_sep = float("nan")
        print(f"\n  separacion: {wr1 - wr0:+.2f} puntos a favor de las operadas")
        print(f"  ?es real esa separacion?  p={p_sep:.4f}  "
              f"{'SI, el umbral selecciona' if p_sep < 0.05 else 'aun no concluyente'}")
        if wr0 >= wr1:
            print("\n  -> AVISO: las descartadas aciertan igual o mas que las operadas.")
            print("     El umbral no esta seleccionando nada: solo reduce el numero")
            print("     de operaciones. Revisar la calibracion del modelo.")

    # ?estaria mejor el umbral en otro sitio?
    if len(r) >= 60:
        print("\n  ?Y si el umbral fuera otro?  (acierto virtual por decil de confianza)")
        r["decil"] = pd.qcut(r.confianza, 10, labels=False, duplicates="drop")
        print(f"  {'decil':>6}{'conf. min':>12}{'n':>7}{'acierto':>10}")
        print("  " + "-" * 38)
        for d_, g in r.groupby("decil"):
            if len(g) < 5:
                continue
            marca = "  <-- sobre breakeven" if 100 * g.ok.mean() > BREAKEVEN else ""
            print(f"  {int(d_):>6}{g.confianza.min():>12.5f}{len(g):>7,}"
                  f"{100 * g.ok.mean():>9.2f}%{marca}")
        print("\n  Los deciles altos deberian acertar mas que los bajos. Si la")
        print("  columna no sube, la confianza del modelo no significa nada y el")
        print("  umbral es arbitrario, este donde este.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="live.db")
    ap.add_argument("--run", type=int, default=None)
    ap.add_argument("--esperado", type=float, default=None,
                    help="WR esperado; por defecto se toma del run")
    a = ap.parse_args()

    try:
        ops, pred, runs = cargar(a.db, a.run)
    except Exception as e:
        raise SystemExit(f"no se pudo leer {a.db}: {e}\n"
                         f"Crea el esquema con: py registro.py --crear")

    esperado = a.esperado
    if esperado is None:
        if len(runs) and runs.wr_esperado.notna().any():
            esperado = float(runs.wr_esperado.dropna().iloc[-1])
        else:
            esperado = 59.67

    print(f"Base: {a.db}" + (f"  |  run {a.run}" if a.run else "  |  todos los runs"))
    if len(runs):
        r = runs.iloc[-1]
        print(f"Modelo: {r.modelo_nombre} ({r.modelo_hash})  "
              f"entrenado hasta {r.modelo_entrenado}")
        print(f"Cuenta: {r.tipo_cuenta}   horas: {r.horas_operativas}   monto: ${r.monto}")

    veredicto(ops, esperado)
    donde_falla(ops)
    por_que_falla(ops)
    descartadas(pred)

    print(f"\n{'=' * 84}")
    print("Datos crudos disponibles para reanalisis:")
    print("  predicciones.ventana_ohlc  -> las velas de cada decision, en JSON")
    print("  predicciones.indicadores   -> todos los indicadores de ese momento")
    print("  Con eso puedes probar indicadores nuevos sobre decisiones ya tomadas,")
    print("  sin re-descargar nada ni esperar a que vuelvan a darse las condiciones.")


if __name__ == "__main__":
    main()
