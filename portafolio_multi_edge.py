"""
¿Se puede armar, combinando los edges YA CONFIRMADOS de este proyecto, una
cartera que opere >=5 veces por día (payout>=80%) y que además termine la
MAYORÍA de los días en positivo? Esa es la pregunta de negocio real -- el WR
promedio y la cartera "más días verdes" no son la misma pregunta.

Tres edges confirmados hasta ahora (ver memoria del proyecto):
  A) ML cripto 15m, BTCUSD/ETHUSD, top 20% confianza (modelo_cripto15,
     hash bc23a4be4266). Payout 87%, 24x7. WR OOS ~56.9% (ver
     backtest_filtro_tendencia_cripto.py: 26.198 señales, IC95 [56.3,57.5]).
  B) ML forex 15m, EURUSD-op, top 10% confianza, horario 1-17h UTC
     (modelo_op15, hash bfbe36396802, pero aquí se reproduce su propio
     walk-forward restringido a EURUSD). Payout 90%. WR esperado ~55.6%.
  C) DeMarker(17) 80/20, reingreso tras racha>17 velas de 1 min fuera de
     banda, SOLO BTCUSD/ETHUSD. Payout asumido 86%. WR ~62.07% pero con muy
     pocos datos (1m cripto solo existe 2026-08-01 a 2026-09-30, ~2 meses).

Para cada edge se simula:
  * SIN martingala: $1 fijo por señal, resultado = +payout si gana, -1 si
    pierde.
  * CON martingala (4 pasos, MISMA matemática que estrategia_gold4_bb.
    ciclos_de: tras perder, vuelve a apostar en la vela siguiente con el
    monto x (1+payout)/payout, hasta max_ops intentos). Para el edge A/B
    (15m) se generaliza ciclos_de a un paso de 900s en vez de 60s, usando el
    MISMO criterio de reintento (la vela que sigue, sin exigir que vuelva a
    ser señal -- igual que gold4_bb, que tampoco lo exige).

Con eso se construye, por cada combinación de edges disponible ese día, el
P&L diario combinado y se reporta qué fracción de los días históricos habría
cerrado en positivo -- con y sin martingala.

Reutiliza directamente: entrenar_modelo.preparar, ml_walkforward.crear_modelo,
estrategia_gold4_bb (cargar_velas, indicadores, ciclos_de, PAYOUT, MULT_MARTINGALA),
estrategias_propias.demarker_reentrada. No se reentrena nada nuevo: se
reproduce el walk-forward que este proyecto ya usa en validar_cripto.py /
validar_pares.py para producir señales fuera de muestra.

    py portafolio_multi_edge.py
"""
import math
import os
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

import entrenar_modelo as E
import ml_walkforward as ML
import simulador as S
import estrategia_gold4_bb as G
from estrategias_propias import demarker_reentrada

MAX_OPS = 4          # la rama en curso es "martingala-4pasos"
CACHE_CRIPTO = "oos_cripto_filtros.pkl"     # generado por backtest_filtro_tendencia_cripto.py
CACHE_EURUSD = "oos_eurusd_15m.pkl"
CACHE_DEMARKER = "oos_demarker_1m.pkl"
rng = np.random.default_rng(42)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


# ───────────────────────── edge A: cripto ML 15m ─────────────────────────

def edge_cripto():
    """Reusa el cache ya generado por backtest_filtro_tendencia_cripto.py
    (mismo walk-forward de validar_cripto.py, 8 bloques, 20% más confiable)."""
    if not os.path.exists(CACHE_CRIPTO):
        raise SystemExit(f"Falta {CACHE_CRIPTO}; correr primero backtest_filtro_tendencia_cripto.py")
    D = pd.read_pickle(CACHE_CRIPTO)
    D = D.sort_values(["activo", "ts"]).reset_index(drop=True)
    return D, 0.87


# ───────────────────────── edge B: EURUSD-op ML 15m ─────────────────────────

