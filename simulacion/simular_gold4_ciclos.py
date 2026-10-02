import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Gold 4.0 con ciclos de recuperación — la regla que pidió el usuario:

  1. Se elige un momento al azar dentro del horario operable (01-17h UTC,
     lunes a viernes) y un par al azar.
  2. Se espera la primera señal del Gold 4.0 en ese par: "Up" -> CALL,
     "Down" -> PUT. Se opera a 1 minuto.
  3. Si gana, el ciclo termina.
     Si pierde, se espera la SIGUIENTE señal del MISMO par en el MISMO
     sentido y se vuelve a operar. Máximo 10 intentos por ciclo.
     Empate: el broker devuelve lo apostado; no gana ni pierde, y el ciclo
     sigue buscando la siguiente señal (consume un intento).
  4. 10 ciclos por día.

Monto fijo de $1 por intento (la regla no pide subir la apuesta). Aparte se
calcula la variante martingala (subir la apuesta tras cada pérdida para
recuperar), porque es con lo que suele usarse esta lógica.

Pago de la opción turbo de 1 min en -op: 86%.
Se repite todo con 30 sorteos distintos de puntos de inicio.

    py simular_gold4_ciclos.py
"""
import sqlite3

import numpy as np
import pandas as pd

from simular_gold4 import PARES, senales

PAYOUT = 0.86
INTENTOS = 10
CICLOS_DIA = 10
SORTEOS = 30
MULT_MARTINGALA = (1 + PAYOUT) / PAYOUT     # recupera lo perdido + la ganancia de 1


def preparar():
    c = sqlite3.connect("velas.db")
    datos = {}
    for par in PARES:
        v = pd.read_sql("SELECT ts, open, high, low, close FROM velas WHERE activo=? "
                        "AND timeframe=60 ORDER BY ts", c, params=(par,))
        v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
        up_a, dn_a, up_b, dn_b = senales(v)
        ts = v.ts.values
        sig_ok = np.r_[ts[1:] - ts[:-1] == 60, False]          # vela siguiente existe
        operable = (v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)).values
        base = sig_ok & operable
        cl = v.close.values
        resultado = np.sign(np.r_[cl[1:] - cl[:-1], 0])          # +1 subio, -1 bajo, 0 empate
        dia = v.dt.dt.normalize().values
        datos[par] = {
            "ts": ts, "dia": dia, "res": resultado,
            "A": {+1: np.flatnonzero(up_a.values & base), -1: np.flatnonzero(dn_a.values & base)},
            "B": {+1: np.flatnonzero(up_b.values & base), -1: np.flatnonzero(dn_b.values & base)},
        }
    return datos


def ciclo(d, sistema, inicio_ts, dia):
    """Devuelve la lista de resultados de cada intento: +1 gana, -1 pierde, 0 empate."""
    ups, dns = d[sistema][+1], d[sistema][-1]
    ts, dias, res = d["ts"], d["dia"], d["res"]
    # primera señal (cualquier sentido) desde el inicio, el mismo día
    iu = np.searchsorted(ts[ups], inicio_ts) if len(ups) else 0
    idn = np.searchsorted(ts[dns], inicio_ts) if len(dns) else 0
    cand = []
    if iu < len(ups) and dias[ups[iu]] == dia:
        cand.append((ts[ups[iu]], +1))
    if idn < len(dns) and dias[dns[idn]] == dia:
        cand.append((ts[dns[idn]], -1))
    if not cand:
        return None
    t0, sentido = min(cand)
    lista = ups if sentido == +1 else dns
    j = np.searchsorted(ts[lista], t0)
    intentos = []
    while j < len(lista) and len(intentos) < INTENTOS:
        i = lista[j]
        if dias[i] != dia:
            break                                   # se acabo el horario del dia
        r = res[i] * sentido
        intentos.append(int(r))
        if r > 0:
            break
        # siguiente señal en el mismo sentido DESPUÉS del vencimiento
        j = np.searchsorted(lista, i + 2)
    return intentos


def main():
    datos = preparar()
    todos_dias = sorted(set(np.concatenate([d["dia"][d["res"] != 0] for d in datos.values()])))
    todos_dias = [x for x in todos_dias if pd.Timestamp(x).weekday() < 5]
    n_dias = len(todos_dias)
    print(f"{len(PARES)} pares · velas de 1 min · {n_dias} días hábiles · "
          f"{CICLOS_DIA} ciclos/día · {SORTEOS} sorteos\n")

    for sistema, nombre in (("A", "flechas Up/Down"), ("B", "COMPRAR/VENDER")):
        rng = np.random.default_rng(42)
        ciclos = []
        for s in range(SORTEOS):
            for dia in todos_dias:
                for _ in range(CICLOS_DIA):
                    par = PARES[rng.integers(len(PARES))]
                    base = pd.Timestamp(dia).timestamp()
                    inicio = base + rng.integers(3600, 18 * 3600)
                    r = ciclo(datos[par], sistema, inicio, dia)
                    if r:
                        ciclos.append(r)

        n_c = len(ciclos)
        gano_ciclo = sum(1 for c in ciclos if 1 in c)
        agoto = sum(1 for c in ciclos if 1 not in c and len(c) == INTENTOS)
        corto = n_c - gano_ciclo - agoto
        trades = [x for c in ciclos for x in c]
        g, p, e = trades.count(1), trades.count(-1), trades.count(0)
        pnl_fijo = g * PAYOUT - p
        dias_sim = n_dias * SORTEOS

        # martingala: apuesta 1, tras pérdida multiplica; empate repite la misma apuesta
        pnl_m, peor, pnl_dia = 0.0, 0.0, []
        for c in ciclos:
            apuesta, neto = 1.0, 0.0
            for x in c:
                if x == 1:
                    neto += apuesta * PAYOUT
                    break
                if x == -1:
                    neto -= apuesta
                    apuesta *= MULT_MARTINGALA
            pnl_m += neto
            peor = min(peor, neto)

        print("=" * 90)
        print(f"GOLD 4.0 · sistema {sistema} ({nombre})")
        print("=" * 90)
        print(f"  ciclos simulados: {n_c:,}  ({n_c / dias_sim:.1f} por día)")
        print(f"  ciclos que TERMINAN GANANDO: {100 * gano_ciclo / n_c:.1f}%")
        print(f"  ciclos que agotan los 10 intentos sin ganar: {100 * agoto / n_c:.1f}%")
        print(f"  ciclos cortados por fin de horario sin ganar: {100 * corto / n_c:.1f}%")
        dist = pd.Series([len(c) for c in ciclos if 1 in c]).value_counts().sort_index()
        print("  intentos que hicieron falta para ganar:",
              "  ".join(f"{k}:{100 * v / gano_ciclo:.0f}%" for k, v in dist.items() if k <= 5),
              "  6+:", f"{100 * dist[dist.index > 5].sum() / gano_ciclo:.0f}%")
        print(f"\n  operaciones: {len(trades):,}  ({len(trades) / dias_sim:.1f} por día)")
        print(f"  ganadas {g:,} · perdidas {p:,} · empates {e:,}")
        print(f"  acierto por operación (sin empates): {100 * g / (g + p):.2f}%   "
              f"breakeven: {100 / (1 + PAYOUT):.2f}%")
        print(f"\n  MONTO FIJO $1:   resultado por día  ${pnl_fijo / dias_sim:+.2f}   "
              f"por mes (21 días)  ${21 * pnl_fijo / dias_sim:+.2f}")
        print(f"  MARTINGALA x{MULT_MARTINGALA:.2f}: resultado por día  ${pnl_m / dias_sim:+.2f}   "
              f"peor ciclo  ${peor:,.2f}")
        print(f"     (el 10º intento de un ciclo exige apostar "
              f"${MULT_MARTINGALA ** 9:,.0f} para recuperar $1)")
        print()


if __name__ == "__main__":
    main()
