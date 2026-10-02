import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
¿Por qué el modelo cripto (BTCUSD-op/ETHUSD-op 15m, hash bc23a4be4266) va
37.1% en sus primeras 35 operaciones en vivo cuando el walk-forward esperaba
56-58%? Y sobre todo: ¿hay algún FILTRO que separe, de forma robusta y fuera
de muestra, las señales que revierten de verdad de las que son solo "comprar
en medio de una tendencia"?

Se reconstruye el mismo walk-forward de validar_cripto.py (8 bloques, GB,
20% más confiable — igual que el modelo en producción) sobre TODO el
histórico de velas.db (2024-07-21 a 2026-09-30, BTCUSD+ETHUSD 15m), lo que da
miles de señales fuera de muestra (no decenas como en vivo). Sobre ese
conjunto se prueban tres cosas, con la misma disciplina que ya usa este
proyecto (minar en la 1a mitad, validar en la 2a; Benjamini-Hochberg q=0.10
sobre TODAS las pruebas hechas; intervalos de Wilson; control aleatorio):

  1. Filtro de tendencia larga: ¿la apuesta en la MISMA dirección que una
     tendencia larga (ret20, distancia a SMA50, pendiente de EMA50, o
     ADX14 alto con +DI/-DI) rinde peor que la apuesta en contra?
  2. Racha + extremo de oscilador como confirmación de agotamiento real.
  3. Barrido ciego de los 34 indicadores (uno a uno y en pares), minado en
     la 1a mitad y validado en la 2a.

    py backtest_filtro_tendencia_cripto.py
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
PCT_SENAL = 0.20          # igual que modelo_cripto15.joblib (pct_operado)
BLOQUES = 8
CACHE = "oos_cripto_filtros.pkl"
Q_BH = 0.10
rng = np.random.default_rng(13)


# ───────────────────────── utilidades compartidas ─────────────────────────

def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def bh_q(pvals):
    """Benjamini-Hochberg: q-valor por prueba (no solo el umbral)."""
    p = np.asarray(pvals, dtype=float)
    orden = np.argsort(p)
    m = len(p)
    q = np.empty(m)
    prev = 1.0
    for rank in range(m, 0, -1):
        i = orden[rank - 1]
        prev = min(prev, p[i] * m / rank)
        q[i] = prev
    return q


def metr(k, n):
    lo, hi = wilson(k, n)
    return dict(n=n, k=k, wr=100 * k / n if n else float("nan"), lo=lo, hi=hi)


def fisher_p(k1, n1, k2, n2):
    """p-valor de 'el grupo 1 acierta distinto que el grupo 2' (dos colas)."""
    tabla = [[k1, n1 - k1], [k2, n2 - k2]]
    try:
        _, p = stats.fisher_exact(tabla)
    except Exception:
        p = 1.0
    return p


# ───────────────────────── datos + walk-forward OOS ─────────────────────────

def preparar():
    """Igual que validar_cripto.preparar, más un par de variables de
    tendencia (EMA50 y su pendiente) que NO usa el modelo pero sirven para
    probar el filtro de tendencia. No se reentrena nada: el modelo real ya
    está fijo (bc23a4be4266); aquí se replica su walk-forward de validación
    con las MISMAS features y el MISMO código de entrenamiento (ML.crear_modelo)."""
    filas = []
    for a in CRIPTO:
        df = S.cargar_tf("velas.db", a, 900)
        df = S.indicadores(df)
        c = df.close
        for k in (1, 2, 3, 5, 10, 20):
            df[f"ret{k}"] = c.pct_change(k) * 100
        df["vol20"] = c.pct_change().rolling(20).std() * 100
        df["vol_rel"] = df.vol20 / df.vol20.rolling(100).mean()
        df["rango_rel"] = (df.high - df.low) / c * 100
        df["cierre_en_rango"] = (c - df.low) / (df.high - df.low).replace(0, np.nan)

        # --- solo para analisis de filtros, el modelo no las usa ---
        ema50 = c.ewm(span=50, adjust=False).mean()
        df["ema50_slope_pct"] = (ema50 - ema50.shift(5)) / c * 100

        nxt = c.shift(-1)
        ok = df.contigua.shift(-1).fillna(False) & nxt.notna() & (nxt != c)
        sub = df[ok].copy()
        sub["sube"] = (nxt[ok] > sub.close).astype(int)
        sub["activo"] = a
        filas.append(sub)
    M = pd.concat(filas).sort_values("ts").reset_index(drop=True)
    feats = [f for f in ML.FEATURES if f in M.columns]
    extra = ["ema50_slope_pct"]
    M = M.dropna(subset=feats + extra).reset_index(drop=True)
    return M, feats


