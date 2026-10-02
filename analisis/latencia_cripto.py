import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
¿Sobrevive la ventaja cripto a entrar tarde?

El backtest supone entrar al precio exacto del cierre de la vela. El bot en
vivo entra ~9 s después (mediana; p90 18 s). En cripto el precio se mueve
rápido: si la reversión ocurre en los primeros segundos, en vivo desaparece.

Con velas de 1 minuto se simula entrar 1 y 2 MINUTOS tarde — seis y doce veces
peor que la latencia real. El vencimiento sigue siendo el mismo (la opción
binaria vence en la rejilla de 15 min), así que entrar tarde solo empeora el
precio de entrada, que es justo lo que se quiere medir.

    py latencia_cripto.py
"""
import math
import sqlite3

import numpy as np
import pandas as pd

BE = 100 / 1.87


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    mar = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (cen - mar), 100 * (cen + mar))


def main():
    D = pd.read_pickle("oos_cripto.pkl")
    D["ts"] = D.dt.astype("int64") // 10**9
    c = sqlite3.connect("velas.db")
    print(f"{'activo':<8}{'nivel':>6}{'entrada':>12}{'n':>7}{'WR':>8}{'IC95':>16}   vs BE {BE:.2f}%")
    for a in D.activo.unique():
        m1 = dict(c.execute("SELECT ts, close FROM velas WHERE activo=? AND timeframe=60",
                            (a,)))
        if not m1:
            print(f"{a}: sin velas de 1 minuto")
            continue
        g = D[D.activo == a].copy()
        cierre_decision = g.ts + 900                 # cuando se decide
        venc = g.ts + 1800                           # cuando vence la opcion
        precio_venc = (venc - 60).map(m1)            # cierre de la ultima vela de 1m
        for nivel in ("op10", "op20", "op50"):
            s = g[g[nivel]]
            for retraso, etiqueta in ((0, "al cierre"), (1, "+1 min"), (2, "+2 min")):
                # retraso 0: cierre de la vela de 1m que termina en la decision
                entrada = (cierre_decision.loc[s.index] + (retraso - 1) * 60).map(m1)
                fin = precio_venc.loc[s.index]
                ok = entrada.notna() & fin.notna() & (fin != entrada)
                call = s.p[ok] > 0.5
                subio = fin[ok] > entrada[ok]
                gano = (call == subio)
                n, k = int(ok.sum()), int(gano.sum())
                if n < 50:
                    continue
                lo, hi = wilson(k, n)
                v = "sobre BE" if lo > BE else ("en zona" if 100 * k / n > BE else "BAJO BE")
                print(f"{a:<8}{nivel[2:] + '%':>6}{etiqueta:>12}{n:>7,}{100 * k / n:>7.2f}%"
                      f"  [{lo:5.2f},{hi:5.2f}]   {v}")
        print()


if __name__ == "__main__":
    main()
