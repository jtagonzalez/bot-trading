import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
RESOLVER — rellena el desenlace de las predicciones que nunca se operaron.

El bot registra TODAS sus evaluaciones, pero solo opera las que pasan el
umbral: en el run 3 fueron 20 de 232. De las otras 212 sabiamos que dijo el
modelo y nada de lo que paso despues.

Este script contesta a "?que habria pasado?" para todas ellas. La vela que
decide es la SIGUIENTE a la de la señal, y sigue estando en el feed del
broker, asi que el dato es recuperable meses despues.

Sirve para dos cosas concretas:

  * EVALUAR EL UMBRAL. Si las descartadas aciertan tanto como las operadas,
    el umbral no esta seleccionando nada y solo reduce el numero de
    operaciones. Si aciertan menos, el umbral funciona.

  * MULTIPLICAR LA MUESTRA. 232 evaluaciones etiquetadas valen mucho mas que
    20 operaciones para medir si el modelo acierta.

El bot ya resuelve sobre la marcha las velas que descarga en cada ciclo. Este
script es para el historico acumulado y para los activos que el bot ya no mira.

Uso:
    py resolver_predicciones.py                  # todo lo pendiente
    py resolver_predicciones.py --run 3
    py resolver_predicciones.py --resumen        # solo mostrar, sin conectar
"""
import argparse
import io
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

from registro import Registro


def pendientes(db, run=None):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    ahora = int(time.time())
    q = """SELECT activo, timeframe, COUNT(*) n, MIN(ts_vela) desde, MAX(ts_vela) hasta
           FROM predicciones
           WHERE resultado_virtual IS NULL AND ts_vela + 2*timeframe <= ?"""
    args = [ahora]
    if run:
        q += " AND run_id=?"
        args.append(run)
    q += " GROUP BY activo, timeframe ORDER BY activo"
    filas = c.execute(q, args).fetchall()
    c.close()
    return filas


def resumen(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    print("\nEstado de resolucion:")
    for r in c.execute("""SELECT run_id,
                                 COUNT(*) total,
                                 SUM(CASE WHEN resultado_virtual IS NOT NULL
                                          THEN 1 ELSE 0 END) resueltas,
                                 SUM(operada) operadas
                          FROM predicciones GROUP BY run_id ORDER BY run_id"""):
        print(f"  run {r['run_id']}: {r['total']:>5,} evaluaciones  "
              f"{r['resueltas']:>5,} resueltas  {r['operadas']:>4,} operadas")

    print("\n?Esta bien puesto el umbral?  (WR virtual de cada grupo)")
    filas = c.execute("SELECT * FROM v_umbral WHERE resueltas > 0 "
                      "ORDER BY run_id, grupo").fetchall()
    if not filas:
        print("  todavia no hay predicciones resueltas")
    else:
        print(f"  {'run':>4} {'grupo':<12}{'n':>7}{'resueltas':>11}{'WR':>9}"
              f"{'confianza':>12}")
        print("  " + "-" * 56)
        for r in filas:
            wr = f"{r['wr_virtual']:.2f}%" if r["wr_virtual"] is not None else "—"
            print(f"  {r['run_id']:>4} {r['grupo']:<12}{r['n']:>7,}"
                  f"{r['resueltas']:>11,}{wr:>9}{r['confianza_media']:>12.5f}")
        print("\n  Si el grupo 'operada' no acierta mas que el 'descartada',")
        print("  el umbral no esta seleccionando nada util.")
    c.close()


def credenciales():
    src = io.open("bot_indicadores.py", encoding="utf-8").read()
    return (re.search(r'"email":\s*"([^"]+)"', src).group(1),
            re.search(r'"password":\s*"([^"]+)"', src).group(1))


def conectar():
    from iqoptionapi.stable_api import IQ_Option
    email, pwd = credenciales()
    api = IQ_Option(email, pwd)
    api.connect()
    for _ in range(30):
        if api.check_connect():
            break
        time.sleep(1)
    if not api.check_connect():
        sys.exit("no se pudo conectar a IQ Option")
    api.change_balance("PRACTICE")     # solo lectura de velas, nunca opera
    # La libreria no conoce los '-op' ni otros 255 activos; sus ids estan en init_v2
    try:
        import iqoptionapi.constants as OP
        init = api.get_all_init_v2() or {}
        for tipo in ("turbo", "binary"):
            for k, v in ((init.get(tipo) or {}).get("actives") or {}).items():
                nom = (v.get("name") or "").split(".")[-1]
                if nom and nom not in OP.ACTIVES:
                    OP.ACTIVES[nom] = int(k)
    except Exception:
        pass
    return api


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="live.db")
    ap.add_argument("--run", type=int, default=None)
    ap.add_argument("--resumen", action="store_true",
                    help="solo mostrar el estado, sin conectar")
    a = ap.parse_args()

    if a.resumen:
        Registro(a.db)          # asegura que existan columnas y vistas
        resumen(a.db)
        return

    reg = Registro(a.db)        # crea columnas nuevas si hacen falta
    filas = pendientes(a.db, a.run)
    if not filas:
        print("No hay predicciones pendientes de resolver.")
        resumen(a.db)
        return

    total = sum(f["n"] for f in filas)
    print(f"{total:,} predicciones pendientes en {len(filas)} combinaciones "
          f"activo/timeframe\n")

    api = conectar()
    print("conectado (PRACTICE, solo lectura de velas)\n")

    resueltas = 0
    for f in filas:
        activo, tf = f["activo"], f["timeframe"]
        # cuantas velas cubren el rango pendiente, con margen
        span = f["hasta"] - f["desde"] + 4 * tf
        n = min(1000, max(20, int(span / tf) + 10))
        print(f"  {activo:<14} tf={tf:>5}s  {f['n']:>4} pendientes  "
              f"({datetime.fromtimestamp(f['desde'], timezone.utc):%m-%d %H:%M} a "
              f"{datetime.fromtimestamp(f['hasta'], timezone.utc):%m-%d %H:%M} UTC)"
              f"  pidiendo {n} velas...", end="", flush=True)
        try:
            velas = api.get_candles(activo, tf, n, f["hasta"] + 3 * tf)
        except Exception as e:
            print(f" fallo: {e}")
            continue
        k = reg.resolver_virtuales(activo, tf, velas)
        resueltas += k
        print(f" {k} resueltas")
        time.sleep(0.4)

    print(f"\n{resueltas:,} predicciones resueltas.")
    resumen(a.db)


if __name__ == "__main__":
    main()