def walk_forward_oos(M, feats, pct=PCT_SENAL, bloques=BLOQUES):
    """Mismo walk-forward que validar_cripto.py / entrenar_modelo.validar:
    entrena solo con el pasado de cada bloque, selecciona el pct% mas
    confiable con el umbral calculado SOLO en ese train. Nunca ve el futuro."""
    X = M[feats].replace([np.inf, -np.inf], np.nan)
    X = X.fillna(X.median()).values
    y = M.sube.values
    cortes = np.linspace(0, len(M), bloques + 1).astype(int)
    partes = []
    for i in range(1, bloques):
        trf, ti, tf_ = cortes[i], cortes[i], cortes[i + 1]
        if trf < 3000 or tf_ - ti < 100:
            continue
        m = ML.crear_modelo("gb")
        m.fit(X[:trf], y[:trf])
        conf_tr = np.abs(m.predict_proba(X[:trf])[:, 1] - 0.5)
        umbral = np.quantile(conf_tr, 1 - pct)
        p = m.predict_proba(X[ti:tf_])[:, 1]
        sub = M.iloc[ti:tf_].copy()
        sub["p"] = p
        sub["umbral_bloque"] = umbral
        sub["senal"] = np.abs(p - 0.5) >= umbral
        partes.append(sub)
        print(f"  bloque {i}/{bloques - 1}  train={trf:,}  umbral={umbral:.4f}  "
              f"señales={int(sub.senal.sum()):,}/{len(sub):,}")
    D = pd.concat(partes).reset_index(drop=True)
    D["call"] = D.p > 0.5
    D["gano"] = np.where(D.call, D.sube, 1 - D.sube)
    return D


def cargar_oos():
    import os
    if os.path.exists(CACHE):
        print(f"(usando cache {CACHE}; borralo para recalcular)")
        return pd.read_pickle(CACHE)
    print("Preparando dataset cripto 15m (BTCUSD+ETHUSD, histórico completo)...")
    M, feats = preparar()
    print(f"  {len(M):,} velas utilizables · {M.dt.min():%Y-%m-%d} a {M.dt.max():%Y-%m-%d}\n")
    print(f"Walk-forward OOS ({BLOQUES} bloques, {100 * PCT_SENAL:.0f}% más confiable, "
          f"igual que el modelo en vivo)...")
    D = walk_forward_oos(M, feats)
    D.to_pickle(CACHE)
    return D


# ───────────────────────── informe de una prueba ─────────────────────────

PRUEBAS = []   # acumula (nombre, p_valor) de TODAS las pruebas de hipótesis del estudio


def registrar(nombre, p):
    PRUEBAS.append((nombre, p))


def imprimir_grupo(nombre, g, be=BE):
    n, k = len(g), int(g.gano.sum())
    if n == 0:
        print(f"    {nombre:<28} sin señales")
        return None
    lo, hi = wilson(k, n)
    print(f"    {nombre:<28} n={n:>6,}  WR={100 * k / n:6.2f}%  IC95=[{lo:5.2f},{hi:5.2f}]"
          f"  {'sobre BE' if lo > be else ('wr>BE' if 100*k/n > be else 'no')}")
    return n, k


# ───────────────────────── H1: filtro de tendencia larga ─────────────────────────

