"""
ENTRENADOR — genera el modelo que operara en vivo y lo deja guardado.

Entrena con todo el historico disponible y guarda en un solo archivo:
  * el modelo
  * el UMBRAL de confianza, calculado sobre los datos de entrenamiento
    (nunca sobre datos futuros: asi es como funcionara en vivo)
  * la lista exacta de features y su orden
  * las medianas para rellenar huecos
  * metadatos: hash, fecha de entrenamiento, rendimiento esperado

El hash identifica la version exacta. Queda grabado en cada run del registro,
asi que siempre se puede saber que modelo genero que operaciones.

Uso:
    py entrenar_modelo.py                       # 30m, horas 20-23, pct 10%
    py entrenar_modelo.py --tf 15m --pct 0.05
    py entrenar_modelo.py --validar             # ademas, walk-forward de control
"""
import argparse
import hashlib
import io
import json
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import ml_walkforward as ML
import simulador as S

SALIDA_DEFECTO = "modelo_live.joblib"


def preparar(db, tf_seg, horas, activos=None, incluir_finde=False):
    """Construye la matriz de entrenamiento igual que lo hara el bot en vivo."""
    filas = []
    lista = activos if activos else S.activos_disponibles(db, tf_seg)
    for a in lista:
        df = S.cargar_tf(db, a, tf_seg)
        if len(df) < 500:
            continue
        df = S.indicadores(df)
        c = df.close
        for k in (1, 2, 3, 5, 10, 20):
            df[f"ret{k}"] = c.pct_change(k) * 100
        df["vol20"] = c.pct_change().rolling(20).std() * 100
        df["vol_rel"] = df.vol20 / df.vol20.rolling(100).mean()
        df["rango_rel"] = (df.high - df.low) / c * 100
        df["cierre_en_rango"] = (c - df.low) / (df.high - df.low).replace(0, np.nan)

        # el forex cierra el fin de semana; cripto cotiza 24x7
        ok = df.contigua.shift(-1).fillna(False)
        if not incluir_finde:
            ok = ok & (df.dow < 5)
        if horas:
            ok = ok & (df.hora >= horas[0]) & (df.hora <= horas[1])
        nxt = c.shift(-1)
        ok = ok & nxt.notna() & (nxt != c)
        sub = df[ok].copy()
        sub["sube"] = (nxt[ok] > sub.close).astype(int)
        sub["activo"] = a
        filas.append(sub)
    if not filas:
        return None
    M = pd.concat(filas).sort_values("ts").reset_index(drop=True)
    return M.dropna(subset=[f for f in ML.FEATURES if f in M.columns]).reset_index(drop=True)


