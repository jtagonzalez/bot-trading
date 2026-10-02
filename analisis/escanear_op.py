import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
?HAY EDGE DENTRO DE LA VENTANA QUE LA CUENTA PUEDE OPERAR?

Contexto: los activos '-op' son el forex REAL (verificado: EURUSD-op cotiza
1.1441 y coincide con nuestro historico de EURUSD con 0.05 pips de diferencia
mediana sobre 299 velas). Pagan 87-90%, no 84%. Pero su sesion es
01:00-18:30 UTC de lunes a viernes.

El problema: el edge que habiamos encontrado vivia en 20-23 UTC, que queda
FUERA de esa ventana. Asi que la pregunta cambia — ya no es "?hay edge?" sino
"?hay edge en las horas en que la cuenta puede operar, con un breakeven de
52.63% en vez de 54.35%?".

Baja el listón dos puntos largos. Eso puede convertir en rentable algo que
antes no lo era.

Uso:
    py escanear_op.py                      # 30m y 15m, payout 90%
    py escanear_op.py --tf 15m --payout 87
"""
import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import entrenar_modelo as E
import ml_walkforward as ML
import simulador as S

# Los 10 pares reales que tenemos descargados. Sus equivalentes operables son
# los mismos nombres con sufijo '-op'.
REALES = ["AUDUSD", "EURGBP", "EURJPY", "EURUSD", "GBPJPY",
          "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]


def walk_forward(X, y, horas, dts, activos, bloques=8, pcts=(2, 5, 10, 20, 50, 100)):
    """Entrena con el pasado, opera el futuro. El umbral sale SOLO del train."""
    cortes = np.linspace(0, len(X), bloques + 1).astype(int)
    P, Y, H, D, A = [], [], [], [], []
    for i in range(1, bloques):
        trf, ti, tf_ = cortes[i], cortes[i], cortes[i + 1]
        if trf < 5000 or tf_ - ti < 500:
            continue
        m = ML.crear_modelo("gb")
        m.fit(X[:trf], y[:trf])
        P.append(m.predict_proba(X[ti:tf_])[:, 1])
        Y.append(y[ti:tf_])
        H.append(horas[ti:tf_])
        D.append(dts[ti:tf_])
        A.append(activos[ti:tf_])
    if not P:
        return None
    return (np.concatenate(P), np.concatenate(Y), np.concatenate(H),
            np.concatenate(D), np.concatenate(A))


def informe(p, y, h, a, be, etiqueta):
    conf = np.abs(p - 0.5)
    acierto = np.where(p > 0.5, y, 1 - y)
    print(f"\n{'=' * 78}\n{etiqueta}\n{'=' * 78}")
    n, k = len(acierto), int(acierto.sum())
    lo, hi = S.wilson(k, n)
    print(f"  sin filtrar : n={n:,}  WR={100 * k / n:.2f}%  IC95=[{lo:.2f},{hi:.2f}]")
    print(f"  breakeven   : {be:.2f}%")

    print(f"\n  SELECTIVIDAD (opera solo las mas confiables)")
    print(f"  {'opera el':>9}{'n':>9}{'WR':>8}{'IC95':>16}   EV/op    veredicto")
    print("  " + "-" * 64)
    mejor = None
    for pct in (2, 5, 10, 20, 50, 100):
        u = np.quantile(conf, 1 - pct / 100)
        sel = conf >= u
        if sel.sum() < 200:
            continue
        ac = acierto[sel]
        nn, kk = len(ac), int(ac.sum())
        wr = 100 * kk / nn
        l, hh = S.wilson(kk, nn)
        payout = be_a_payout(be)
        ev = (kk / nn) * payout - (1 - kk / nn)
        v = "RENTABLE" if l > be else ("sobre BE" if wr > be else "no")
        print(f"  {pct:>8}%{nn:>9,}{wr:>7.2f}%  [{l:5.2f},{hh:5.2f}]  ${ev:+7.4f}   {v}")
        if l > be and (mejor is None or wr > mejor[1]):
            mejor = (pct, wr, l, nn)

    # por hora, con el 10% mas confiable
    u10 = np.quantile(conf, 0.90)
    sel = conf >= u10
    print(f"\n  POR HORA UTC (operando el 10% mas confiable)")
    print(f"  {'hora':>5}{'n':>8}{'WR':>8}{'IC95':>16}")
    print("  " + "-" * 40)
    for hora in sorted(set(h[sel])):
        m = sel & (h == hora)
        if m.sum() < 100:
            continue
        ac = acierto[m]
        nn, kk = len(ac), int(ac.sum())
        l, hh = S.wilson(kk, nn)
        marca = "  <-- supera BE" if l > be else ""
        print(f"  {int(hora):>4}h{nn:>8,}{100 * kk / nn:>7.2f}%  [{l:5.2f},{hh:5.2f}]{marca}")

    print(f"\n  POR ACTIVO (operando el 10% mas confiable)")
    print(f"  {'activo':>9}{'n':>8}{'WR':>8}{'IC95':>16}")
    print("  " + "-" * 44)
    for act in sorted(set(a[sel])):
        m = sel & (a == act)
        if m.sum() < 100:
            continue
        ac = acierto[m]
        nn, kk = len(ac), int(ac.sum())
        l, hh = S.wilson(kk, nn)
        marca = "  <-- supera BE" if l > be else ""
        print(f"  {act:>9}{nn:>8,}{100 * kk / nn:>7.2f}%  [{l:5.2f},{hh:5.2f}]{marca}")
    return mejor


def be_a_payout(be):
    return 100 / be - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="velas.db")
    ap.add_argument("--tf", default="30m,15m")
    ap.add_argument("--payout", type=float, default=90.0,
                    help="payout del activo operable (EURUSD-op paga 90%%)")
    ap.add_argument("--horas", default="1-17",
                    help="franja UTC operable; la sesion -op es 01:00-18:30")
    ap.add_argument("--bloques", type=int, default=8)
    ap.add_argument("--guardar", action="store_true",
                    help="guarda las predicciones out-of-sample para reanalisis")
    args = ap.parse_args()

    be = 100 / (1 + args.payout / 100)
    horas = tuple(int(x) for x in args.horas.split("-"))
    print(f"Payout {args.payout}%  ->  breakeven {be:.2f}%")
    print(f"Ventana operable: {horas[0]:02d}:00-{horas[1]:02d}:59 UTC, lunes a viernes")
    print(f"(la sesion real de los -op es 01:00-18:30 UTC)")

    for nombre in [t.strip() for t in args.tf.split(",")]:
        seg = S.TFS[nombre]
        print(f"\n\nPreparando {nombre}...")
        M = E.preparar(args.db, seg, horas, REALES)
        if M is None or len(M) < 10000:
            print(f"  datos insuficientes para {nombre}")
            continue
        feats = [f for f in ML.FEATURES if f in M.columns]
        X = M[feats].replace([np.inf, -np.inf], np.nan)
        X = X.fillna(X.median()).values
        y = M.sube.values
        print(f"  {len(M):,} muestras | {M.activo.nunique()} activos | "
              f"{M.dt.min():%Y-%m-%d} a {M.dt.max():%Y-%m-%d}")
        print(f"  tasa base (sube): {100 * y.mean():.2f}%")

        r = walk_forward(X, y, M.hora.values, M.dt.values, M.activo.values, args.bloques)
        if r is None:
            print("  muestra insuficiente para walk-forward")
            continue
        p, yy, hh, dd, aa = r
        informe(p, yy, hh, aa, be, f"{nombre} — REAL, {horas[0]}-{horas[1]}h UTC, "
                                   f"payout {args.payout}%")
        if args.guardar:
            f = f"oos_{nombre}.npz"
            np.savez_compressed(f, p=p, y=yy, hora=hh, dt=dd.astype("datetime64[s]").astype("int64"),
                                activo=aa)
            print()
            print("  predicciones out-of-sample guardadas en " + f)
            print("  (para comprobar si los hallazgos aguantan en el tiempo)")


if __name__ == "__main__":
    main()