def hipotesis_1(S_sig):
    print("\n" + "=" * 100)
    print("HIPÓTESIS 1 — ¿la apuesta A FAVOR de la tendencia larga rinde distinto que EN CONTRA?")
    print("=" * 100)
    direccion = np.where(S_sig.call, 1, -1)
    mitad = S_sig.dt.quantile(0.5)
    primera, segunda = S_sig.dt <= mitad, S_sig.dt > mitad

    defs = {
        "ret20 (retorno 20 velas)": np.sign(S_sig.ret20.values),
        "dist_sma50 (precio vs SMA50)": np.sign(S_sig.dist_sma50.values),
        "pendiente EMA50 (5 velas)": np.sign(S_sig.ema50_slope_pct.values),
    }
    resumen = []
    for nombre, trend in defs.items():
        definida = trend != 0
        coincide = definida & (direccion == trend)
        contraria = definida & (direccion == -trend)
        nc, kc = int(coincide.sum()), int(S_sig.gano.values[coincide].sum())
        nx, kx = int(contraria.sum()), int(S_sig.gano.values[contraria].sum())
        if nc < 50 or nx < 50:
            continue
        p = fisher_p(kc, nc, kx, nx)
        registrar(f"H1 {nombre}: coincide vs contraria", p)
        # estabilidad en 2 mitades (solo descriptivo)
        w1c = 100 * S_sig.gano.values[coincide & primera.values].mean() if (coincide & primera.values).sum() >= 20 else np.nan
        w2c = 100 * S_sig.gano.values[coincide & segunda.values].mean() if (coincide & segunda.values).sum() >= 20 else np.nan
        w1x = 100 * S_sig.gano.values[contraria & primera.values].mean() if (contraria & primera.values).sum() >= 20 else np.nan
        w2x = 100 * S_sig.gano.values[contraria & segunda.values].mean() if (contraria & segunda.values).sum() >= 20 else np.nan
        print(f"\n  --- {nombre} ---")
        imprimir_grupo("coincide con tendencia", pd.DataFrame({"gano": S_sig.gano.values[coincide]}))
        imprimir_grupo("en contra de tendencia", pd.DataFrame({"gano": S_sig.gano.values[contraria]}))
        print(f"    p (fisher, coincide != contraria) = {p:.4g}")
        print(f"    estabilidad coincide:  1a mitad {w1c:5.2f}%  2a mitad {w2c:5.2f}%")
        print(f"    estabilidad contraria: 1a mitad {w1x:5.2f}%  2a mitad {w2x:5.2f}%")
        resumen.append(dict(nombre=nombre, nc=nc, wc=100 * kc / nc, nx=nx, wx=100 * kx / nx,
                            p=p, w1c=w1c, w2c=w2c, w1x=w1x, w2x=w2x))

    # ADX14 alto + DI direccional
    print(f"\n  --- ADX14 (tendencia fuerte definida por +DI/-DI) ---")
    for umbral_adx in (20, 25, 30):
        fuerte = S_sig.adx14.values >= umbral_adx
        trend_di = np.sign(S_sig.plus_di.values - S_sig.minus_di.values)
        coincide = fuerte & (direccion == trend_di)
        contraria = fuerte & (direccion == -trend_di)
        nc, kc = int(coincide.sum()), int(S_sig.gano.values[coincide].sum())
        nx, kx = int(contraria.sum()), int(S_sig.gano.values[contraria].sum())
        if nc < 50 or nx < 50:
            print(f"    ADX14>={umbral_adx}: muestra insuficiente (nc={nc}, nx={nx})")
            continue
        p = fisher_p(kc, nc, kx, nx)
        registrar(f"H1 ADX14>={umbral_adx} coincide vs contraria", p)
        lo_c, hi_c = wilson(kc, nc)
        lo_x, hi_x = wilson(kx, nx)
        print(f"    ADX14>={umbral_adx}:  coincide n={nc:>5,} WR={100*kc/nc:6.2f}% [{lo_c:5.2f},{hi_c:5.2f}]"
              f"   |   contraria n={nx:>5,} WR={100*kx/nx:6.2f}% [{lo_x:5.2f},{hi_x:5.2f}]   p={p:.4g}")
        resumen.append(dict(nombre=f"ADX14>={umbral_adx}", nc=nc, wc=100*kc/nc, nx=nx, wx=100*kx/nx, p=p,
                            w1c=np.nan, w2c=np.nan, w1x=np.nan, w2x=np.nan))
    return pd.DataFrame(resumen)


# ───────────────────────── H2: racha + extremo de oscilador ─────────────────────────

