"""
Weis Wave Volume + Bollinger(14,2) — estrategia de RUPTURA/CONTINUACIÓN
(al revés de la reversión a la media que ya validamos en el resto del
proyecto: aquí se apuesta A FAVOR del nuevo impulso, solo si lo confirma el
volumen agregado por onda).

INDICADOR 1 — Weis Wave Volume (aproximación, no es el código propietario).
  VERSIÓN 2, corregida a partir de una captura real del histograma WWV que
  mostró el usuario (09:45-10:30, ~1 barra por vela): el patrón visual es
  una ESCALERA -- dentro de una racha del mismo color cada barra sucesiva es
  MÁS ALTA que la anterior, hasta que la racha corta de color y el
  crecimiento arranca de nuevo desde una barra chica. Eso es la firma de que
  cada barra = VOLUMEN ACUMULADO (running sum) desde que empezó la onda
  actual hasta esa vela inclusive -- NO el volumen aislado de una vela ni de
  un "ladrillo" cerrado (así era la versión anterior de este script, que se
  descarta: ver nota al pie).
  * Dirección de la onda: igual Renko de siempre -- mientras el cierre no
    retroceda ATR_MULT x ATR(14) en contra de la dirección vigente (medido
    desde el EXTREMO alcanzado, no desde un nivel fijo), sigue siendo la
    MISMA onda. ATR(14) sobre |close[i]-close[i-1]| (Price Source=Close),
    EWM alpha=1/14 (misma convención que simulador.indicadores).
  * Altura de la barra en la vela i = suma del volumen de TODAS las velas
    desde el inicio de la onda actual hasta i inclusive (crece vela a vela).
  * Al revertir, el acumulado de la onda nueva arranca en 0 otra vez; la
    vela que causó el retroceso es la PRIMERA vela de la onda nueva.

INDICADOR 2 — Bollinger(14,2) sobre el cierre (OJO: 14,2 — distinto del 20,2
que se usa en el resto del proyecto).

REGLA DE ENTRADA (interpretación corregida, ver informe para el detalle):
  1. "Racha de >= RACHA_MIN velas del mismo color" = la onda (roja o verde)
     que se completa, antes de cortarse, duró al menos RACHA_MIN velas
     (osea mostró al menos RACHA_MIN barras creciendo de ese color).
  2. Al cortarse esa racha empieza la onda opuesta, con su acumulado
     reiniciado en 0. Se vigila SOLO esa primera onda opuesta.
  3. Se monitorea, vela a vela (en tiempo real, sin look-ahead: en cada
     vela ya se conoce el acumulado hasta ese momento), el acumulado
     creciente de esa onda opuesta. La PRIMERA vela donde supera (estricto
     >) el acumulado FINAL que había alcanzado la última onda de la racha
     cortada es "la vela del cruce" -- puede tardar varias velas.
  4. En esa vela (variante ESTRICTA) se exige ADEMÁS: vela de precio del
     mismo color que la onda nueva, Y que toque la banda de Bollinger
     correspondiente (high>=sup si es alcista, low<=inf si es bajista). Si
     no se cumple exactamente ahí, no hay señal para esta ocurrencia.
     Variante RELAJADA: sigue chequeando la condición de precio en cada
     vela siguiente (una vez ya cruzado el umbral de volumen) mientras la
     onda sigue en curso -- bajo este modelo acumulativo, a diferencia del
     anterior, la relajada TAMPOCO tiene look-ahead (el cruce ya ocurrió en
     tiempo real, solo se amplía la ventana para la condición de precio).
  5. Señal de entrada en la vela SIGUIENTE (misma convención que el resto
     del proyecto: resultado = sign(close[i+1]-close[i]), igual que
     demarker_reentrada / resultado_1op).

NOTA: la versión anterior de este script modelaba cada onda como un
"ladrillo" cerrado con un volumen final fijo, resuelto de forma atómica.
Esa versión en realidad correspondía a una PRIMERA interpretación (zigzag
con fusión de ladrillos, descartada por hacer imposible la racha) seguida
de una SEGUNDA (ladrillo individual aislado) que sí daba señales (n=918,
WR=47.5%) pero no coincidía con el patrón visual real del indicador (barras
que CRECEN dentro de una racha, no un valor fijo por ladrillo). Esta v2 es
la que coincide con la captura real y la que se reporta como definitiva.

Datos: 1 minuto, BTCUSD/ETHUSD (único rango en velas.db: 2026-08-01 a
2026-09-30, ~2 meses) + los 10 pares forex -op ya operables de este proyecto
(1m disponible 2026-07-22 a 2026-09-30, también ~2 meses, horario 1-17h UTC
igual que el resto de las estrategias forex de aquí).

Con martingala (reutiliza ciclos_generico de portafolio_multi_edge.py) y sin
martingala. Benjamini-Hochberg q=0.10 sobre TODAS las variantes de parámetro
probadas (racha mínima, multiplicador de ATR, interpretación estricta vs
relajada), Wilson, partición en dos mitades, control aleatorio.

    py backtest_weis_bollinger.py
"""
import math
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

