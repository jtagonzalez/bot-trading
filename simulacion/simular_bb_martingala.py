import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Gold 4.0 + Bollinger (14,2) con martingala CONTINUA a 1 minuto.

Reglas (las del usuario):
  * entrada: el setup de las capturas, en velas de 1 min (ver simular_gold4_bb.py)
  * operación de 1 min; si la vela cierra perdedora, en la vela SIGUIENTE se abre
    automáticamente otra de 1 min en el MISMO sentido, sin esperar señal
  * monto x2,16 tras cada pérdida: con pago 86% recupera lo perdido + $1
  * "máximo 6 veces": se calcula como 6 operaciones en total y como la inicial
    más 6 martingalas (7 en total)
  * empate: el broker devuelve lo apostado; se repite el mismo monto en la vela
    siguiente, consumiendo un intento
  * con un ciclo abierto en un par, se ignoran sus señales nuevas

Forex (10 pares, 01-17h UTC lun-vie) y cripto (BTC, ETH, 24x7). Monto base $1.

    py simular_bb_martingala.py
"""
import sqlite3

import numpy as np
import pandas as pd

from simular_gold4 import senales

PAYOUT = 0.86
MULT = (1 + PAYOUT) / PAYOUT
FOREX = ["AUDUSD", "EURGBP", "EURJPY", "EURUSD", "GBPJPY",
         "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]
CRIPTO = ["BTCUSD", "ETHUSD"]


def setup(v):
    _, _, up_b, dn_b = senales(v)
    cian = v.close.rolling(50).mean()
    mid = v.close.rolling(14).mean()
    sd = v.close.rolling(14).std(ddof=0)
    sup, inf = mid + 2 * sd, mid - 2 * sd
    venta_bb = (v.close < cian) & (v.close < v.open) & (v.high >= sup)
    compra_bb = (v.close > cian) & (v.close > v.open) & (v.low <= inf)
    return {"estricto (con Gold)": (compra_bb & up_b, venta_bb & dn_b),
            "sin Gold": (compra_bb, venta_bb)}


def ciclos_de(v, compra, venta, operable, max_ops):
    ts, cl = v.ts.values, v.close.values
    n = len(v)
    senal = np.where(compra.values, 1, np.where(venta.values, -1, 0))
    out = []                        # (ts_inicio, neto, pasos, gano)
    i = 0
    while i < n - 1:
        if senal[i] == 0 or not operable[i]:
            i += 1
            continue
        sentido, apuesta, neto, k, j, gano = senal[i], 1.0, 0.0, 0, i, False
        while k < max_ops and j + 1 < n and ts[j + 1] - ts[j] == 60 and operable[j]:
            r = np.sign(cl[j + 1] - cl[j]) * sentido
            k += 1
            if r > 0:
                neto += apuesta * PAYOUT
                gano = True
                j += 1
                break
            if r < 0:
                neto -= apuesta
                apuesta *= MULT
            j += 1                  # la siguiente operacion entra en la vela siguiente
        if k:
            out.append((ts[i], neto, k, gano))
        i = max(j, i + 1)           # no se abren ciclos nuevos con uno abierto
    return out


def main():
    c = sqlite3.connect("velas.db")
    for grupo, activos, cripto in (("FOREX (10 pares)", FOREX, False),
                                   ("CRIPTO (BTC, ETH)", CRIPTO, True)):
        datos = []
        for a in activos:
            v = pd.read_sql("SELECT ts, open, high, low, close FROM velas WHERE activo=? "
                            "AND timeframe=60 ORDER BY ts", c, params=(a,))
            v["dt"] = pd.to_datetime(v.ts, unit="s", utc=True)
            op = (np.ones(len(v), bool) if cripto else
                  (v.dt.dt.hour.between(1, 17) & (v.dt.dt.weekday < 5)).values)
            datos.append((a, v, setup(v), op))
        dias = len({d for _, v, _, op in datos for d in v.dt.dt.normalize()[op].unique()})

        for variante in ("estricto (con Gold)", "sin Gold"):
            for max_ops, etiqueta in ((6, "6 operaciones en total"),
                                      (7, "inicial + 6 martingalas")):
                cs = []
                for a, v, s, op in datos:
                    compra, venta = s[variante]
                    cs += ciclos_de(v, compra, venta, op, max_ops)
                if not cs:
                    continue
                cs.sort()
                netos = np.array([x[1] for x in cs])
                gano = np.array([x[3] for x in cs])
                pasos = np.array([x[2] for x in cs])
                curva = np.cumsum(netos)
                caida = (np.maximum.accumulate(np.r_[0, curva])[1:] - curva).max()
                riesgo = sum(MULT ** k for k in range(max_ops))
                perdidos = int((~gano).sum())
                print("=" * 96)
                print(f"{grupo} · {variante} · {etiqueta}")
                print("=" * 96)
                print(f"  ciclos: {len(cs):,} en {dias} días ({len(cs) / dias:.1f}/día)")
                print(f"  ciclos que terminan ganando: {100 * gano.mean():.2f}%   "
                      f"perdidos enteros: {perdidos} ({100 * perdidos / len(cs):.2f}%)")
                dist = "  ".join(f"{k}:{100 * ((pasos == k) & gano).sum() / max(gano.sum(), 1):.0f}%"
                                 for k in range(1, max_ops + 1))
                print(f"  ganado en el intento  {dist}")
                print(f"  un ciclo perdido entero cuesta ${riesgo:,.2f} "
                      f"(última apuesta ${MULT ** (max_ops - 1):,.2f})")
                print(f"\n  RESULTADO TOTAL: ${netos.sum():+,.2f}   por día: ${netos.sum() / dias:+.2f}")
                print(f"  lo ganado por los ciclos buenos: ${netos[gano].sum():+,.2f}")
                print(f"  lo perdido por los {perdidos} ciclos malos: ${netos[~gano].sum():+,.2f}")
                print(f"  peor racha (caída máxima del saldo): ${caida:,.2f}  "
                      f"-> saldo mínimo necesario para no quebrar a $1 de base")
                print()


if __name__ == "__main__":
    main()