def hipotesis_2(S_sig):
    print("\n" + "=" * 100)
    print("HIPÓTESIS 2 — racha larga + extremo del oscilador, ¿señal de agotamiento real?")
    print("(umbral elegido en la 1a mitad, validado en la 2a — nunca al revés)")
    print("=" * 100)
    g = S_sig.copy()
    g["racha_abs"] = g.racha.abs()
    # score de "extremo a favor de la apuesta": positivo y grande cuando el
    # oscilador está estirado del lado que justifica la reversión apostada.
    g["score_cci"] = np.where(g.call, -g.cci20, g.cci20)
    g["score_stoch"] = np.where(g.call, 50 - g.stoch_k, g.stoch_k - 50)

    mitad = g.dt.quantile(0.5)
    A, B = g[g.dt <= mitad], g[g.dt > mitad]

    mejor = None
    candidatos = []
    for K in (2, 3, 4, 5, 6):
        for q in (0.5, 0.7, 0.8, 0.9):
            u_cci = A.score_cci.quantile(q)
            u_stoch = A.score_stoch.quantile(q)
            m_tr = (A.racha_abs >= K) & (A.score_cci >= u_cci) & (A.score_stoch >= u_stoch)
            n_tr = int(m_tr.sum())
            if n_tr < 80:
                continue
            wr_tr = 100 * A.gano[m_tr].mean()
            candidatos.append((K, u_cci, u_stoch, n_tr, wr_tr, q))
    candidatos.sort(key=lambda x: -x[4])
    print(f"\n  mejores combinaciones en la 1a mitad (minado, top 8 de {len(candidatos)}):")
    print(f"  {'racha>=':>8}{'cuantil':>9}{'u_cci':>10}{'u_stoch':>10}{'n_train':>9}{'WR_train':>10}")
    for K, u_cci, u_stoch, n_tr, wr_tr, q in candidatos[:8]:
        print(f"  {K:>8}{q:>9.2f}{u_cci:>10.2f}{u_stoch:>10.2f}{n_tr:>9,}{wr_tr:>9.2f}%")

    if not candidatos:
        print("  ninguna combinación con n_train>=80.")
        return None

    K, u_cci, u_stoch, n_tr, wr_tr, q = candidatos[0]
    m_te = (B.racha_abs >= K) & (B.score_cci >= u_cci) & (B.score_stoch >= u_stoch)
    n_te, k_te = int(m_te.sum()), int(B.gano[m_te].sum())
    base_te_n, base_te_k = len(B), int(B.gano.sum())
    print(f"\n  CANDIDATO ELEGIDO (mejor en train): racha_abs>={K} AND score_cci>={u_cci:.2f} "
          f"AND score_stoch>={u_stoch:.2f}")
    if n_te < 30:
        print(f"  validación en 2a mitad: n={n_te} -- muestra insuficiente, no se puede concluir.")
        p = 1.0
    else:
        lo, hi = wilson(k_te, n_te)
        p_vs_be = stats.binomtest(k_te, n_te, BE / 100, alternative="greater").pvalue
        p_vs_base = fisher_p(k_te, n_te, base_te_k - k_te, base_te_n - n_te)
        print(f"  validación en 2a mitad: n={n_te:,}  WR={100*k_te/n_te:.2f}%  IC95=[{lo:.2f},{hi:.2f}]")
        print(f"  base (todas las señales, 2a mitad): WR={100*base_te_k/base_te_n:.2f}%  n={base_te_n:,}")
        print(f"  p (vs breakeven {BE:.2f}%) = {p_vs_be:.4g}   p (vs resto de señales) = {p_vs_base:.4g}")
        registrar("H2 racha+extremo (umbral de train, validado en test)", p_vs_base)
        p = p_vs_base

    # control aleatorio del mismo tamaño que el candidato elegido, para comparar
    idx_rand = rng.choice(len(B), size=min(n_te, len(B)), replace=False) if n_te else np.array([], int)
    if len(idx_rand) >= 30:
        k_r = int(B.gano.values[idx_rand].sum())
        lo_r, hi_r = wilson(k_r, len(idx_rand))
        print(f"  control ALEATORIO (mismo tamaño n={len(idx_rand):,}): WR={100*k_r/len(idx_rand):.2f}%  "
              f"IC95=[{lo_r:.2f},{hi_r:.2f}]")

    return dict(K=K, u_cci=u_cci, u_stoch=u_stoch, n_train=n_tr, wr_train=wr_tr,
               n_test=n_te, wr_test=100 * k_te / n_te if n_te else np.nan, p=p)


# ───────────────────────── H3: barrido ciego de los 34 indicadores ─────────────────────────

