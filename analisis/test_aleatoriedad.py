import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
?Son los precios distinguibles de un paseo aleatorio?

Si una serie pasa estos tests, NINGUN metodo la puede predecir: no hay
estructura que encontrar, por sofisticado que sea el modelo. Esta pregunta
va ANTES que "probemos otro indicador" o "probemos una red neuronal".

  * Autocorrelacion de retornos + Ljung-Box: ?los retornos recuerdan al pasado?
  * Test de rachas: ?hay mas o menos alternancias de las que da el azar?
  * Variance Ratio (Lo-MacKinlay): 1 = paseo aleatorio, <1 reversion, >1 tendencia
"""
import numpy as np
import warnings
from scipy import stats
warnings.filterwarnings("ignore")
import simulador as S


def runs_test(x):
    s = np.sign(x); s = s[s != 0]
    n1, n2 = int((s > 0).sum()), int((s < 0).sum()); n = n1 + n2
    if n < 20: return np.nan, np.nan
    runs = 1 + int(np.sum(s[1:] != s[:-1]))
    esp = 2 * n1 * n2 / n + 1
    var = (2 * n1 * n2 * (2 * n1 * n2 - n)) / (n * n * (n - 1))
    if var <= 0: return np.nan, np.nan
    z = (runs - esp) / np.sqrt(var)
    return z, 2 * (1 - stats.norm.cdf(abs(z)))


def variance_ratio(p, q=2):
    r = np.diff(np.log(p)); n = len(r)
    if n < 100: return np.nan, np.nan
    m = n // q * q; r = r[:m]
    v1 = np.var(r, ddof=1)
    vq = np.var(r.reshape(-1, q).sum(axis=1), ddof=1) / q
    if v1 <= 0: return np.nan, np.nan
    vr = vq / v1
    z = (vr - 1) * np.sqrt(m / (2 * (q - 1)))
    return vr, 2 * (1 - stats.norm.cdf(abs(z)))


def ljung_box(x, lags=10):
    n = len(x)
    ac = [np.corrcoef(x[:-k], x[k:])[0, 1] for k in range(1, lags + 1)]
    q = n * (n + 2) * sum((a * a) / (n - k) for k, a in enumerate(ac, 1))
    return q, 1 - stats.chi2.cdf(q, lags), ac[0]


def main(tf=1800, nombre="30m"):
    todos = S.activos_disponibles("velas.db", tf)
    otc = [a for a in todos if a.endswith("-OTC")]
    real = [a for a in todos if not a.endswith("-OTC")]
    print(f"TESTS DE ALEATORIEDAD — {nombre}")
    print("p < 0.01 en cualquier test = hay estructura explotable\n")
    print(f"{'activo':<14}{'autocorr1':>11}{'LjungBox p':>12}{'runs p':>9}"
          f"{'VR(2)':>8}{'VR p':>8}  veredicto")
    print("-" * 82)
    resumen = {}
    for grupo, lst in (("OTC (operable)", otc), ("REAL (no operable)", real[:5])):
        print(f"  --- {grupo} ---")
        con_est = 0
        for a in lst:
            df = S.cargar_tf("velas.db", a, tf)
            if len(df) < 5000: continue
            p = df.close.values
            r = np.diff(np.log(p)); r = r[np.isfinite(r)]
            if len(r) < 1000: continue
            _, pq, ac1 = ljung_box(r)
            _, pr = runs_test(r)
            vr, pv = variance_ratio(p)
            est = (pq < 0.01) or (pr < 0.01) or (pv < 0.01)
            con_est += est
            print(f"{a:<14}{ac1:>11.4f}{pq:>12.2e}{pr:>9.3f}{vr:>8.3f}{pv:>8.3f}  "
                  f"{'ESTRUCTURA' if est else 'azar puro'}")
        resumen[grupo] = (con_est, len([a for a in lst]))
        print()
    print("RESUMEN")
    for g, (c, t) in resumen.items():
        print(f"  {g:<22} {c} de {t} muestran estructura")


if __name__ == "__main__":
    import sys
    tf = int(sys.argv[1]) if len(sys.argv) > 1 else 1800
    main(tf, {60: "1m", 900: "15m", 1800: "30m"}.get(tf, str(tf)))
