"""
TRADING ALGORITMICO — modelos de ML con reentrenamiento progresivo (walk-forward).

Responde dos preguntas distintas:

  1. ?Un modelo de ML encuentra senal donde las reglas simples no llegaron?
  2. ?Puede MEJORAR PROGRESIVAMENTE reentrenandose con datos nuevos?

La segunda es la clave y se mide asi: el modelo se entrena solo con el pasado,
predice el siguiente bloque, y despues se reentrena incluyendo ese bloque. Nunca
ve el futuro. Es exactamente como operaria en vivo.

LA APUESTA REAL: aunque el acierto medio sea 53%, quizas el modelo sepa
DISTINGUIR en que operaciones confia. Si al quedarse solo con el 10% de mayor
confianza el acierto sube por encima del 54.35%, hay negocio. Eso es lo que mide
la tabla de SELECTIVIDAD, y es la parte mas importante del informe.

Uso:
    py ml_walkforward.py --tf 30m
    py ml_walkforward.py --tf 15m --modelo gb --bloques 12
    py ml_walkforward.py --tf 30m --solo-senales   # solo velas con RSI/Stoch extremo
"""
import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import simulador as S

PAYOUT = 0.84
BREAKEVEN = 100 / (1 + PAYOUT)


def construir(db, tf_seg, solo_senales=False):
    """Una fila por vela operable, con features y la direccion real de la siguiente."""
    activos = S.activos_disponibles(db, tf_seg)
    out = []
    for a in activos:
        df = S.cargar_tf(db, a, tf_seg)
        if len(df) < 500:
            continue
        df = S.indicadores(df)

        c = df.close
        # retornos pasados: lo unico que el modelo puede saber en el momento de entrar
        for k in (1, 2, 3, 5, 10, 20):
            df[f"ret{k}"] = c.pct_change(k) * 100
        df["vol20"] = c.pct_change().rolling(20).std() * 100
        df["vol_rel"] = df.vol20 / df.vol20.rolling(100).mean()
        df["rango_rel"] = (df.high - df.low) / c * 100
        # posicion del cierre dentro del rango de la vela
        df["cierre_en_rango"] = (c - df.low) / (df.high - df.low).replace(0, np.nan)

        ok = df.contigua.shift(-1).fillna(False) & (df.dow < 5)
        if solo_senales:
            ok = ok & (((df.rsi7 < 30) & (df.stoch_k < 10)) |
                       ((df.rsi7 > 70) & (df.stoch_k > 90)))
        nxt = c.shift(-1)
        sub = df[ok].copy()
        n2 = nxt[ok]
        vivo = (n2 != sub.close)          # los empates devuelven la apuesta
        sub = sub[vivo]
        sub["sube"] = (n2[vivo] > sub.close).astype(int)
        sub["activo"] = a
        out.append(sub)
    if not out:
        return pd.DataFrame()
    return pd.concat(out).sort_values("ts").reset_index(drop=True)


FEATURES = [
    "rsi7", "rsi14", "rsi21", "stoch_k", "stoch_d", "williams_r", "cci20",
    "adx14", "plus_di", "minus_di", "macd_hist", "bb_pct", "bb_width",
    "atr_pct", "roc5", "roc10", "body_ratio", "wick_sup", "wick_inf",
    "dist_ema9", "dist_ema21", "dist_sma50", "racha", "hora", "dow",
    "ret1", "ret2", "ret3", "ret5", "ret10", "ret20",
    "vol20", "vol_rel", "rango_rel", "cierre_en_rango",
]


def crear_modelo(nombre):
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if nombre == "gb":
        return HistGradientBoostingClassifier(
            max_iter=300, max_depth=5, learning_rate=0.05,
            min_samples_leaf=200, l2_regularization=1.0, random_state=0)
    if nombre == "rf":
        return RandomForestClassifier(
            n_estimators=300, min_samples_leaf=100, max_features="sqrt",
            n_jobs=-1, random_state=0)
    return make_pipeline(StandardScaler(),
                         LogisticRegression(max_iter=2000, C=0.05))


