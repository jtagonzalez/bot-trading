import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
DESCUBRIR ACTIVOS — que puede operar REALMENTE esta cuenta, y con que payout.

Esto hay que correrlo ANTES de descargar histórico o entrenar nada. Descargar
datos de activos que la cuenta no puede operar es trabajo perdido (aprendido a
las malas: se descargaron 614.000 velas de forex real que resulto no ser
operable en esta cuenta).

Usa la misma secuencia que el bot original, que si funciona:
    update_ACTIVES_OPCODE()  ->  esperar  ->  get_all_open_time()

Guarda el resultado en activos_operables.json para que el resto de scripts lo usen.

Uso:
    py descubrir_activos.py
    py descubrir_activos.py --verificar   # ademas prueba una orden de $1 en PRACTICE
"""
import argparse, io, json, re, sys, time
from datetime import datetime, timezone


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
        sys.exit("no se pudo conectar")
    api.change_balance("PRACTICE")
    return api


def escanear(api, espera=4):
    """Secuencia del bot original: sin update_ACTIVES_OPCODE los datos vienen vacios."""
    try:
        api.update_ACTIVES_OPCODE()
    except Exception as e:
        print(f"  aviso: update_ACTIVES_OPCODE fallo ({str(e)[:40]})")
    time.sleep(espera)
    abiertos = api.get_all_open_time() or {}
    try:
        payouts = api.get_all_profit() or {}
    except Exception:
        payouts = {}
    return abiertos, payouts


def clasificar(nombre):
    n = nombre.upper()
    if n.endswith("-OTC"):
        base = n[:-4]
    elif n.endswith("-OP"):
        base = n[:-3]
    else:
        base = n
    divisas = ("EUR", "USD", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD")
    if len(base) == 6 and base[:3] in divisas and base[3:] in divisas:
        return "divisa"
    if base.endswith("USD") and len(base) > 6:
        return "cripto"
    if base in ("AUS200", "SP500", "NASDAQ", "DAX30", "FTSE100", "NIKKEI", "US30"):
        return "indice"
    return "accion/otro"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--salida", default="activos_operables.json")
    ap.add_argument("--payout-minimo", type=float, default=70.0)
    ap.add_argument("--verificar", action="store_true",
                    help="prueba una orden de $1 en PRACTICE por activo (lento)")
    ap.add_argument("--max-verificar", type=int, default=15)
    args = ap.parse_args()

    api = conectar()
    ahora = datetime.now(timezone.utc)
    print(f"{ahora:%Y-%m-%d %H:%M} UTC  |  PRACTICE  |  balance ${api.get_balance():,.2f}\n")

    print("escaneando (update_ACTIVES_OPCODE + get_all_open_time)...")
    abiertos, payouts = escanear(api)
    binarios = abiertos.get("binary", {}) or {}
    turbos = abiertos.get("turbo", {}) or {}
    print(f"  binary: {len(binarios)} activos   turbo: {len(turbos)} activos")
    if not binarios and not turbos:
        sys.exit("  la API no devolvio activos; reintenta en unos minutos")

    filas = []
    for nombre in sorted(set(binarios) | set(turbos)):
        ab_b = binarios.get(nombre, {}).get("open", False)
        ab_t = turbos.get(nombre, {}).get("open", False)
        if not (ab_b or ab_t):
            continue
        p = payouts.get(nombre, {})
        pb = round(100 * (p.get("binary", 0) or 0), 1) if isinstance(p, dict) else 0.0
        pt = round(100 * (p.get("turbo", 0) or 0), 1) if isinstance(p, dict) else 0.0
        filas.append({"activo": nombre, "tipo": clasificar(nombre),
                      "otc": nombre.upper().endswith("-OTC"),
                      "binary_abierto": ab_b, "turbo_abierto": ab_t,
                      "payout_binary": pb, "payout_turbo": pt,
                      "payout": max(pb, pt)})

    print(f"\nabiertos ahora mismo: {len(filas)}")
    por_tipo = {}
    for f in filas:
        k = (f["tipo"], "OTC" if f["otc"] else "real")
        por_tipo[k] = por_tipo.get(k, 0) + 1
    print(f"\n  {'categoria':<16}{'mercado':>9}{'n':>7}")
    print("  " + "-" * 32)
    for (t, m), n in sorted(por_tipo.items()):
        print(f"  {t:<16}{m:>9}{n:>7}")

    con_payout = [f for f in filas if f["payout"] >= args.payout_minimo]
    print(f"\ncon payout >= {args.payout_minimo:.0f}%: {len(con_payout)}")
    if con_payout:
        top = sorted(con_payout, key=lambda x: -x["payout"])[:20]
        print(f"\n  {'activo':<20}{'tipo':<14}{'payout':>8}")
        print("  " + "-" * 42)
        for f in top:
            print(f"  {f['activo']:<20}{f['tipo']:<14}{f['payout']:>7.1f}%")
    else:
        print("  (la API no publico payouts; usa --verificar para probar con ordenes)")

    verificados = None
    if args.verificar:
        print(f"\nverificando con ordenes de $1 en PRACTICE (max {args.max_verificar})...")
        cand = (con_payout or filas)[:args.max_verificar]
        verificados = []
        for f in cand:
            try:
                ok, info = api.buy(1.0, f["activo"], "call", 1)
            except Exception as e:
                ok, info = False, str(e)[:40]
            f["operable"] = bool(ok)
            if ok:
                verificados.append(f["activo"])
            print(f"  {f['activo']:<20}{'OPERABLE' if ok else 'no'}  {'' if ok else str(info)[:40]}")
            time.sleep(0.6)
        print(f"\n  operables confirmados: {len(verificados)}/{len(cand)}")

    salida = {"timestamp": ahora.isoformat(), "total_abiertos": len(filas),
              "payout_minimo": args.payout_minimo, "activos": filas,
              "verificados": verificados}
    io.open(args.salida, "w", encoding="utf-8").write(
        json.dumps(salida, indent=2, ensure_ascii=False))
    print(f"\nguardado en {args.salida}")
    print("Ahora si: descargar historico SOLO de los operables.")


if __name__ == "__main__":
    main()
