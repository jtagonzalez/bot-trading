import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Gold 4.0 + filtro de tendencia: ¿mejora si solo opera cuando hay tendencia
y nunca con el precio estable?

Filtros (se aplican encima de la señal del Gold 4.0):
  RSI 55/45        CALL solo si RSI14 > 55, PUT solo si RSI14 < 45
                   (45-55 = precio estable: no opera)
  RSI 60/40        versión estricta
  Estocástico 60/40  lo mismo con %K(14)
  ADX > 25         solo si hay tendencia definida (fuerza, sin dirección)
  ADX > 25 + RSI 55/45
  RSI clásico 30/70  CALL con RSI < 30, PUT con RSI > 70 (uso contrario,
                   el habitual de "sobrecompra/sobreventa")

Se evalúa con velas de 1 minuto (10 pares, horario operable 01-17h UTC) y
vencimientos de 1 y 5 min, operación por operación y con los ciclos del
usuario (misma moneda, mismo sentido, hasta 10 intentos, 10 ciclos al día).

    py simular_gold4_filtros.py
"""
import math
import sqlite3

import numpy as np
import pandas as pd

from simular_gold4 import PARES, senales

PAYOUT = 0.86
BE = 100 / (1 + PAYOUT)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def rsi(c, n=14):
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    p = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / p.replace(0, np.nan))


def adx(h, l, c, n=14):
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(pdm, index=h.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(mdm, index=h.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def estocastico(h, l, c, n=14):
    return 100 * (c - l.rolling(n).min()) / (h.rolling(n).max() - l.rolling(n).min()).replace(0, np.nan)


FILTROS = {
    "sin filtro":            lambda r, s, a, up: np.ones(len(r), bool),
    "RSI 55/45":             lambda r, s, a, up: np.where(up, r > 55, r < 45),
    "RSI 60/40":             lambda r, s, a, up: np.where(up, r > 60, r < 40),
    "Estocastico 60/40":     lambda r, s, a, up: np.where(up, s > 60, s < 40),
    "ADX > 25":              lambda r, s, a, up: a > 25,
    "ADX>25 + RSI 55/45":    lambda r, s, a, up: (a > 25) & np.where(up, r > 55, r < 45),
    "RSI clasico 30/70":     lambda r, s, a, up: np.where(up, r < 30, r > 70),
}


def preparar():
    c = sqlite3.connect("velas.db")
    out = {}
    for par in PARES:
        v = pd.read_sql("SELECT ts, open, high, low, close FROM velas WHERE activo=? "
                        "AND timeframe=60 ORDER BY ts", c, params=(par,))
        v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
        sig = dict(zip(("A_up", "A_dn", "B_up", "B_dn"), senales(v)))
        out[par] = (v, sig, rsi(v.close).values, estocastico(v.high, v.low, v.close).values,
                    adx(v.high, v.low, v.close).values)
    return out


def indices(datos, N, sistema, filtro):
    """Por par: índices de señales CALL y PUT que pasan el filtro, y resultados."""
    res = {}
    for par, (v, sig, r, s, a) in datos.items():
        ts, cl = v.ts.values, v.close.values
        ok = np.r_[ts[N:] - ts[:-N] == 60 * N, [False] * N]
        oper = (v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)).values
        base = ok & oper
        resultado = np.sign(np.r_[cl[N:] - cl[:-N], [0] * N])
        f_up = FILTROS[filtro](r, s, a, np.ones(len(v), bool))
        f_dn = FILTROS[filtro](r, s, a, np.zeros(len(v), bool))
        up = np.flatnonzero(sig[f"{sistema}_up"].values & base & np.nan_to_num(f_up).astype(bool))
        dn = np.flatnonzero(sig[f"{sistema}_dn"].values & base & np.nan_to_num(f_dn).astype(bool))
        res[par] = {"ts": ts, "dia": v.dt.dt.normalize().values, "res": resultado,
                    1: up, -1: dn}
    return res


def sueltas(ix):
    g = p = 0
    for d in ix.values():
        for sg in (1, -1):
            r = d["res"][d[sg]] * sg
            g += int((r > 0).sum()); p += int((r < 0).sum())
    return g, p


def ciclos(ix, N, dias, rng):
    tot_g = tot_p = gan = n_c = 0
    for _ in range(10):                      # 10 sorteos
        for dia in dias:
            for _ in range(10):              # 10 ciclos por dia
                par = PARES[rng.integers(len(PARES))]
                d = ix[par]
                ini = pd.Timestamp(dia).timestamp() + rng.integers(3600, 18 * 3600)
                cand = []
                for sg in (1, -1):
                    L = d[sg]
                    k = np.searchsorted(d["ts"][L], ini) if len(L) else 0
                    if k < len(L) and d["dia"][L[k]] == dia:
                        cand.append((d["ts"][L[k]], sg))
                if not cand:
                    continue
                t0, sg = min(cand)
                L = d[sg]
                j = np.searchsorted(d["ts"][L], t0)
                intentos = []
                while j < len(L) and len(intentos) < 10:
                    i = L[j]
                    if d["dia"][i] != dia:
                        break
                    x = int(d["res"][i] * sg)
                    intentos.append(x)
                    if x > 0:
                        break
                    j = np.searchsorted(L, i + N + 1)
                n_c += 1
                gan += int(1 in intentos)
                tot_g += intentos.count(1); tot_p += intentos.count(-1)
    return n_c, gan, tot_g, tot_p


def main():
    datos = preparar()
    # mismas fechas (datetime64) que usan los indices, o nunca coinciden
    dias = sorted(set(np.concatenate([v.dt.dt.normalize().values for v, *_ in datos.values()])))
    dias = [x for x in dias if pd.Timestamp(x).weekday() < 5]
    n_dias = len(dias)
    print(f"10 pares · velas de 1 min · {n_dias} días hábiles · breakeven turbo {BE:.2f}%\n")
    for N in (1, 5):
        for sistema, nombre in (("A", "flechas Up/Down"), ("B", "COMPRAR/VENDER")):
            print("=" * 104)
            print(f"GOLD 4.0 sistema {sistema} ({nombre}) · vencimiento {N} min")
            print("=" * 104)
            print(f"  {'filtro':<22}{'señales/día':>12}{'acierto':>9}{'IC95':>16}"
                  f"{'$/día':>8}   {'ciclos: ganan':>14}{'acierto':>9}{'$/día':>8}")
            rng = np.random.default_rng(7)
            for filtro in FILTROS:
                ix = indices(datos, N, sistema, filtro)
                g, p = sueltas(ix)
                if g + p < 50:
                    print(f"  {filtro:<22}  muy pocas señales ({g + p})")
                    continue
                lo, hi = wilson(g, g + p)
                dia = (g * PAYOUT - p) / n_dias
                n_c, gan, cg, cp = ciclos(ix, N, dias, rng)
                marca = "  <-- supera BE" if lo > BE else ""
                ciclo_txt = (f"{100 * gan / n_c:>13.1f}%{100 * cg / max(cg + cp, 1):>8.2f}%"
                             f"{(cg * PAYOUT - cp) / (n_dias * 10):>+8.2f}") if n_c else "   sin ciclos"
                print(f"  {filtro:<22}{(g + p) / n_dias:>12.1f}{100 * g / (g + p):>8.2f}%"
                      f"  [{lo:5.2f},{hi:5.2f}]{dia:>+8.2f}   {ciclo_txt}{marca}")
            print()


if __name__ == "__main__":
    main()
