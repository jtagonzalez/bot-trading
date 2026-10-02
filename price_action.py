"""
PRICE ACTION — decidir mirando la FORMA CRUDA de las ultimas N velas.

En lugar de darle al modelo indicadores ya digeridos (RSI, Stoch, MACD...), se le
da el dibujo entero: el OHLC de las ultimas N velas, normalizado.

Por que puede funcionar mejor: un indicador es un RESUMEN CON PERDIDA de esas
mismas velas. El RSI convierte 14 velas en un numero; la ventana cruda conserva
todo (donde estaban las mechas, si el movimiento fue de golpe o escalonado, si
hubo un rechazo...). El modelo puede encontrar patrones que ningun indicador
captura.

Por que puede funcionar peor: 80 numeros en lugar de 25 es mas sitio donde
sobreajustar.

Normalizacion (esto es lo que hace que funcione entre activos y epocas):
  * todo se mide respecto al CIERRE de la ultima vela de la ventana
  * se divide por la volatilidad de la propia ventana
  -> el modelo ve FORMAS, no precios. Un patron en EURUSD a 1.05 es comparable
     con el mismo patron en GBPJPY a 190.

Uso:
    py price_action.py --tf 30m --velas 20
    py price_action.py --tf 30m --velas 15 --horas 20-23
    py price_action.py --tf 15m --velas 20 --comparar
"""
import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import ml_walkforward as ML
import simulador as S

PAYOUT = 0.84
BREAKEVEN = 100 / (1 + PAYOUT)


def ventana_cruda(df, n_velas):
    """Convierte cada vela en un vector con la forma de las n_velas anteriores.

    Devuelve una matriz (filas, 4*n_velas). Cada bloque de 4 es el OHLC de una
    vela pasada, medido desde el cierre actual y escalado por la volatilidad de
    la ventana.
    """
    o, h, l, c = (df.open.values, df.high.values, df.low.values, df.close.values)
    N = len(df)
    # escala: desviacion de los retornos en la ventana (evita dividir por cero)
    ret = np.diff(c, prepend=c[0]) / np.maximum(c, 1e-12)
    escala = pd.Series(ret).rolling(n_velas).std().values * np.maximum(c, 1e-12)
    escala = np.where((escala <= 0) | ~np.isfinite(escala), np.nan, escala)

    cols = []
    for k in range(n_velas - 1, -1, -1):          # de la mas antigua a la actual
        for serie, nombre in ((o, "o"), (h, "h"), (l, "l"), (c, "c")):
            desp = np.full(N, np.nan)
            if k == 0:
                desp = serie
            else:
                desp[k:] = serie[:-k]
            cols.append(((desp - c) / escala, f"{nombre}_{k}"))
    X = np.column_stack([v for v, _ in cols])
    nombres = [n for _, n in cols]
    return X, nombres


def construir(db, tf_seg, n_velas, horas=None, con_indicadores=True):
    activos = S.activos_disponibles(db, tf_seg)
    Xs, ys, metas = [], [], []
    for a in activos:
        df = S.cargar_tf(db, a, tf_seg)
        if len(df) < 500:
            continue
        df = S.indicadores(df)
        # las mismas features derivadas que usa ml_walkforward, para comparar de igual a igual
        c_ = df.close
        for k in (1, 2, 3, 5, 10, 20):
            df[f"ret{k}"] = c_.pct_change(k) * 100
        df["vol20"] = c_.pct_change().rolling(20).std() * 100
        df["vol_rel"] = df.vol20 / df.vol20.rolling(100).mean()
        df["rango_rel"] = (df.high - df.low) / c_ * 100
        df["cierre_en_rango"] = (c_ - df.low) / (df.high - df.low).replace(0, np.nan)

        Xpa, nom_pa = ventana_cruda(df, n_velas)

        ok = df.contigua.shift(-1).fillna(False).values & (df.dow.values < 5)
        if horas is not None:
            h0, h1 = horas
            ok = ok & (df.hora.values >= h0) & (df.hora.values <= h1)
        nxt = df.close.shift(-1).values
        ok = ok & np.isfinite(nxt) & (nxt != df.close.values)
        ok = ok & np.isfinite(Xpa).all(axis=1)

        if con_indicadores:
            Xi = df[ML.FEATURES].replace([np.inf, -np.inf], np.nan).values
            ok = ok & np.isfinite(Xi).all(axis=1)
        idx = np.where(ok)[0]
        if len(idx) == 0:
            continue

        Xs.append((Xpa[idx], Xi[idx] if con_indicadores else None))
        ys.append((nxt[idx] > df.close.values[idx]).astype(int))
        metas.append(pd.DataFrame({
            "ts": df.ts.values[idx], "dt": df.dt.values[idx],
            "activo": a, "hora": df.hora.values[idx]}))
    if not Xs:
        return None
    Xpa = np.vstack([x[0] for x in Xs])
    Xi = np.vstack([x[1] for x in Xs]) if con_indicadores else None
    y = np.concatenate(ys)
    meta = pd.concat(metas, ignore_index=True)
    orden = np.argsort(meta.ts.values, kind="stable")
    return (Xpa[orden], (Xi[orden] if Xi is not None else None),
            y[orden], meta.iloc[orden].reset_index(drop=True), nom_pa)