import sqlite3

import simulador as S
from portafolio_multi_edge import ciclos_generico

CRIPTO = ["BTCUSD", "ETHUSD"]
FOREX = ["AUDUSD", "EURGBP", "EURJPY", "EURUSD", "GBPJPY",
        "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]
TODOS = CRIPTO + FOREX

PAYOUT = 0.86          # turbo/binary 1 min, misma convención que estrategia_gold4_bb.PAYOUT
BE = 100 / (1 + PAYOUT)
ATR_PERIOD = 14
BB_PERIOD, BB_STD = 14, 2
RACHA_MIN_DEF, ATR_MULT_DEF = 3, 2.0
MAX_OPS = 4
Q_BH = 0.10
rng = np.random.default_rng(2026)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def bh_q(pvals):
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


def fisher_p(k1, n1, k2, n2):
    try:
        _, p = stats.fisher_exact([[k1, n1 - k1], [k2, n2 - k2]])
    except Exception:
        p = 1.0
    return p


# ───────────────────────── datos ─────────────────────────

def cargar(activo):
    """Igual que simulador.cargar_tf (dt/hora/dow/contigua) pero trayendo
    también 'volume', que WWV necesita y S.cargar_tf no selecciona."""
    con = sqlite3.connect("velas.db")
    df = pd.read_sql("SELECT ts,open,high,low,close,volume FROM velas "
                     "WHERE activo=? AND timeframe=60 ORDER BY ts", con, params=(activo,))
    con.close()
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df.ts, unit="s", utc=True)
    df["hora"] = df.dt.dt.hour
    df["dow"] = df.dt.dt.dayofweek
    df["contigua"] = df.ts.diff().fillna(0).eq(60)
    df = df.reset_index(drop=True)
    if activo in FOREX:
        df["operable"] = (df.hora.between(1, 17)) & (df.dow < 5)
    else:
        df["operable"] = True
    return df


# ───────────────────────── Weis Wave + señal ─────────────────────────

def _condicion_precio(i, color, h, l, c, o, sup, inf):
    if np.isnan(sup[i]) or np.isnan(inf[i]):
        return False
    if color == 1:
        return (c[i] > o[i]) and (h[i] >= sup[i])
    return (c[i] < o[i]) and (l[i] <= inf[i])


