import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Valida en 2,5 años de histórico las pistas que salieron de comparar
ganadas contra perdidas en vivo.

Lo que se encontró en vivo (1.484 señales, una sola semana de mercado bajista):
las CALL ganan cuando el precio está MÁS sobrevendido y las PUT cuando está
MÁS sobrecomprado. Es decir: el modelo acierta cuando su señal coincide con un
extremo de reversión a la media, y falla cuando la señal es "a medias".

Una semana es un solo régimen de mercado. Aquí se prueba en 2,5 años de forex
real a 15m, con los UMBRALES FIJADOS DESDE LOS DATOS EN VIVO — no se reajustan
al histórico, porque reajustarlos sería volver a buscar entre muchas opciones
y encontrar otra vez lo que el azar deje a mano.

Predicciones fuera de muestra: walk-forward en 8 bloques. El modelo nunca ve
el bloque que se evalúa.

    py validar_filtros_historico.py
"""
import math
import os
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import entrenar_modelo as E
import ml_walkforward as ML
import simulador as S

REALES = ["AUDUSD", "EURGBP", "EURJPY", "EURUSD", "GBPJPY",
          "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]
BE = 52.63
CACHE = "oos_15m_con_features.pkl"


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def oos():
    """Predicciones fuera de muestra CON las features de cada fila."""
    if os.path.exists(CACHE):
        return pd.read_pickle(CACHE)
    print("construyendo predicciones fuera de muestra (tarda unos minutos)...")
    M = E.preparar("velas.db", 900, (1, 17), REALES)
    feats = [f for f in ML.FEATURES if f in M.columns]
    X = M[feats].replace([np.inf, -np.inf], np.nan)
    X = X.fillna(X.median()).values
    y = M.sube.values
    cortes = np.linspace(0, len(X), 9).astype(int)
    partes = []
    for i in range(1, 8):
        trf, ti, tf_ = cortes[i], cortes[i], cortes[i + 1]
        if trf < 5000:
            continue
        m = ML.crear_modelo("gb")
        m.fit(X[:trf], y[:trf])
        sub = M.iloc[ti:tf_].copy()
        sub["p"] = m.predict_proba(X[ti:tf_])[:, 1]
        partes.append(sub)
        print(f"   bloque {i}: {len(sub):,} predicciones")
    D = pd.concat(partes)
    D.to_pickle(CACHE)
    return D


def main():
    D = oos()
    D["conf"] = (D.p - 0.5).abs()
    D["call"] = D.p > 0.5
    D["gano"] = np.where(D.call, D.sube, 1 - D.sube)
    dias = D.dt.dt.normalize().nunique()
    print(f"\n{len(D):,} predicciones fuera de muestra · {dias} días hábiles · "
          f"{D.activo.nunique()} pares · {D.dt.min():%Y-%m} a {D.dt.max():%Y-%m}")
    print(f"tasa de subida en el periodo: {100 * D.sube.mean():.2f}% "
          f"(equilibrado: no hay tendencia de fondo que regale una dirección)\n")

    # Reglas con umbrales sacados de los datos EN VIVO, no del histórico
    reglas = {
        "sin filtro (actual)":        pd.Series(True, index=D.index),
        "estocástico extremo":        np.where(D.call, D.stoch_k < 30, D.stoch_k > 70),
        "%B Bollinger extremo":       np.where(D.call, D.bb_pct < 0.2, D.bb_pct > 0.8),
        "CCI extremo":                np.where(D.call, D.cci20 < -100, D.cci20 > 100),
        "Williams %R extremo":        np.where(D.call, D.williams_r < -80, D.williams_r > -20),
        "RSI7 extremo":               np.where(D.call, D.rsi7 < 35, D.rsi7 > 65),
    }
    osc = sum(np.asarray(v, dtype=int) for k, v in list(reglas.items())[1:])
    reglas["≥2 osciladores extremos"] = osc >= 2
    reglas["≥3 osciladores extremos"] = osc >= 3
    reglas["hora 5-7 UTC"] = D.hora.between(5, 7)

    niveles = [("todas las señales", 1.00), ("50% más confiable", 0.50),
               ("10% más confiable", 0.10)]
    for nombre_nivel, pct in niveles:
        u = D.conf.quantile(1 - pct)
        base = D[D.conf >= u]
        print("=" * 96)
        print(f"{nombre_nivel.upper()}  (confianza ≥ {u:.4f})")
        print("=" * 96)
        print(f"  {'regla':<26}{'n':>9}{'WR':>8}{'IC95':>16}{'ops/día':>10}   veredicto")
        print("  " + "-" * 84)
        for r, mask in reglas.items():
            sel = base[np.asarray(mask)[D.index.get_indexer(base.index)]] \
                if not isinstance(mask, pd.Series) else base[mask.loc[base.index]]
            n, k = len(sel), int(sel.gano.sum())
            if n < 200:
                continue
            lo, hi = wilson(k, n)
            v = "RENTABLE" if lo > BE else ("sobre BE" if 100 * k / n > BE else "no")
            print(f"  {r:<26}{n:>9,}{100 * k / n:>7.2f}%  [{lo:5.2f},{hi:5.2f}]"
                  f"{n / dias:>10.1f}   {v}")
        print()

    # estabilidad temporal de la mejor combinación
    print("=" * 96)
    print("ESTABILIDAD: ≥2 osciladores extremos + 50% más confiable, año a año")
    print("=" * 96)
    u = D.conf.quantile(0.5)
    sel = D[(D.conf >= u) & (osc >= 2)]
    for anio, g in sel.groupby(sel.dt.dt.year):
        n, k = len(g), int(g.gano.sum())
        lo, hi = wilson(k, n)
        print(f"  {anio}   n={n:>6,}   {100 * k / n:6.2f}%   [{lo:5.2f},{hi:5.2f}]"
              f"{'   sobre BE' if lo > BE else ''}")


if __name__ == "__main__":
    main()