def validar(X, y, pct, bloques=8):
    """Walk-forward de control: confirma que el modelo que vamos a usar rinde."""
    cortes = np.linspace(0, len(X), bloques + 1).astype(int)
    P, Y = [], []
    for i in range(1, bloques):
        trf, ti, tf = cortes[i], cortes[i], cortes[i + 1]
        if trf < 3000 or tf - ti < 200:
            continue
        m = ML.crear_modelo("gb")
        m.fit(X[:trf], y[:trf])
        u = np.quantile(np.abs(m.predict_proba(X[:trf])[:, 1] - 0.5), 1 - pct)
        p = m.predict_proba(X[ti:tf])[:, 1]
        sel = np.abs(p - 0.5) >= u
        if sel.sum() < 20:
            continue
        P.append(p[sel])
        Y.append(y[ti:tf][sel])
    if not P:
        return None
    p, yy = np.concatenate(P), np.concatenate(Y)
    ac = np.where(p > 0.5, yy, 1 - yy)
    n, k = len(ac), int(ac.sum())
    lo, hi = S.wilson(k, n)
    return {"n": n, "wr": 100 * k / n, "lo": lo, "hi": hi}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="velas.db")
    ap.add_argument("--tf", default="30m", choices=list(S.TFS))
    ap.add_argument("--horas", default="20-23", help="franja UTC o 'todas'")
    ap.add_argument("--pct", type=float, default=0.10,
                    help="fraccion mas confiable que se operara")
    ap.add_argument("--salida", default=SALIDA_DEFECTO)
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--activos", default=None,
                    help="lista separada por comas; por defecto todos los del timeframe")
    ap.add_argument("--operables", default=None,
                    help="usar los activos verificados de activos_operables.json")
    ap.add_argument("--fines-de-semana", action="store_true",
                    help="incluir sabados y domingos (activos 24x7 como cripto)")
    args = ap.parse_args()

    horas = None if args.horas == "todas" else tuple(int(x) for x in args.horas.split("-"))
    tf_seg = S.TFS[args.tf]

    print(f"Preparando datos {args.tf}" + (f", horas {args.horas} UTC" if horas else "") + " ...")
    activos = None
    if args.operables:
        d = json.loads(io.open(args.operables, encoding="utf-8").read())
        activos = d["verificados"]
        print(f"  usando {len(activos)} activos verificados como operables")
    elif args.activos:
        activos = [a.strip() for a in args.activos.split(",")]
    M = preparar(args.db, tf_seg, horas, activos, args.fines_de_semana)
    if M is None or len(M) < 5000:
        raise SystemExit("datos insuficientes para entrenar")

    feats = [f for f in ML.FEATURES if f in M.columns]
    X = M[feats].replace([np.inf, -np.inf], np.nan)
    medianas = X.median()
    X = X.fillna(medianas).values
    y = M.sube.values
    print(f"  {len(M):,} muestras  |  {M.activo.nunique()} activos  |  {len(feats)} features")
    print(f"  periodo: {M.dt.min():%Y-%m-%d} a {M.dt.max():%Y-%m-%d}")

    esperado = None
    if args.validar:
        print("\nValidacion walk-forward de control (esto tarda)...")
        esperado = validar(X, y, args.pct)
        if esperado:
            print(f"  rendimiento esperado: n={esperado['n']:,}  WR={esperado['wr']:.2f}%  "
                  f"IC95=[{esperado['lo']:.2f},{esperado['hi']:.2f}]")
            be = 100 / 1.84
            if esperado["lo"] <= be:
                print(f"  AVISO: el limite bajo del IC95 no supera el breakeven ({be:.2f}%).")
                print(f"  El modelo se guarda igualmente, pero NO operar con dinero real.")

    print(f"\nEntrenando modelo final con las {len(M):,} muestras...")
    modelo = ML.crear_modelo("gb")
    modelo.fit(X, y)
    p_train = modelo.predict_proba(X)[:, 1]
    umbral = float(np.quantile(np.abs(p_train - 0.5), 1 - args.pct))
    print(f"  umbral de confianza (percentil {100 * (1 - args.pct):.0f} del train): {umbral:.6f}")
    sel = np.abs(p_train - 0.5) >= umbral
    print(f"  en train seleccionaria {sel.sum():,} de {len(X):,} muestras "
          f"({100 * sel.mean():.1f}%)")

    meta = {
        "creado": datetime.now(timezone.utc).isoformat(),
        "timeframe": tf_seg,
        "timeframe_nombre": args.tf,
        "horas": args.horas,
        "pct_operado": args.pct,
        "umbral": umbral,
        "features": feats,
        "n_entrenamiento": int(len(M)),
        "activos": sorted(M.activo.unique().tolist()),
        "datos_hasta": str(M.dt.max()),
        "wr_esperado": esperado["wr"] if esperado else None,
        "ic95_esperado": [esperado["lo"], esperado["hi"]] if esperado else None,
    }
    meta["hash"] = hashlib.sha256(
        json.dumps({k: v for k, v in meta.items() if k != "hash"},
                   sort_keys=True, default=str).encode()).hexdigest()[:12]

    import joblib
    joblib.dump({"modelo": modelo, "medianas": medianas, "meta": meta}, args.salida)
    print(f"\nGuardado en {args.salida}")
    print(f"  hash: {meta['hash']}   (queda grabado en cada run del registro)")
    print(f"  datos hasta: {meta['datos_hasta']}")
    if esperado:
        print(f"  WR esperado en vivo: {esperado['wr']:.2f}%")
    print(f"\nPara reentrenar mas adelante: vuelve a correr este script; el hash")
    print(f"cambiara y podras comparar los runs de cada version por separado.")


if __name__ == "__main__":
    main()