VIEJOS_FOREX = ["AUDUSD", "EURGBP", "EURJPY", "EURUSD", "GBPJPY",
               "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]


def edge_eurusd(pct=0.10, bloques=8):
    """Mismo walk-forward que validar_pares.py / modelo_op15.joblib: el
    modelo se entrena con los 10 pares de siempre JUNTOS (igual que en
    producción, que evalúa los 9 pares -op y opera solo EURUSD-op con un
    modelo y umbral compartidos) y luego se queda solo con las señales de
    EURUSD. Entrenar solo con EURUSD (lo que hacía una versión anterior de
    este script) da un modelo mucho más pobre -- EURUSD solo no alcanza la
    masa de datos con la que el modelo real fue entrenado (429.879 filas,
    10 pares, ver meta de modelo_op15.joblib)."""
    if os.path.exists(CACHE_EURUSD):
        print(f"(usando cache {CACHE_EURUSD})")
        return pd.read_pickle(CACHE_EURUSD), 0.90
    print("Preparando 10 pares forex 15m, 1-17h UTC (igual que modelo_op15.joblib)...")
    M = E.preparar("velas.db", 900, (1, 17), VIEJOS_FOREX)
    feats = [f for f in ML.FEATURES if f in M.columns]
    X = M[feats].replace([np.inf, -np.inf], np.nan)
    X = X.fillna(X.median()).values
    y = M.sube.values
    cortes = np.linspace(0, len(M), bloques + 1).astype(int)
    partes = []
    for i in range(1, bloques):
        trf, ti, tf_ = cortes[i], cortes[i], cortes[i + 1]
        if trf < 2000 or tf_ - ti < 100:
            continue
        m = ML.crear_modelo("gb")
        m.fit(X[:trf], y[:trf])
        umbral = np.quantile(np.abs(m.predict_proba(X[:trf])[:, 1] - 0.5), 1 - pct)
        p = m.predict_proba(X[ti:tf_])[:, 1]
        sub = M.iloc[ti:tf_].copy()
        sub["p"], sub["senal"] = p, np.abs(p - 0.5) >= umbral
        partes.append(sub)
        print(f"  bloque {i}/{bloques - 1}  train={trf:,}  umbral={umbral:.4f}  "
              f"señales={int(sub.senal.sum()):,}/{len(sub):,}")
    D = pd.concat(partes).reset_index(drop=True)
    D["call"] = D.p > 0.5
    D["gano"] = np.where(D.call, D.sube, 1 - D.sube)
    D["operable"] = True   # ya viene filtrado a horas 1-17 por E.preparar
    D = D[D.activo == "EURUSD"].reset_index(drop=True)   # solo se opera EURUSD-op en vivo
    D.to_pickle(CACHE_EURUSD)
    return D, 0.90


# ───────────────────────── edge C: DeMarker(17) 1m cripto ─────────────────────────

def edge_demarker(periodo=17, sobrecompra=80, sobreventa=20, min_velas=17):
    """DeMarker(17) 80/20, reingreso tras racha>17 velas fuera de banda, 1
    minuto, SOLO BTCUSD/ETHUSD -- la regla exacta de supervisar_demarker.sh.
    Solo hay 1m de cripto desde 2026-08-01 (velas.db), así que esta ventana
    es mucho más corta que las otras dos (~2 meses)."""
    if os.path.exists(CACHE_DEMARKER):
        print(f"(usando cache {CACHE_DEMARKER})")
        return pd.read_pickle(CACHE_DEMARKER), 0.86
    import sqlite3
    con = sqlite3.connect("velas.db")
    filas = []
    for a in G.CRIPTO:
        v = G.indicadores(G.cargar_velas(con, a, tf=60, operable_forex=False)).copy()
        compra, venta = demarker_reentrada(v, n=periodo, sobreventa=sobreventa,
                                           sobrecompra=sobrecompra, min_velas=min_velas)
        nxt = v.close.shift(-1)
        contigua_sig = (v.ts.shift(-1) - v.ts == 60).fillna(False)
        es_senal = (compra | venta) & contigua_sig & nxt.notna() & (nxt != v.close)
        direccion = np.where(compra, 1, np.where(venta, -1, 0))
        v["senal"] = es_senal.values
        v["call"] = direccion == 1
        # "gano" solo tiene sentido donde hay señal; se calcula para todas las
        # filas por comodidad vectorial pero solo se usa donde senal=True.
        v["gano"] = np.where(v.call.values, (nxt > v.close).astype(int),
                             (nxt < v.close).astype(int))
        v["activo"] = a
        v["operable"] = True
        # IMPORTANTE: se conserva la serie COMPLETA de 1 minuto (no solo las
        # señales), porque ciclos_generico necesita las velas siguientes
        # contiguas para simular los reintentos de la martingala.
        filas.append(v)
    D = pd.concat(filas).sort_values(["activo", "ts"]).reset_index(drop=True)
    D.to_pickle(CACHE_DEMARKER)
    return D, 0.86


# ───────────────────────── martingala genérica (igual matemática que ciclos_de) ─────────────────────────

def ciclos_generico(ts, close, entra, direccion, operable, payout, step_seg, max_ops=MAX_OPS):
    """Generalización de estrategia_gold4_bb.ciclos_de a cualquier paso de
    tiempo y payout. Tras perder, reintenta en la SIGUIENTE vela contigua del
    mismo paso, sin exigir que esa vela vuelva a ser señal (igual que gold4).
    Devuelve lista de (ts_inicio, neto, intentos, gano, total_arriesgado)."""
    mult = (1 + payout) / payout
    n = len(ts)
    out = []
    i = 0
    while i < n - 1:
        if not entra[i] or not operable[i]:
            i += 1
            continue
        sentido, apuesta, neto, arriesgado, k, j, gano = (
            direccion[i], 1.0, 0.0, 0.0, 0, i, False)
        while k < max_ops and j + 1 < n and ts[j + 1] - ts[j] == step_seg and operable[j]:
            r = np.sign(close[j + 1] - close[j]) * sentido
            k += 1
            arriesgado += apuesta
            if r > 0:
                neto += apuesta * payout
                gano = True
                j += 1
                break
            if r < 0:
                neto -= apuesta
                apuesta *= mult
            j += 1
        if k:
            out.append((ts[i], neto, k, gano, arriesgado))
        i = max(j, i + 1)
    return out


def simular_edge_flat(D, payout):
    """Sin martingala: $1 fijo por señal."""
    d = D[D.senal].copy()
    d["neto"] = np.where(d.gano, payout, -1.0)
    d["arriesgado"] = 1.0
    return d


def simular_edge_martingala(D, payout, step_seg, max_ops=MAX_OPS):
    """Con martingala, por activo (cada activo tiene su propia serie contigua)."""
    filas = []
    for a, g in D.groupby("activo"):
        g = g.sort_values("ts").reset_index(drop=True)
        ts = g.ts.values.astype(np.int64)
        close = g.close.values.astype(float)
        entra = g.senal.values if "senal" in g.columns else np.ones(len(g), bool)
        direccion = np.where(g.call.values, 1, -1)
        operable = g.operable.values if "operable" in g.columns else np.ones(len(g), bool)
        ciclos = ciclos_generico(ts, close, entra, direccion, operable, payout, step_seg, max_ops)
        for ts0, neto, k, gano, arriesgado in ciclos:
            filas.append({"activo": a, "ts": ts0, "neto": neto, "intentos": k,
                          "gano": gano, "arriesgado": arriesgado})
    df = pd.DataFrame(filas)
    if len(df):
        df["dt"] = pd.to_datetime(df.ts, unit="s", utc=True)
    return df


def resumen_edge(nombre, flat, mart, payout, be):
    n_f, k_f = len(flat), int(flat.gano.sum()) if "gano" in flat.columns else int((flat.neto > 0).sum())
    lo, hi = wilson(k_f, n_f)
    dias = flat.dt.dt.normalize().nunique() if "dt" in flat.columns else flat.dt.dt.normalize().nunique()
    print(f"\n  {nombre}  (payout {100*payout:.0f}%, breakeven {be:.2f}%)")
    print(f"    SIN martingala: n={n_f:,}  WR={100*k_f/n_f:.2f}%  IC95=[{lo:.2f},{hi:.2f}]  "
          f"EV/$={flat.neto.mean():+.4f}  días={dias}  ops/día={n_f/max(1,dias):.2f}  "
          f"$/día esperado={flat.neto.sum()/max(1,dias):+.3f}")
    if len(mart):
        ev_riesgo = mart.neto.sum() / mart.arriesgado.sum()
        pct_perdidos = 100 * (~mart.gano).mean()
        print(f"    CON martingala ({MAX_OPS} pasos): ciclos={len(mart):,}  "
              f"ciclos perdidos (agotan intentos)={pct_perdidos:.1f}%  "
              f"EV por $ arriesgado={ev_riesgo:+.4f}  "
              f"$/día esperado={mart.neto.sum()/max(1,dias):+.3f}  "
              f"arriesgado medio/ciclo=${mart.arriesgado.mean():.2f}")
        print(f"    -> martingala {'MEJORA' if ev_riesgo > flat.neto.mean() else 'EMPEORA'} "
              f"el EV por $ arriesgado frente a apuesta plana "
              f"({ev_riesgo:+.4f} vs {flat.neto.mean():+.4f})")
    return n_f, k_f, dias


def pnl_diario(flat_or_mart, col_valor="neto"):
    """$/día sumando TODAS las filas (ya se decide fuera qué edges entran)."""
    d = flat_or_mart.copy()
    d["dia"] = d.dt.dt.normalize()
    return d.groupby("dia")[col_valor].sum()


def reporte_dias(pnl, etiqueta):
    n = len(pnl)
    if n == 0:
        print(f"  {etiqueta}: sin días.")
        return
    pos = int((pnl > 0).sum())
    neg = int((pnl < 0).sum())
    cero = n - pos - neg
    lo, hi = wilson(pos, n)
    print(f"  {etiqueta}: {n} días  |  positivos={pos} ({100*pos/n:.1f}%, IC95=[{lo:.1f},{hi:.1f}])  "
          f"negativos={neg} ({100*neg/n:.1f}%)  neutros={cero}")
    print(f"      $/día: media={pnl.mean():+.3f}  mediana={pnl.median():+.3f}  "
          f"peor día={pnl.min():+.2f}  mejor día={pnl.max():+.2f}  std={pnl.std():.3f}")


def main():
    print("=" * 100)
    print("EDGE A — ML cripto 15m (BTCUSD/ETHUSD, top 20% confianza)")
    print("=" * 100)
    DA, pay_a = edge_cripto()
    be_a = 100 / (1 + pay_a)
    flat_a = simular_edge_flat(DA, pay_a)
    mart_a = simular_edge_martingala(DA, pay_a, step_seg=900)
    resumen_edge("Cripto ML 15m", flat_a, mart_a, pay_a, be_a)

    print("\n" + "=" * 100)
    print("EDGE B — ML forex 15m (EURUSD-op, top 10% confianza, 1-17h UTC)")
    print("=" * 100)
    DB, pay_b = edge_eurusd()
    be_b = 100 / (1 + pay_b)
    DB["activo"] = "EURUSD"
    flat_b = simular_edge_flat(DB, pay_b)
    mart_b = simular_edge_martingala(DB, pay_b, step_seg=900)
    resumen_edge("EURUSD-op ML 15m", flat_b, mart_b, pay_b, be_b)

    print("\n" + "=" * 100)
    print("EDGE C — DeMarker(17) 80/20, reingreso >17 velas 1m, SOLO cripto")
    print("=" * 100)
    DC, pay_c = edge_demarker()
    be_c = 100 / (1 + pay_c)
    flat_c = simular_edge_flat(DC, pay_c)
    mart_c = simular_edge_martingala(DC, pay_c, step_seg=60)
    resumen_edge("DeMarker(17) 1m", flat_c, mart_c, pay_c, be_c)

    # ───────────────────────── ops/día combinado, payout>=80% ─────────────────────────
    print("\n" + "=" * 100)
    print("OPS/DÍA COMBINADO (los 3 edges, SIN martingala, payout>=80% todos)")
    print("=" * 100)
    for nombre, f, pay in (("Cripto ML", flat_a, pay_a), ("EURUSD-op ML", flat_b, pay_b),
                          ("DeMarker", flat_c, pay_c)):
        dias = f.dt.dt.normalize().nunique()
        print(f"  {nombre:<16} payout={100*pay:.0f}%  ops/día={len(f)/max(1,dias):.2f}  ({len(f):,} en {dias} días)")
    total_ops_dia = sum(len(f) / max(1, f.dt.dt.normalize().nunique()) for f in (flat_a, flat_b, flat_c))
    print(f"\n  TOTAL combinado (suma simple de tasas, los 3 edges): ~{total_ops_dia:.1f} ops/día")
    print(f"  Objetivo >=5 ops/día con payout>=80%: "
          f"{'CUMPLIDO largamente (cripto solo ya da ' + f'{len(flat_a)/max(1,flat_a.dt.dt.normalize().nunique()):.1f}' + '/día)' if total_ops_dia >= 5 else 'NO cumplido'}")

    # ───────────────────────── P&L diario combinado ─────────────────────────
    print("\n" + "=" * 100)
    print("DISTRIBUCIÓN DE P&L DIARIO — ¿cuántos días terminan en positivo?")
    print("(a $1 de monto base; martingala usa el monto base x multiplicador en cada paso)")
    print("=" * 100)

    print("\n--- SIN martingala ---")
    pnl_a = pnl_diario(flat_a)
    pnl_b = pnl_diario(flat_b)
    pnl_c = pnl_diario(flat_c)
    reporte_dias(pnl_a, "Solo cripto ML (2024-10 a 2026-09, ~2 años)")
    reporte_dias(pnl_b, "Solo EURUSD-op ML")
    reporte_dias(pnl_c, "Solo DeMarker (~2 meses, muestra chica)")

    combinado_ab = pnl_a.add(pnl_b, fill_value=0)
    reporte_dias(combinado_ab, "Cripto ML + EURUSD-op ML combinados (ventana larga)")

    combinado_abc = pnl_a.add(pnl_b, fill_value=0).add(pnl_c, fill_value=0)
    ventana_c = (pnl_c.index.min(), pnl_c.index.max()) if len(pnl_c) else (None, None)
    if ventana_c[0] is not None:
        solapa = combinado_abc.loc[ventana_c[0]:ventana_c[1]]
        reporte_dias(solapa, f"Los 3 edges combinados, SOLO en la ventana donde DeMarker tiene datos "
                              f"({ventana_c[0].date()} a {ventana_c[1].date()})")

    print("\n--- CON martingala (todas las señales que abren ciclo) ---")
    pnl_a_m = pnl_diario(mart_a) if len(mart_a) else pd.Series(dtype=float)
    pnl_b_m = pnl_diario(mart_b) if len(mart_b) else pd.Series(dtype=float)
    pnl_c_m = pnl_diario(mart_c) if len(mart_c) else pd.Series(dtype=float)
    reporte_dias(pnl_a_m, "Solo cripto ML")
    reporte_dias(pnl_b_m, "Solo EURUSD-op ML")
    reporte_dias(pnl_c_m, "Solo DeMarker")
    combinado_ab_m = pnl_a_m.add(pnl_b_m, fill_value=0)
    reporte_dias(combinado_ab_m, "Cripto ML + EURUSD-op ML combinados, CON martingala")

    print("\n" + "=" * 100)
    print("MARTINGALA vs SIN MARTINGALA — ¿cuál conviene? (comparación directa)")
    print("=" * 100)
    for nombre, pnl_flat, pnl_m in (("Cripto ML", pnl_a, pnl_a_m),
                                    ("EURUSD-op ML", pnl_b, pnl_b_m),
                                    ("DeMarker", pnl_c, pnl_c_m)):
        if len(pnl_flat) == 0 or len(pnl_m) == 0:
            continue
        pos_flat = 100 * (pnl_flat > 0).mean()
        pos_m = 100 * (pnl_m > 0).mean()
        print(f"  {nombre:<16} % días positivos SIN martingala={pos_flat:5.1f}%   "
              f"CON martingala={pos_m:5.1f}%   "
              f"std diario SIN={pnl_flat.std():6.3f}  CON={pnl_m.std():6.3f}")

    print("\n" + "=" * 100)
    print("VEREDICTO")
    print("=" * 100)
    print("  (ver tabla impresa arriba 'martingala MEJORA/EMPEORA el EV por $ arriesgado'")
    print("   para cada edge, y el % de días positivos con y sin martingala.)")


if __name__ == "__main__":
    main()