def hipotesis_3(S_sig, feats):
    print("\n" + "=" * 100)
    print("HIPÓTESIS 3 — barrido ciego: ¿algún otro indicador (solo o en pares) separa")
    print("ganadas de perdidas DENTRO de las señales ya seleccionadas por el modelo?")
    print("(se mina en la 1a mitad, se valida SOLO lo prometedor en la 2a — igual que")
    print(" simulador.buscar() y busqueda_exhaustiva.py)")
    print("=" * 100)
    g = S_sig.copy()
    mitad = g.dt.quantile(0.5)
    A, B = g[g.dt <= mitad].copy(), g[g.dt > mitad].copy()

    usables = [f for f in feats if g[f].nunique() > 5]
    cand = []
    for f in usables:
        x = A[f].values
        for q in (0.1, 0.2, 0.3, 0.7, 0.8, 0.9):
            t = np.quantile(x, q)
            for op in (">", "<"):
                m = (x > t) if op == ">" else (x < t)
                if m.sum() < 150:
                    continue
                wr_tr = 100 * A.gano.values[m].mean()
                cand.append({"f": f, "op": op, "t": t, "n_tr": int(m.sum()), "wr_tr": wr_tr})
    C = pd.DataFrame(cand).drop_duplicates(subset=["f", "op", "t"])
    C = C.sort_values("wr_tr", ascending=False)
    print(f"\n  {len(C):,} combinaciones minadas en la 1a mitad. Top 15 por WR de train:")
    print(f"  {'regla':<34}{'n_train':>9}{'WR_train':>10}")
    for _, r in C.head(15).iterrows():
        print(f"  {f'{r.f} {r.op} {r.t:.4g}':<34}{r.n_tr:>9,}{r.wr_tr:>9.2f}%")

    # control aleatorio: una condicion sin sentido, minada igual que las demas
    A["azar"] = rng.random(len(A))
    for q in (0.2, 0.5, 0.8):
        t = A.azar.quantile(q)
        m = A.azar.values > t
        C_az = {"f": "ALEATORIO", "op": ">", "t": t, "n_tr": int(m.sum()),
                "wr_tr": 100 * A.gano.values[m].mean()}
        C = pd.concat([C, pd.DataFrame([C_az])], ignore_index=True)

    def validar_test(f, op, t, Bdf):
        if f == "ALEATORIO":
            Bdf = Bdf.assign(azar=rng.random(len(Bdf)))
            f = "azar"
        x = Bdf[f].values
        m = (x > t) if op == ">" else (x < t)
        n, k = int(m.sum()), int(Bdf.gano.values[m].sum())
        return n, k, m

    print(f"\n  validación en la 2a mitad de los 12 mejores candidatos + control aleatorio:")
    print(f"  {'regla':<34}{'n_test':>8}{'WR_test':>9}{'IC95':>16}{'p(vs resto)':>12}")
    resultados = []
    top = pd.concat([C.head(12), C[C.f == "ALEATORIO"]])
    for _, r in top.iterrows():
        n, k, m = validar_test(r.f, r.op, r.t, B)
        if n < 100:
            continue
        lo, hi = wilson(k, n)
        resto_n, resto_k = len(B) - n, int(B.gano.sum()) - k
        p = fisher_p(k, n, resto_k, resto_n)
        nombre = f"{r.f} {r.op} {r.t:.4g}"
        registrar(f"H3 {nombre}", p)
        print(f"  {nombre:<34}{n:>8,}{100*k/n:>8.2f}%  [{lo:5.2f},{hi:5.2f}]{p:>12.4g}")
        resultados.append(dict(regla=nombre, n=n, wr=100 * k / n, lo=lo, hi=hi, p=p))

    # pares: combinar los 5 mejores (no aleatorios) de a dos
    print(f"\n  --- pares de los 5 mejores candidatos (no aleatorios) ---")
    mejores5 = C[C.f != "ALEATORIO"].head(5).to_dict("records")
    vistos = set()
    for i in range(len(mejores5)):
        for j in range(i + 1, len(mejores5)):
            a, b = mejores5[i], mejores5[j]
            if a["f"] == b["f"]:
                continue
            clave = tuple(sorted([f"{a['f']}{a['op']}", f"{b['f']}{b['op']}"]))
            if clave in vistos:
                continue
            vistos.add(clave)
            xa, xb = B[a["f"]].values, B[b["f"]].values
            ma = (xa > a["t"]) if a["op"] == ">" else (xa < a["t"])
            mb = (xb > b["t"]) if b["op"] == ">" else (xb < b["t"])
            m = ma & mb
            n, k = int(m.sum()), int(B.gano.values[m].sum())
            if n < 100:
                continue
            lo, hi = wilson(k, n)
            resto_n, resto_k = len(B) - n, int(B.gano.sum()) - k
            p = fisher_p(k, n, resto_k, resto_n)
            nombre = f"{a['f']}{a['op']}{a['t']:.4g} AND {b['f']}{b['op']}{b['t']:.4g}"
            registrar(f"H3 par {nombre}", p)
            print(f"  {nombre[:48]:<50}{n:>8,}{100*k/n:>8.2f}%  [{lo:5.2f},{hi:5.2f}]{p:>10.4g}")
            resultados.append(dict(regla=nombre, n=n, wr=100 * k / n, lo=lo, hi=hi, p=p))

    return pd.DataFrame(resultados)