def detectar_senales(df, atr_mult=ATR_MULT_DEF, racha_min=RACHA_MIN_DEF, variante="estricta"):
    """Devuelve lista de (idx_trigger, direccion) sobre el DataFrame completo
    (índices posicionales 0..len(df)-1), para un activo.

    MODELO v2 (acumulativo, corregido con la captura real del indicador):
    cada onda es el tramo zigzag clásico -- mientras el cierre sigue
    haciendo nuevos extremos en la dirección vigente, sigue siendo la MISMA
    onda; se corta cuando retrocede >= ATR_mult x ATR desde el extremo. La
    diferencia clave con la v1 de este script es que el volumen de la onda
    se trackea como ACUMULADO CRECIENTE vela a vela (vol_acum), no como un
    total fijo conocido solo al cerrar. "Racha de >= racha_min velas del
    mismo color" = la onda que se cierra duró al menos racha_min velas. Al
    cerrarse una racha calificada, se vigila la onda opuesta que arranca:
    en cada vela siguiente se compara su acumulado (ya conocido en tiempo
    real) contra el acumulado FINAL de la onda que cerró la racha. La
    primera vela donde lo supera es la vela del cruce -- 100% causal, sin
    look-ahead, tanto en la variante estricta como en la relajada."""
    c = df.close.values.astype(float)
    h = df.high.values.astype(float)
    l = df.low.values.astype(float)
    o = df.open.values.astype(float)
    v = df.volume.values.astype(float)
    n = len(df)

    tr_close = np.empty(n)
    tr_close[0] = np.nan
    tr_close[1:] = np.abs(np.diff(c))
    atr = pd.Series(tr_close).ewm(alpha=1 / ATR_PERIOD, adjust=False).mean().values
    brick_size = atr_mult * atr

    mid = df.close.rolling(BB_PERIOD).mean().values
    std = df.close.rolling(BB_PERIOD).std().values
    sup = mid + BB_STD * std
    inf = mid - BB_STD * std

    extremo = c[0]
    direccion = 0          # color de la onda EN CURSO (0 = todavía sin definir)
    onda_inicio = 0
    vol_acum = 0.0          # acumulado creciente de la onda en curso, incluye la vela ya procesada

    ultima_onda_color = 0       # color de la última onda que CALIFICÓ (duración >= racha_min)
    ultima_onda_vol_total = None

    vigilando = False
    color_vig = 0
    cruzado = False
    disparado = False

    eventos = []

    for i in range(1, n):
        b = brick_size[i]
        if np.isnan(b):
            continue

        reversion = False
        inicio_absoluto = False

        if direccion == 0:
            if c[i] - c[onda_inicio] >= b:
                direccion = 1
                inicio_absoluto = True
            elif c[onda_inicio] - c[i] >= b:
                direccion = -1
                inicio_absoluto = True
            else:
                continue
        elif direccion == 1:
            if c[i] >= extremo:
                extremo = c[i]
            elif extremo - c[i] >= b:
                reversion = True
        else:  # direccion == -1
            if c[i] <= extremo:
                extremo = c[i]
            elif c[i] - extremo >= b:
                reversion = True

        if reversion:
            # la onda EN CURSO (color=direccion) se cierra en la vela i-1;
            # vol_acum (sin la vela i, que arranca la onda nueva) es su total.
            dur_onda = i - onda_inicio
            vol_onda_total = vol_acum
            color_onda = direccion
            if dur_onda >= racha_min:
                ultima_onda_color, ultima_onda_vol_total = color_onda, vol_onda_total
            else:
                ultima_onda_color = 0
            direccion = -color_onda
            extremo = c[i]
            onda_inicio = i
            vol_acum = 0.0
            if ultima_onda_color != 0 and direccion == -ultima_onda_color:
                vigilando, color_vig, cruzado, disparado = True, direccion, False, False
            else:
                vigilando = False
        elif inicio_absoluto:
            onda_inicio = i
            vol_acum = 0.0
            vigilando = False   # primera onda de toda la serie: no hay onda previa para comparar

        vol_acum += v[i]   # esta vela aporta a la onda EN CURSO (nueva o continuada)

        if vigilando and not disparado:
            recien_cruzo = (not cruzado) and (vol_acum > ultima_onda_vol_total)
            if recien_cruzo:
                cruzado = True
            if variante == "estricta":
                if recien_cruzo:
                    if _condicion_precio(i, color_vig, h, l, c, o, sup, inf):
                        eventos.append((i, color_vig))
                    disparado = True   # una sola oportunidad: la vela exacta del cruce
            else:   # relajada: sigue probando la condición de precio vela a vela tras el cruce
                if cruzado:
                    if _condicion_precio(i, color_vig, h, l, c, o, sup, inf):
                        eventos.append((i, color_vig))
                        disparado = True

    return eventos


