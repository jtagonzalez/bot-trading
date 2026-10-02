import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Descarga velas historicas de 1 minuto desde IQ Option a `velas.db`.

Solo LEE datos de mercado — no opera, no toca el balance.

Uso:
    py descargar_velas.py                 # pares reales, 30 dias
    py descargar_velas.py --dias 60
    py descargar_velas.py --activos EURUSD,GBPUSD
    py descargar_velas.py --otc           # incluye los OTC (para comparar)
"""
import argparse, io, json, re, sqlite3, sys, time
from datetime import datetime

# Pares de mercado REAL (no OTC). El broker no los genera: cotizan en el interbancario.
PARES_REALES = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF",
    "USDCAD", "NZDUSD", "EURJPY", "GBPJPY", "EURGBP",
]
PARES_OTC = [p + "-OTC" for p in PARES_REALES]

DB = "velas.db"
LOTE = 1000      # maximo que acepta la API por llamada

# Timeframes que acepta IQ Option, en segundos.
# Descargar directamente en el TF objetivo multiplica el historico disponible:
# 1000 velas son 16 horas a 1m, pero 10 dias a 15m y 20 dias a 30m.
TFS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def crear_db(path=DB):
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS velas (
            activo TEXT NOT NULL,
            timeframe INTEGER NOT NULL,
            ts INTEGER NOT NULL,          -- epoch de APERTURA de la vela
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (activo, timeframe, ts)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_velas ON velas(activo, timeframe, ts)")
    con.commit()
    return con


def conectar(email, password):
    from iqoptionapi.stable_api import IQ_Option
    api = IQ_Option(email, password)
    api.connect()
    for _ in range(30):
        if api.check_connect():
            break
        time.sleep(1)
    if not api.check_connect():
        sys.exit("No se pudo conectar a IQ Option.")
    api.change_balance("PRACTICE")   # nunca tocar la cuenta real
    print("Conectado (cuenta PRACTICE, solo lectura de velas).")
    return api


def descargar(api, con, activo, dias, tf=60):
    objetivo = int(dias * 24 * 3600 / tf)   # velas del timeframe pedido
    endtime = time.time()
    total = 0
    vacios = 0
    while total < objetivo:
        try:
            velas = api.get_candles(activo, tf, LOTE, endtime)
        except Exception as e:
            print(f"    error: {e}; reintento en 3s")
            time.sleep(3)
            vacios += 1
            if vacios > 5:
                break
            continue

        if not velas:
            vacios += 1
            if vacios > 3:
                break
            time.sleep(1)
            continue
        vacios = 0

        filas = [(activo, tf, int(v["from"]), float(v["open"]), float(v["max"]),
                  float(v["min"]), float(v["close"]), float(v.get("volume", 0) or 0))
                 for v in velas]
        con.executemany(
            "INSERT OR IGNORE INTO velas (activo,timeframe,ts,open,high,low,close,volume) "
            "VALUES (?,?,?,?,?,?,?,?)", filas)
        con.commit()
        total += len(filas)

        nuevo_end = min(v["from"] for v in velas)
        if nuevo_end >= endtime:      # sin avance -> no hay mas historico
            break
        endtime = nuevo_end
        print(f"\r    {activo}: {total:,}/{objetivo:,} velas "
              f"(hasta {datetime.fromtimestamp(endtime):%Y-%m-%d %H:%M})   ", end="", flush=True)
        time.sleep(0.35)              # cortesia con la API
    print()
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--activos", type=str, default=None,
                    help="lista separada por comas; por defecto los 10 pares reales")
    ap.add_argument("--otc", action="store_true", help="descargar tambien los -OTC")
    ap.add_argument("--operables", default=None, metavar="JSON",
                    help="descargar los activos de activos_operables.json "
                         "(los que la cuenta puede operar de verdad). "
                         "Evita descargar historico de activos no operables.")
    ap.add_argument("--tipo", default=None,
                    help="filtra por categoria al usar --operables: divisa, cripto, indice, accion/otro")
    ap.add_argument("--top", type=int, default=None,
                    help="al usar --operables, quedarse con los N de mayor payout")
    ap.add_argument("--tf", default="1m", help="timeframe(s) separados por coma: " + ",".join(TFS))
    ap.add_argument("--email", default=None)
    ap.add_argument("--password", default=None)
    args = ap.parse_args()

    if args.operables:
        d = json.loads(io.open(args.operables, encoding="utf-8").read())
        cand = d["activos"]
        if d.get("verificados"):
            ver = set(d["verificados"])
            cand = [a for a in cand if a["activo"] in ver]
        if args.tipo:
            cand = [a for a in cand if a["tipo"] == args.tipo]
        cand.sort(key=lambda a: -a.get("payout", 0))
        if args.top:
            cand = cand[:args.top]
        activos = [a["activo"] for a in cand]
        print(f"{len(activos)} activos operables desde {args.operables}")
    elif args.activos:
        activos = [a.strip() for a in args.activos.split(",") if a.strip()]
    else:
        activos = PARES_REALES + (PARES_OTC if args.otc else [])

    email, password = args.email, args.password
    if not email or not password:
        # reutiliza las credenciales del bot si no se pasan por linea de comandos
        try:
            src = io.open("bot_indicadores.py", encoding="utf-8").read()
            email = email or re.search(r'"email":\s*"([^"]+)"', src).group(1)
            password = password or re.search(r'"password":\s*"([^"]+)"', src).group(1)
        except Exception:
            sys.exit("Faltan credenciales: usa --email y --password")

    con = crear_db()
    api = conectar(email, password)

    tfs = []
    for t in args.tf.split(","):
        t = t.strip()
        if t not in TFS:
            sys.exit(f"timeframe invalido: {t}. Validos: {', '.join(TFS)}")
        tfs.append((t, TFS[t]))

    print(f"\nDescargando {args.dias} dias | TF: {', '.join(t for t, _ in tfs)} "
          f"| {len(activos)} activos\n")
    for nombre_tf, tf in tfs:
        print(f"  === {nombre_tf} ===")
        for a in activos:
            print(f"  {a}")
            n = descargar(api, con, a, args.dias, tf)
            if n == 0:
                print(f"    sin datos (activo cerrado o nombre invalido)")

    print("\nResumen en velas.db:")
    for r in con.execute("""SELECT timeframe, activo, COUNT(*) n, MIN(ts) a, MAX(ts) b
                            FROM velas GROUP BY timeframe, activo
                            ORDER BY timeframe, activo"""):
        print(f"  tf={r[0]:>5}s {r[1]:12s} {r[2]:>8,} velas  "
              f"{datetime.fromtimestamp(r[3]):%Y-%m-%d} -> {datetime.fromtimestamp(r[4]):%Y-%m-%d}")
    con.close()


if __name__ == "__main__":
    main()