# ───────────────────────── main ─────────────────────────

def main():
    D = cargar_oos()
    S_sig = D[D.senal].reset_index(drop=True)
    dias = D.dt.dt.normalize().nunique()

    print("\n" + "=" * 100)
    print("DATASET Y REPLICACIÓN DE BASE (confirma que coincide con lo ya validado)")
    print("=" * 100)
    print(f"  velas OOS evaluadas (todos los bloques, antes de filtrar confianza): {len(D):,}")
    print(f"  señales (top {100*PCT_SENAL:.0f}% más confiable, igual que bc23a4be4266): {len(S_sig):,}")
    print(f"  periodo: {D.dt.min():%Y-%m-%d} a {D.dt.max():%Y-%m-%d}  ({dias} días, 24x7)")
    n, k = len(S_sig), int(S_sig.gano.sum())
    lo, hi = wilson(k, n)
    print(f"  WR GLOBAL de las señales (sin filtro): {100*k/n:.2f}%  IC95=[{lo:.2f},{hi:.2f}]  "
          f"breakeven={BE:.2f}%")
    mitad = S_sig.dt.quantile(0.5)
    w1 = 100 * S_sig[S_sig.dt <= mitad].gano.mean()
    w2 = 100 * S_sig[S_sig.dt > mitad].gano.mean()
    print(f"  1a mitad: {w1:.2f}%   2a mitad: {w2:.2f}%")
    for a, s in S_sig.groupby("activo"):
        nn, kk = len(s), int(s.gano.sum())
        ll, hh = wilson(kk, nn)
        print(f"    {a:8s} n={nn:>6,}  WR={100*kk/nn:6.2f}%  IC95=[{ll:5.2f},{hh:5.2f}]")

    feats = [f for f in ML.FEATURES if f in S_sig.columns]
    r1 = hipotesis_1(S_sig)
    r2 = hipotesis_2(S_sig)
    r3 = hipotesis_3(S_sig, feats)

    print("\n" + "=" * 100)
    print("CORRECCIÓN POR COMPARACIONES MÚLTIPLES — TODAS las pruebas de hipótesis del estudio")
    print("=" * 100)
    nombres = [n for n, p in PRUEBAS]
    pvals = [p for n, p in PRUEBAS]
    q = bh_q(pvals)
    tabla = pd.DataFrame({"prueba": nombres, "p": pvals, "q": q}).sort_values("p")
    print(f"  total de pruebas: {len(tabla)}   (q=BH, umbral {Q_BH})\n")
    pd.set_option("display.max_colwidth", 60)
    print(tabla.to_string(index=False, float_format=lambda x: f"{x:.4g}"))
    sobreviven = tabla[tabla.q < Q_BH]
    print(f"\n  SOBREVIVEN la corrección BH (q<{Q_BH}): {len(sobreviven)} de {len(tabla)}")
    if len(sobreviven):
        print(sobreviven.to_string(index=False, float_format=lambda x: f"{x:.4g}"))
    else:
        print("  NINGUNA prueba sobrevive la corrección por comparaciones múltiples.")

    print("\n" + "=" * 100)
    print("VEREDICTO FINAL")
    print("=" * 100)
    if len(sobreviven):
        print("  Hay al menos un patrón que sobrevive la corrección múltiple. Revisar si")
        print("  además es estable en las dos mitades temporales antes de considerarlo real")
        print("  (ver el detalle impreso arriba para cada hipótesis).")
    else:
        print("  Ningún filtro probado (tendencia larga, racha+extremo, ni el barrido ciego")
        print("  de los 34 indicadores) sobrevive la corrección por comparaciones múltiples.")
        print("  La caída a 37.1% en las 35 operaciones en vivo es compatible con ruido de")
        print("  muestra chica sobre una estrategia cuyo WR esperado real (OOS, miles de")
        print(f"  señales) es {100*k/n:.2f}% [{lo:.2f},{hi:.2f}] -- no con un régimen nuevo")
        print("  identificable con los datos disponibles.")


if __name__ == "__main__":
    main()