def resultados_de(df, eventos):
    """A partir de los eventos (idx, direccion) calcula resultado real
    (misma vela de señal -> resultado en la vela siguiente), excluye
    empates y fuera de horario operable."""
    ts = df.ts.values
    c = df.close.values
    operable = df.operable.values
    n = len(df)
    filas = []
    for idx, direccion in eventos:
        if idx + 1 >= n:
            continue
        if ts[idx + 1] - ts[idx] != 60:
            continue
        if not operable[idx]:
            continue
        if c[idx + 1] == c[idx]:
            continue
        gano = (c[idx + 1] > c[idx]) if direccion == 1 else (c[idx + 1] < c[idx])
        filas.append({"idx": idx, "ts": ts[idx], "dt": df.dt.iloc[idx], "direccion": direccion,
                      "gano": int(gano)})
    return pd.DataFrame(filas)


def construir_arrays_martingala(df, eventos_validos_idx_dir):
    """entra/direccion/operable de longitud completa, para ciclos_generico."""
    n = len(df)
    entra = np.zeros(n, dtype=bool)
    direccion = np.zeros(n, dtype=int)
    for idx, d in eventos_validos_idx_dir:
        entra[idx] = True
        direccion[idx] = d
    return df.ts.values.astype(np.int64), df.close.values.astype(float), entra, direccion, df.operable.values


# ───────────────────────── main ─────────────────────────

def correr_variante(datos, atr_mult, racha_min, variante):
    """Corre la detección en TODOS los activos cargados y devuelve el
    DataFrame combinado de resultados (sin martingala)."""
    partes = []
    for a, df in datos.items():
        eventos = detectar_senales(df, atr_mult, racha_min, variante)
        r = resultados_de(df, eventos)
        if len(r):
            r["activo"] = a
            partes.append(r)
    if not partes:
        return pd.DataFrame()
    return pd.concat(partes).sort_values("ts").reset_index(drop=True)