def wilson(k, n):
    return S.wilson(k, n)


def walk_forward(M, modelo_nombre, bloques=10):
    """Entrena solo con el pasado, predice el bloque siguiente. Nunca ve el futuro."""
    X = M[FEATURES].replace([np.inf, -np.inf], np.nan)
    med = X.median()
    X = X.fillna(med).values
    y = M.sube.values

    cortes = np.linspace(0, len(M), bloques + 1).astype(int)
    filas, preds, reales = [], [], []
    for i in range(1, bloques):
        tr_fin = cortes[i]
        te_ini, te_fin = cortes[i], cortes[i + 1]
        if tr_fin < 2000 or te_fin - te_ini < 200:
            continue
        mdl = crear_modelo(modelo_nombre)
        mdl.fit(X[:tr_fin], y[:tr_fin])
        p = mdl.predict_proba(X[te_ini:te_fin])[:, 1]
        yt = y[te_ini:te_fin]

        # el modelo predice direccion; operamos call si p>0.5, put si p<0.5
        acierto = np.where(p > 0.5, yt, 1 - yt)
        n, k = len(acierto), int(acierto.sum())
        lo, hi = wilson(k, n)
        filas.append({
            "bloque": i, "n_train": tr_fin, "n_test": n,
            "wr": 100 * k / n, "lo": lo, "hi": hi,
            "desde": M.dt.iloc[te_ini], "hasta": M.dt.iloc[te_fin - 1],
        })
        preds.append(p)
        reales.append(yt)
    return pd.DataFrame(filas), np.concatenate(preds), np.concatenate(reales)


