import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Rellena, para las operaciones YA cerradas que no las tienen, las velas que
cubren todo su ciclo (señal + cada paso de martingala). Sin esto, el grafico
de esas operaciones corta justo en la vela de la señal y no se ven los pasos
donde realmente se perdio o gano — que es lo unico interesante.

Las operaciones nuevas ya las guardan solas (bot_weis_bollinger lo hace al
cerrar cada ciclo). Esto es solo para las viejas.

    py rellenar_velas_ciclo.py                 # todas las que falten
    py rellenar_velas_ciclo.py --run 13
    py rellenar_velas_ciclo.py --op 99
"""
import argparse
import io
import json
import re
import sqlite3
import time


def conectar():
    src = io.open("bot_indicadores.py", encoding="utf-8").read()
    email = re.search(r'"email":\s*"([^"]+)"', src).group(1)
    pwd = re.search(r'"password":\s*"([^"]+)"', src).group(1)
    from iqoptionapi.stable_api import IQ_Option
    api = IQ_Option(email, pwd)
    for intento in range(1, 4):
        try:
            api.connect()
        except Exception as e:
            print(f"  intento {intento}: {e}")
            time.sleep(5 * intento)
            continue
        for _ in range(20):
            if api.check_connect():
                break
            time.sleep(1)
        if api.check_connect():
            break
        time.sleep(5 * intento)
    if not api.check_connect():
        raise SystemExit("no se pudo conectar a IQ Option")
    api.change_balance("PRACTICE")
    import iqoptionapi.constants as OP
    init = api.get_all_init_v2() or {}
    for tipo in ("turbo", "binary"):
        for k, v in ((init.get(tipo) or {}).get("actives") or {}).items():
            nom = (v.get("name") or "").split(".")[-1]
            if nom and nom not in OP.ACTIVES:
                OP.ACTIVES[nom] = int(k)
    return api


def pasos_de(detalle):
    if not detalle:
        return 1
    try:
        p = json.loads(detalle).get("ciclo_pasos")
        return len(p) if isinstance(p, list) and p else 1
    except Exception:
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="live.db")
    ap.add_argument("--run", type=int, default=None)
    ap.add_argument("--op", type=int, default=None)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    filtro, params = "o.vela_resultado IS NULL AND o.resultado IS NOT NULL", []
    if args.op:
        filtro, params = "o.id = ?", [args.op]
    elif args.run:
        filtro += " AND o.run_id = ?"
        params = [args.run]
    filas = con.execute(f"""
        SELECT o.id, o.error_detalle, p.activo, p.ts_vela, p.timeframe
        FROM operaciones o JOIN predicciones p ON p.id = o.prediccion_id
        WHERE {filtro} ORDER BY o.id""", params).fetchall()

    if not filas:
        print("nada que rellenar")
        return
    print(f"{len(filas)} operación(es) sin velas del ciclo; conectando...")
    api = conectar()
    print("conectado\n")

    ok = fallo = 0
    for f in filas:
        n_pasos = pasos_de(f["error_detalle"])
        tf, ts = f["timeframe"], f["ts_vela"]
        try:
            crudas = api.get_candles(f["activo"], tf, 18 + n_pasos + 4,
                                     ts + (n_pasos + 3) * tf)
        except Exception as e:
            crudas = None
            print(f"  op{f['id']:<4} {f['activo']:<10} ERROR {e}")
        if not crudas:
            fallo += 1
            if crudas is None:
                continue
            print(f"  op{f['id']:<4} {f['activo']:<10} sin velas disponibles")
            continue
        velas = [[int(v["from"]), float(v["open"]), float(v["max"]),
                  float(v["min"]), float(v["close"])]
                 for v in sorted(crudas, key=lambda c: c["from"])]
        cubre = sum(1 for v in velas if v[0] > ts)
        con.execute("UPDATE operaciones SET vela_resultado=? WHERE id=?",
                    (json.dumps({"velas": velas}), f["id"]))
        con.commit()
        ok += 1
        print(f"  op{f['id']:<4} {f['activo']:<10} {len(velas)} velas "
              f"({cubre} posteriores a la señal, necesita {n_pasos})")
        time.sleep(0.35)          # no atropellar la API

    print(f"\nlistas: {ok} · sin datos: {fallo}")
    con.close()


if __name__ == "__main__":
    main()