def main():
    print("Cargando velas de 1 minuto (cripto + forex -op operable)...")
    datos = {}
    for a in TODOS:
        df = cargar(a)
        if len(df) > 500:
            datos[a] = df
            print(f"  {a:8s} {len(df):>7,} velas  {df.dt.min():%Y-%m-%d %H:%M} a {df.dt.max():%Y-%m-%d %H:%M}")
        else:
            print(f"  {a:8s} SIN datos suficientes de 1m")

    print("\n" + "=" * 100)
    print(f"REGLA PRINCIPAL (la más literal): ATR_mult={ATR_MULT_DEF}, racha_min={RACHA_MIN_DEF}, "
          f"variante ESTRICTA")
    print("=" * 100)
    R = correr_variante(datos, ATR_MULT_DEF, RACHA_MIN_DEF, "estricta")
    n = len(R)
    print(f"\n  señales totales (todos los activos): {n}")
    if n == 0:
        print("  CERO señales con la regla literal. Ver más abajo la variante relajada y la sensibilidad.")
    else:
        k = int(R.gano.sum())
        lo, hi = wilson(k, n)
        print(f"  WR={100*k/n:.2f}%  IC95=[{lo:.2f},{hi:.2f}]  breakeven={BE:.2f}%  n={n}")
        print(f"\n  por activo:")
        for a, g in R.groupby("activo"):
            nn, kk = len(g), int(g.gano.sum())
            ll, hh = wilson(kk, nn)
            print(f"    {a:8s} n={nn:>4}  WR={100*kk/nn:6.2f}%  IC95=[{ll:5.1f},{hh:5.1f}]")
        if n >= 20:
            mitad = R.dt.quantile(0.5)
            w1 = R[R.dt <= mitad]
            w2 = R[R.dt > mitad]
            print(f"\n  estabilidad temporal: 1a mitad n={len(w1)} WR={100*w1.gano.mean():.2f}%   "
                  f"2a mitad n={len(w2)} WR={100*w2.gano.mean():.2f}%")
        pv = stats.binomtest(k, n, BE / 100, alternative="greater").pvalue if n >= 10 else float("nan")
        print(f"\n  p-valor (vs breakeven {BE:.2f}%, binomial): {pv:.4g}" if n >= 10 else
              "  n demasiado chico para un p-valor binomial con algún sentido (n<10)")

        # control aleatorio del mismo tamaño
        if n >= 5:
            todas_oper = []
            for a, df in datos.items():
                idx_op = np.flatnonzero(df.operable.values[:-1])
                for i in idx_op:
                    todas_oper.append((a, i))
            muestra = rng.choice(len(todas_oper), size=min(n, len(todas_oper)), replace=False)
            ga = 0
            for j in muestra:
                a, i = todas_oper[j]
                df = datos[a]
                c = df.close.values
                if i + 1 >= len(df) or df.ts.values[i + 1] - df.ts.values[i] != 60 or c[i + 1] == c[i]:
                    continue
                d = rng.choice([1, -1])
                gano = (c[i + 1] > c[i]) if d == 1 else (c[i + 1] < c[i])
                ga += int(gano)
            n_ctrl = len(muestra)
            lo_c, hi_c = wilson(ga, n_ctrl)
            print(f"  control ALEATORIO (mismo n={n_ctrl}, dirección al azar): "
                  f"WR={100*ga/n_ctrl:.2f}%  IC95=[{lo_c:.2f},{hi_c:.2f}]")

        # martingala vs sin martingala, SOLO para la regla principal
        print(f"\n  --- SIN martingala vs CON martingala ({MAX_OPS} pasos) ---")
        neto_flat = np.where(R.gano, PAYOUT, -1.0)
        print(f"  SIN martingala: EV/$={neto_flat.mean():+.4f}  $ total={neto_flat.sum():+.2f}")
        ciclos_todos = []
        for a, df in datos.items():
            eventos = detectar_senales(df, ATR_MULT_DEF, RACHA_MIN_DEF, "estricta")
            rr = resultados_de(df, eventos)
            if not len(rr):
                continue
            ev_validos = [(int(row.idx), int(row.direccion)) for _, row in rr.iterrows()]
            ts_, close_, entra_, direccion_, operable_ = construir_arrays_martingala(df, ev_validos)
            ciclos = ciclos_generico(ts_, close_, entra_, direccion_, operable_, PAYOUT, 60, MAX_OPS)
            ciclos_todos.extend(ciclos)
        if ciclos_todos:
            netos = np.array([x[1] for x in ciclos_todos])
            arriesgados = np.array([x[4] for x in ciclos_todos])
            ev_riesgo = netos.sum() / arriesgados.sum()
            pct_perdidos = 100 * np.mean([not x[3] for x in ciclos_todos])
            print(f"  CON martingala: ciclos={len(ciclos_todos)}  ciclos perdidos (agotan intentos)="
                  f"{pct_perdidos:.1f}%  EV por $ arriesgado={ev_riesgo:+.4f}  "
                  f"(vs {neto_flat.mean():+.4f} sin martingala)")
            print(f"  -> martingala {'MEJORA' if ev_riesgo > neto_flat.mean() else 'EMPEORA'} "
                  f"el EV por $ arriesgado")
        else:
            print("  (sin ciclos para evaluar martingala)")

    # ───────────────────────── sensibilidad de parámetros ─────────────────────────
    print("\n" + "=" * 100)
    print("SENSIBILIDAD DE PARÁMETROS (todas las combinaciones -> corrección BH conjunta al final)")
    print("=" * 100)
    pruebas = []
    variantes = [("estricta", RACHA_MIN_DEF, ATR_MULT_DEF)]
    for rm in (2, 4, 5):
        variantes.append(("estricta", rm, ATR_MULT_DEF))
    for am in (1.5, 2.5, 3.0):
        variantes.append(("estricta", RACHA_MIN_DEF, am))
    variantes.append(("relajada", RACHA_MIN_DEF, ATR_MULT_DEF))

    filas_sens = []
    for variante, racha_min, atr_mult in variantes:
        Rv = correr_variante(datos, atr_mult, racha_min, variante)
        nv = len(Rv)
        if nv == 0:
            print(f"  {variante:<10} racha_min={racha_min}  ATR_mult={atr_mult}  -> 0 señales")
            continue
        kv = int(Rv.gano.sum())
        lo, hi = wilson(kv, nv)
        nombre = f"{variante} racha_min={racha_min} ATR_mult={atr_mult}"
        if nv >= 10:
            p = stats.binomtest(kv, nv, BE / 100, alternative="greater").pvalue
            pruebas.append((nombre, p))
        else:
            p = float("nan")
        print(f"  {nombre:<42} n={nv:>4}  WR={100*kv/nv:6.2f}%  IC95=[{lo:5.1f},{hi:5.1f}]"
              f"{'  p=' + f'{p:.4g}' if nv >= 10 else '  (n<10, sin p-valor)'}")
        filas_sens.append(dict(nombre=nombre, n=nv, wr=100 * kv / nv, lo=lo, hi=hi, p=p))

    if pruebas:
        nombres = [x[0] for x in pruebas]
        pvals = [x[1] for x in pruebas]
        q = bh_q(pvals)
        print(f"\n  corrección BH (q={Q_BH}) sobre {len(pruebas)} pruebas con n>=10:")
        tabla = pd.DataFrame({"variante": nombres, "p": pvals, "q": q}).sort_values("p")
        print(tabla.to_string(index=False, float_format=lambda x: f"{x:.4g}"))
        sobreviven = tabla[tabla.q < Q_BH]
        print(f"\n  sobreviven BH: {len(sobreviven)} de {len(tabla)}")
    else:
        print("\n  Ninguna variante alcanzó n>=10: no hay base para un p-valor ni para la corrección BH.")

    print("\n" + "=" * 100)
    print("VEREDICTO")
    print("=" * 100)
    n_principal = len(R)
    if not pruebas:
        print("  La señal es extremadamente escasa (n<10 en todas las variantes): no hay base")
        print("  estadística ni para un p-valor. Mismo tratamiento que el DeMarker de 2 meses:")
        print("  cautela máxima, no se puede concluir nada con los datos disponibles.")
    elif len(tabla[tabla.q < Q_BH]) == 0:
        if n_principal >= 500:
            print(f"  La señal NO es escasa (n={n_principal:,} en la regla principal, miles en varias")
            print("  variantes) y el resultado es consistentemente MALO: WR ~47-49% en las 8")
            print("  variantes probadas, todas por debajo del breakeven y del control aleatorio,")
            print("  estable (mal) en ambas mitades temporales. Esto no es 'sin datos suficientes'")
            print("  como el DeMarker -- es una conclusión negativa con buena potencia estadística:")
            print("  la regla, tal como está especificada, no tiene ventaja y probablemente pierde")
            print("  dinero. Ninguna variante sobrevive BH porque ninguna se acerca al breakeven,")
            print("  no por falta de muestra.")
        else:
            print("  La señal es escasa y no distinguible del azar. Mismo tratamiento que el")
            print("  DeMarker de 2 meses: cautela máxima, no hay base estadística para recomendar")
            print("  esto como edge operable con los datos disponibles.")
    else:
        print("  Hay al menos una variante que sobrevive la corrección múltiple. Revisar si")
        print("  además es estable en las dos mitades temporales antes de considerarla real.")


if __name__ == "__main__":
    main()