def selectividad(p, y):
    """?Acierta mas cuando esta mas seguro? Esta es la tabla que decide todo."""
    conf = np.abs(p - 0.5)              # distancia a la indecision
    acierto = np.where(p > 0.5, y, 1 - y)
    filas = []
    for pct in (100, 50, 25, 10, 5, 2, 1):
        k_n = max(50, int(len(p) * pct / 100))
        idx = np.argsort(conf)[-k_n:]   # las mas confiadas
        a = acierto[idx]
        n, k = len(a), int(a.sum())
        lo, hi = wilson(k, n)
        filas.append({"pct": pct, "n": n, "wr": 100 * k / n, "lo": lo, "hi": hi,
                      "rentable": lo > BREAKEVEN})
    return pd.DataFrame(filas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="velas.db")
    ap.add_argument("--tf", default="30m", choices=list(S.TFS))
    ap.add_argument("--modelo", default="gb", choices=["gb", "rf", "lr"])
    ap.add_argument("--bloques", type=int, default=10)
    ap.add_argument("--solo-senales", action="store_true",
                    help="solo velas con RSI/Stoch en extremo (la regla que ya tiene edge)")
    args = ap.parse_args()

    seg = S.TFS[args.tf]
    print(f"Construyendo dataset {args.tf}...")
    M = construir(args.db, seg, args.solo_senales)
    if M.empty:
        raise SystemExit("sin datos")
    M = M.dropna(subset=[f for f in FEATURES if f in M.columns])
    print(f"  {len(M):,} muestras  |  {M.activo.nunique()} activos  |  "
          f"{M.dt.min():%Y-%m-%d} -> {M.dt.max():%Y-%m-%d}")
    print(f"  tasa base (sube): {100 * M.sube.mean():.2f}%   breakeven: {BREAKEVEN:.2f}%\n")

    print(f"{'=' * 88}")
    print(f"WALK-FORWARD — modelo '{args.modelo}', {args.bloques} bloques")
    print("El modelo se reentrena en cada bloque con TODO el pasado disponible.")
    print(f"{'=' * 88}")
    R, p, y = walk_forward(M, args.modelo, args.bloques)
    if R.empty:
        raise SystemExit("muestra insuficiente para walk-forward")

    print(f"\n  {'bloque':>7}{'entrena con':>13}{'opera':>9}{'WR':>8}{'IC95':>16}  periodo")
    print("  " + "-" * 80)
    for _, r in R.iterrows():
        print(f"  {int(r.bloque):>7}{int(r.n_train):>13,}{int(r.n_test):>9,}"
              f"{r.wr:>7.2f}%  [{r.lo:5.2f},{r.hi:5.2f}]  "
              f"{r.desde:%Y-%m-%d} a {r.hasta:%Y-%m-%d}")

    acierto = np.where(p > 0.5, y, 1 - y)
    n, k = len(acierto), int(acierto.sum())
    lo, hi = wilson(k, n)
    print(f"\n  GLOBAL out-of-sample: n={n:,}  WR={100 * k / n:.2f}%  IC95=[{lo:.2f},{hi:.2f}]")

    # ?mejora con mas datos? correlacion entre tamano de train y acierto
    if len(R) >= 4:
        c = np.corrcoef(R.n_train, R.wr)[0, 1]
        print(f"\n  ?MEJORA PROGRESIVAMENTE?")
        print(f"    correlacion (datos de entrenamiento vs acierto) = {c:+.3f}")
        prim, ult = R.wr.iloc[:len(R) // 2].mean(), R.wr.iloc[len(R) // 2:].mean()
        print(f"    primera mitad: {prim:.2f}%   segunda mitad: {ult:.2f}%   "
              f"diferencia: {ult - prim:+.2f} pts")
        if c > 0.3 and ult > prim:
            print("    -> hay indicio de mejora con mas datos")
        else:
            print("    -> NO mejora: mas datos historicos no aumentan el acierto")

    print(f"\n{'=' * 88}")
    print("SELECTIVIDAD — ?acierta mas cuando el modelo esta mas seguro?")
    print("Esta es la tabla decisiva: operar MENOS pero MEJOR.")
    print(f"{'=' * 88}")
    Sel = selectividad(p, y)
    print(f"\n  {'opera el':>10}{'n':>9}{'WR':>8}{'IC95':>16}  veredicto")
    print("  " + "-" * 60)
    for _, r in Sel.iterrows():
        v = "RENTABLE" if r.rentable else ("sobre BE (dudoso)" if r.wr > BREAKEVEN else "no")
        print(f"  {int(r.pct):>8}%{int(r.n):>10,}{r.wr:>7.2f}%  [{r.lo:5.2f},{r.hi:5.2f}]  {v}")

    print(f"\n{'=' * 88}")
    print("VEREDICTO")
    print(f"{'=' * 88}")
    ok = Sel[Sel.rentable]
    if len(ok):
        print("  El modelo SI identifica un subconjunto rentable:")
        for _, r in ok.iterrows():
            ops_dia = int(r.n) / max(1, (M.dt.max() - M.dt.min()).days)
            print(f"    operando el {int(r.pct)}% mas confiable: WR={r.wr:.2f}%  "
                  f"n={int(r.n):,}  (~{ops_dia:.1f} operaciones/dia)")
        print("\n  Antes de operarlo: confirmar en PRACTICE el tiempo necesario")
        print("  para acumular n suficiente al ritmo real de senales.")
    else:
        mejor = Sel.loc[Sel.wr.idxmax()]
        print(f"  El modelo NO encuentra ningun subconjunto rentable.")
        print(f"  Mejor caso: operando el {int(mejor.pct)}% mas confiable -> "
              f"{mejor.wr:.2f}% (IC95 bajo {mejor.lo:.2f}%, hace falta {BREAKEVEN:.2f}%)")
        print(f"\n  Si el WR no sube al filtrar por confianza, el modelo no sabe")
        print(f"  cuando confiar: su 'seguridad' no guarda relacion con acertar.")


if __name__ == "__main__":
    main()