def walk_forward(X, y, bloques=10, pct=0.10):
    """Entrena solo con el pasado; umbral de confianza fijado en train."""
    cortes = np.linspace(0, len(X), bloques + 1).astype(int)
    P, Y, B = [], [], []
    for i in range(1, bloques):
        trf, ti, tf = cortes[i], cortes[i], cortes[i + 1]
        if trf < 3000 or tf - ti < 300:
            continue
        m = ML.crear_modelo("gb")
        m.fit(X[:trf], y[:trf])
        ptr = m.predict_proba(X[:trf])[:, 1]
        u = np.quantile(np.abs(ptr - 0.5), 1 - pct)
        pte = m.predict_proba(X[ti:tf])[:, 1]
        P.append(pte)
        Y.append(y[ti:tf])
        B.append(np.full(tf - ti, i))
        walk_forward.umbral = u
    if not P:
        return None
    return np.concatenate(P), np.concatenate(Y), np.concatenate(B)


def evaluar(p, y, pct=0.10, etiqueta=""):
    conf = np.abs(p - 0.5)
    ac = np.where(p > 0.5, y, 1 - y)
    k_n = max(100, int(len(p) * pct))
    idx = np.argsort(conf)[-k_n:]
    a = ac[idx]
    n, k = len(a), int(a.sum())
    lo, hi = S.wilson(k, n)
    n2, k2 = len(ac), int(ac.sum())
    lo2, hi2 = S.wilson(k2, n2)
    return {"etiqueta": etiqueta, "n_todo": n2, "wr_todo": 100 * k2 / n2,
            "n": n, "wr": 100 * k / n, "lo": lo, "hi": hi,
            "ev": (k / n) * PAYOUT - (1 - k / n), "rentable": lo > BREAKEVEN}


def fila(r):
    v = "RENTABLE" if r["rentable"] else "no"
    return (f"  {r['etiqueta']:<34}{r['wr_todo']:>8.2f}%{r['n']:>9,}{r['wr']:>8.2f}%"
            f"  [{r['lo']:5.2f},{r['hi']:5.2f}]{r['ev']:>+9.4f}  {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="velas.db")
    ap.add_argument("--tf", default="30m", choices=list(S.TFS))
    ap.add_argument("--velas", type=int, default=20, help="cuantas velas mira hacia atras")
    ap.add_argument("--horas", default=None, help="franja UTC, ej: 20-23")
    ap.add_argument("--pct", type=float, default=0.10, help="fraccion mas confiable que opera")
    ap.add_argument("--bloques", type=int, default=10)
    ap.add_argument("--comparar", action="store_true",
                    help="compara price action vs indicadores vs ambos")
    args = ap.parse_args()

    horas = None
    if args.horas:
        a, b = args.horas.split("-")
        horas = (int(a), int(b))

    print(f"Construyendo ventanas de {args.velas} velas en {args.tf}"
          + (f", horas {args.horas} UTC" if horas else "") + " ...")
    D = construir(args.db, S.TFS[args.tf], args.velas, horas, con_indicadores=True)
    if D is None:
        raise SystemExit("sin datos")
    Xpa, Xi, y, meta, nom = D
    print(f"  {len(y):,} muestras  |  {meta.activo.nunique()} activos  |  "
          f"{pd.to_datetime(meta.dt.min()):%Y-%m} a {pd.to_datetime(meta.dt.max()):%Y-%m}")
    print(f"  price action: {Xpa.shape[1]} features   indicadores: {Xi.shape[1]} features")
    print(f"  breakeven {BREAKEVEN:.2f}%  |  opera el {100 * args.pct:.0f}% mas confiable\n")

    juegos = [("price action (forma cruda)", Xpa)]
    if args.comparar:
        juegos.append(("indicadores clasicos", Xi))
        juegos.append(("ambos combinados", np.hstack([Xpa, Xi])))

    print(f"  {'representacion':<34}{'WR todo':>9}{'n sel':>9}{'WR sel':>8}"
          f"{'IC95':>16}{'EV':>9}  veredicto")
    print("  " + "-" * 96)
    for nombre, X in juegos:
        r = walk_forward(X, y, args.bloques, args.pct)
        if r is None:
            print(f"  {nombre:<34}  muestra insuficiente")
            continue
        p, yy, bb = r
        print(fila(evaluar(p, yy, args.pct, nombre)))

    print(f"\n  'WR todo' = acierto operando TODAS las velas (sin esperar condiciones).")
    print(f"  'WR sel'  = acierto operando solo el {100 * args.pct:.0f}% donde el modelo mas confia.")


if __name__ == "__main__":
    main()
