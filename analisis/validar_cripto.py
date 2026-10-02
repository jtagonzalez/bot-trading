import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
¿Hay ventaja operable en BTCUSD-op y ETHUSD-op a 15 minutos?

Motivación: Kitron & Wengrowicz (arXiv 2608.21888, ago-2026) miden reversión a
la media a 15 min en el 90% de 183 pares cripto, estable desde 2021, y señalan
que "apostar contra la vela anterior captura la mayor parte del efecto". En
contado la ventaja (1,3 pb) no cubre costos; en binarias solo cuenta la
dirección, así que la pregunta es otra: ¿supera el breakeven de 53,48% (pago 87%)?

La cuenta opera BTCUSD-op y ETHUSD-op 24h x 7 días en binarias de 15 min.

Se prueban, siempre fuera de muestra:
  1. La regla del artículo: contra la vela anterior.
  2. La misma regla solo tras movimientos grandes (el artículo dice que la
     reversión crece con la intensidad del movimiento). El umbral de "grande"
     se fija con la primera mitad de los datos y se evalúa en la segunda.
  3. Nuestro modelo (gradient boosting, 35 variables), walk-forward en 8
     bloques, operando el 10/20/50% más confiable con umbral de entrenamiento.

Cada resultado se juzga con: breakeven 53,48%, corrección BH sobre todas las
pruebas, y estabilidad en dos mitades.

    py validar_cripto.py
"""
import math
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

import ml_walkforward as ML
import simulador as S

CRIPTO = ["BTCUSD", "ETHUSD"]
PAYOUT = 0.87
BE = 100 / (1 + PAYOUT)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def preparar(activo):
    """Igual que entrenar_modelo.preparar pero SIN excluir fines de semana."""
    df = S.cargar_tf("velas.db", activo, 900)
    df = S.indicadores(df)
    c = df.close
    for k in (1, 2, 3, 5, 10, 20):
        df[f"ret{k}"] = c.pct_change(k) * 100
    df["vol20"] = c.pct_change().rolling(20).std() * 100
    df["vol_rel"] = df.vol20 / df.vol20.rolling(100).mean()
    df["rango_rel"] = (df.high - df.low) / c * 100
    df["cierre_en_rango"] = (c - df.low) / (df.high - df.low).replace(0, np.nan)
    nxt = c.shift(-1)
    ok = df.contigua.shift(-1).fillna(False) & nxt.notna() & (nxt != c)
    sub = df[ok].copy()
    sub["sube"] = (nxt[ok] > sub.close).astype(int)
    sub["activo"] = activo
    return sub


def resultado(nombre, g, dias, filas):
    n, k = len(g), int(g.gano.sum())
    if n < 100:
        return
    mitad = g.dt.quantile(0.5)
    w1 = 100 * g[g.dt <= mitad].gano.mean()
    w2 = 100 * g[g.dt > mitad].gano.mean()
    pv = stats.binomtest(k, n, BE / 100, alternative="greater").pvalue
    ev = (k / n) * PAYOUT - (1 - k / n)
    filas.append(dict(prueba=nombre, n=n, wr=100 * k / n, ic=wilson(k, n),
                      w1=w1, w2=w2, p=pv, ops=n / dias, dia=ev * n / dias))


def main():
    M = pd.concat([preparar(a) for a in CRIPTO]).sort_values("ts").reset_index(drop=True)
    feats = [f for f in ML.FEATURES if f in M.columns]
    M = M.dropna(subset=feats + ["ret1"]).reset_index(drop=True)
    dias = M.dt.dt.normalize().nunique()
    print(f"{len(M):,} velas · {', '.join(CRIPTO)} · {M.dt.min():%Y-%m-%d} a "
          f"{M.dt.max():%Y-%m-%d} · {dias} días (24x7)")
    print(f"tasa de subida: {100 * M.sube.mean():.2f}%   breakeven (pago 87%): {BE:.2f}%\n")

    filas = []
    # 1-2. la regla del articulo, en la segunda mitad (la primera fija umbrales)
    corte = M.dt.quantile(0.5)
    A, B = M[M.dt <= corte], M[M.dt > corte].copy()
    B["gano"] = ((B.ret1 > 0) & (B.sube == 0)) | ((B.ret1 < 0) & (B.sube == 1))
    B = B[B.ret1 != 0]
    dias_b = B.dt.dt.normalize().nunique()
    for a in CRIPTO:
        resultado(f"{a} contra vela anterior", B[B.activo == a], dias_b, filas)
        mag_a = A.loc[A.activo == a, "ret1"].abs()
        for q in (0.5, 0.8, 0.9):
            u = mag_a.quantile(q)
            g = B[(B.activo == a) & (B.ret1.abs() >= u)]
            resultado(f"{a} contra vela, mov. top {100 - 100 * q:.0f}%", g, dias_b, filas)

    # 3. nuestro modelo, walk-forward
    X = M[feats].replace([np.inf, -np.inf], np.nan)
    X = X.fillna(X.median()).values
    y = M.sube.values
    cortes = np.linspace(0, len(X), 9).astype(int)
    partes = []
    for i in range(1, 8):
        m = ML.crear_modelo("gb")
        m.fit(X[:cortes[i]], y[:cortes[i]])
        conf_tr = np.abs(m.predict_proba(X[:cortes[i]])[:, 1] - 0.5)
        s = M.iloc[cortes[i]:cortes[i + 1]][["activo", "dt", "sube"]].copy()
        s["p"] = m.predict_proba(X[cortes[i]:cortes[i + 1]])[:, 1]
        for pct in (10, 20, 50):
            s[f"op{pct}"] = np.abs(s.p - 0.5) >= np.quantile(conf_tr, 1 - pct / 100)
        partes.append(s)
        print(f"  bloque modelo {i}/7 listo")
    D = pd.concat(partes)
    D["gano"] = np.where(D.p > 0.5, D.sube, 1 - D.sube)
    D.to_pickle("oos_cripto.pkl")
    dias_d = D.dt.dt.normalize().nunique()
    for a in CRIPTO:
        for pct in (10, 20, 50):
            resultado(f"{a} modelo {pct}% más confiable",
                      D[(D.activo == a) & D[f"op{pct}"]], dias_d, filas)

    R = pd.DataFrame(filas)
    p = R.p.values
    o = np.argsort(p)
    q = np.empty(len(p))
    prev = 1.0
    for r in range(len(p), 0, -1):
        j = o[r - 1]
        prev = min(prev, p[j] * len(p) / r)
        q[j] = prev
    R["q"] = q

    print("\n" + "=" * 106)
    print(f"RESULTADOS FUERA DE MUESTRA · breakeven {BE:.2f}%")
    print("=" * 106)
    print(f"  {'prueba':<36}{'n':>7}{'WR':>8}{'IC95':>16}{'1a':>7}{'2a':>7}"
          f"{'q(BH)':>8}{'ops/día':>9}{'$/día':>8}")
    for _, r in R.iterrows():
        pasa = r.q < 0.05 and r.w1 > BE and r.w2 > BE
        print(f"  {r.prueba:<36}{r.n:>7,}{r.wr:>7.2f}%  [{r.ic[0]:5.2f},{r.ic[1]:5.2f}]"
              f"{r.w1:>6.1f}%{r.w2:>6.1f}%{r.q:>8.3f}{r.ops:>9.1f}{r.dia:>8.2f}"
              f"{'  PASA' if pasa else ''}")


if __name__ == "__main__":
    main()

