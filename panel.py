"""
PANEL EN VIVO — ve las operaciones de TODOS los bots activos, en una sola pagina.

Antes mostraba solo el run mas reciente de live.db; con varios bots corriendo
a la vez (EURUSD 15m, cripto 15m, DeMarker 1m...) eso tapaba a los demas. Ahora
agrupa por `modelo_nombre` (cada bot usa el suyo), muestra el run mas nuevo de
cada uno, y descarta los que no tienen actividad reciente (pruebas viejas
abandonadas) para no ensuciar la vista.

Proceso independiente: abre live.db en SOLO LECTURA, asi que no interfiere con
los bots mientras escriben (la base esta en modo WAL, que permite leer y
escribir a la vez).

    py panel.py              ->  http://localhost:8090
    py panel.py --puerto 9000
    py panel.py --db live.db
    py panel.py --dias-inactivo 3   # cuanto tiempo sin actividad para ocultar un run

No sustituye a analizar_live.py: aquel hace el analisis estadistico serio, este
es para mirar que esta pasando ahora mismo, de todos los bots a la vez.
"""
import argparse
import json
import sqlite3
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PAYOUT_REF = 0.86          # binary/turbo medido
BREAKEVEN = 100 / (1 + PAYOUT_REF)


# ─────────────────── precio en vivo (para operaciones abiertas) ───────────────────
# El panel abre su PROPIA conexion, perezosa y solo si hay operaciones abiertas.
# No usa la de ningun bot: asi un fallo aqui nunca afecta a las ordenes.

_api = None
_api_fallo = 0
_cache = {}
CACHE_SEG = 8


def _conectar():
    global _api, _api_fallo
    if _api is not None:
        return _api
    if time.time() < _api_fallo:
        return None
    try:
        import re as _re
        src = open("bot_indicadores.py", encoding="utf-8").read()
        email = _re.search(r'"email":\s*"([^"]+)"', src).group(1)
        pwd = _re.search(r'"password":\s*"([^"]+)"', src).group(1)
        from iqoptionapi.stable_api import IQ_Option
        api = IQ_Option(email, pwd)
        api.connect()
        for _ in range(15):
            if api.check_connect():
                break
            time.sleep(1)
        if not api.check_connect():
            raise RuntimeError("sin conexion")
        api.change_balance("PRACTICE")
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
        _api = api
        return _api
    except Exception:
        _api_fallo = time.time() + 120
        return None


def precio_actual(activo):
    ahora = time.time()
    hit = _cache.get(activo)
    if hit and ahora - hit[0] < CACHE_SEG:
        return hit[1]
    api = _conectar()
    if api is None:
        return None
    try:
        v = api.get_candles(activo, 60, 1, ahora)
        if v:
            p = float(v[-1]["close"])
            _cache[activo] = (ahora, p)
            return p
    except Exception:
        global _api
        _api = None
    return None


def marcar_abiertas(ops, tf):
    """Para cada operacion: tiempos, y si las abiertas van ganando o perdiendo."""
    ahora = time.time()
    for o in ops:
        t_ini = o["ts_vela"] + tf
        t_fin = o["ts_vela"] + 2 * tf
        o["t_entrada_epoch"] = t_ini
        o["t_expira_epoch"] = t_fin
        o["duracion_seg"] = tf
        vencida = ahora > t_fin + 60
        abierta = o.get("resultado") is None and not vencida
        o["huerfana"] = o.get("resultado") is None and vencida
        if abierta:
            o["transcurrido_seg"] = max(0, int(ahora - t_ini))
            o["restante_seg"] = max(0, int(t_fin - ahora))
            o["progreso"] = min(1.0, max(0.0, (ahora - t_ini) / tf)) if tf else 0
        else:
            o["transcurrido_seg"] = tf
            o["restante_seg"] = 0
            o["progreso"] = 1.0

        if not abierta:
            o["va"] = None
            continue
        p = precio_actual(o["activo"])
        o["precio_ahora"] = p
        if p is None or o.get("precio_entrada") is None:
            o["va"] = None
            continue
        delta = p - o["precio_entrada"]
        favor = delta if o["direccion"] == "call" else -delta
        o["delta"] = round(delta, 6)
        pip = 0.01 if "JPY" in o["activo"].upper() else 0.0001
        o["favor_pips"] = round(favor / pip, 2)
        o["va"] = "ganando" if favor > 0 else ("perdiendo" if favor < 0 else "empate")
    return ops


def _runs_activos(con, dias_inactivo):
    """Todos los runs activos en los ultimos `dias_inactivo` dias, agrupados por
    modelo_nombre. Devuelve una lista de dicts con:
      - "run":     el run mas reciente del grupo (para mostrar la config vigente)
      - "run_ids": lista de IDs de todos los runs del grupo en la ventana activa

    Esto evita que al reiniciar un bot (nuevo run_id) desaparezca la historia
    del run anterior — ambos se suman en la misma vista."""
    corte = time.time() - dias_inactivo * 86400
    filas = con.execute("""
        SELECT r.*, (SELECT MAX(p.ts_decision) FROM predicciones p WHERE p.run_id = r.id) ultima_pred
        FROM runs r
        ORDER BY r.id DESC
    """).fetchall()
    grupos = {}   # modelo_nombre -> {run, run_ids}
    for f in filas:
        d = dict(f)
        modelo = d["modelo_nombre"]
        referencia = d["ultima_pred"] or d["ts_inicio"]
        if not referencia:
            continue
        try:
            ts = datetime.fromisoformat(referencia).timestamp()
        except Exception:
            continue
        if ts < corte:
            continue
        if modelo not in grupos:
            grupos[modelo] = {"run": d, "run_ids": [d["id"]]}
        else:
            grupos[modelo]["run_ids"].append(d["id"])
    return list(grupos.values())


def _pasos_de(detalle):
    """Cuantos pasos de martingala uso un ciclo, leido de operaciones.error_detalle
    (lo escribe bot_weis_bollinger.py como JSON). Devuelve None si la operacion
    no viene de un bot con martingala — asi el panel sabe cuando NO dibujar la
    matriz de pasos."""
    if not detalle:
        return None
    try:
        pasos = json.loads(detalle).get("ciclo_pasos")
    except Exception:
        return None
    return len(pasos) if isinstance(pasos, list) and pasos else None


def _matriz_pasos(ops):
    """Matriz activo x paso-donde-se-resolvio-el-ciclo, para bots con martingala.

    Es la forma de ver de un vistazo si una moneda recupera rapido (todo
    concentrado en el paso 1-2) o si arrastra cadenas largas (pasos 4-6), que es
    donde la martingala se vuelve peligrosa."""
    filas, max_paso = {}, 0
    for o in ops:
        p = o.get("pasos")
        if p is None or o["resultado"] not in ("win", "loss", "tie"):
            continue
        max_paso = max(max_paso, p)
        fila = filas.setdefault(o["activo"], {
            "activo": o["activo"], "n": 0, "ganados": 0, "perdidos": 0,
            "pnl": 0.0, "celdas": {}})
        fila["n"] += 1
        g = o["ganancia"] or 0.0
        fila["pnl"] += g
        fila["ganados"] += 1 if g > 0 else 0
        fila["perdidos"] += 1 if g < 0 else 0
        celda = fila["celdas"].setdefault(str(p), {"g": 0, "p": 0, "n": 0})
        celda["g" if g > 0 else ("p" if g < 0 else "n")] += 1
    if not filas:
        return None
    for f in filas.values():
        f["pnl"] = round(f["pnl"], 2)
    return {"max_paso": max_paso,
            "filas": sorted(filas.values(), key=lambda f: -f["n"])}


def _estadisticas_extra(ops):
    """Metricas que no se ven en la curva pero cambian la lectura: cuanto es la
    ganancia tipica frente a la perdida tipica, la caida maxima acumulada y las
    rachas. En martingala esto importa mas que el win rate."""
    cerradas = [o for o in sorted(ops, key=lambda x: x["id"])
                if o["resultado"] in ("win", "loss", "tie") and o["ganancia"] is not None]
    if not cerradas:
        return {}
    gs = [o["ganancia"] for o in cerradas]
    wins = [g for g in gs if g > 0]
    perdidas = [g for g in gs if g < 0]
    pico = acum = dd = 0.0
    for g in gs:
        acum += g
        pico = max(pico, acum)
        dd = min(dd, acum - pico)
    mejor_racha = peor_racha = actual = signo = 0
    for o in cerradas:
        if o["ganancia"] == 0:
            continue                      # empate real: no corta la racha
        s = 1 if o["ganancia"] > 0 else -1
        actual = actual + s if s == signo else s
        signo = s
        mejor_racha, peor_racha = max(mejor_racha, actual), min(peor_racha, actual)
    return {
        "mejor": round(max(gs), 2), "peor": round(min(gs), 2),
        "ganancia_media": round(sum(wins) / len(wins), 2) if wins else None,
        "perdida_media": round(sum(perdidas) / len(perdidas), 2) if perdidas else None,
        "total_ganado": round(sum(wins), 2), "total_perdido": round(sum(perdidas), 2),
        "drawdown": round(dd, 2),
        "racha_ganadora": mejor_racha, "racha_perdedora": abs(peor_racha),
    }


def consultar_run(con, grupo):
    """Todo lo que el panel necesita para un grupo de runs del mismo modelo."""
    run = grupo["run"]
    run_ids = grupo["run_ids"]
    rid = run["id"]   # el mas reciente, para la config
    ph = ",".join("?" * len(run_ids))

    d = {"run": run}
    if len(run_ids) > 1:
        d["run"]["_runs_agrupados"] = sorted(run_ids)

    d["operaciones"] = [dict(r) for r in con.execute(f"""
        SELECT o.id, p.run_id, p.activo, p.ts_vela, p.hora_utc, p.direccion,
               p.prob, p.confianza, p.cierre_vela, p.payout_ofrecido,
               o.precio_entrada, o.latencia_ms, o.precio_salida, o.resultado,
               o.ganancia, o.monto, o.payout_real, o.ts_entrada, o.ts_salida,
               o.error_detalle
        FROM operaciones o JOIN predicciones p ON p.id = o.prediccion_id
        WHERE p.run_id IN ({ph})
        ORDER BY o.id DESC LIMIT 400""", run_ids)]
    for o in d["operaciones"]:
        o["pasos"] = _pasos_de(o.pop("error_detalle", None))

    d["predicciones"] = [dict(r) for r in con.execute(f"""
        SELECT id, run_id, activo, ts_vela, hora_utc, direccion, prob, confianza,
               umbral, operada, motivo_no_operada, cierre_vela, payout_ofrecido,
               ts_decision, cierre_resultado, resultado_virtual
        FROM predicciones WHERE run_id IN ({ph}) ORDER BY id DESC LIMIT 60""", run_ids)]

    d["umbral"] = [dict(r) for r in con.execute(
        f"SELECT * FROM v_umbral WHERE resueltas > 0 AND run_id IN ({ph}) ORDER BY grupo", run_ids)]

    tot = con.execute(f"SELECT COUNT(*) n FROM predicciones WHERE run_id IN ({ph})", run_ids).fetchone()["n"]
    d["total_predicciones"] = tot
    d["motivos"] = [dict(r) for r in con.execute(f"""
        SELECT COALESCE(motivo_no_operada,'operada') motivo, COUNT(*) n
        FROM predicciones WHERE run_id IN ({ph}) GROUP BY motivo ORDER BY n DESC""", run_ids)]

    d["por_activo"] = [dict(r) for r in con.execute(f"""
        SELECT p.activo,
               COUNT(*) n,
               SUM(CASE WHEN o.resultado='win' THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN o.resultado IN ('win','loss') THEN 1 ELSE 0 END) cerradas,
               ROUND(COALESCE(SUM(o.ganancia),0),2) pnl
        FROM operaciones o JOIN predicciones p ON p.id = o.prediccion_id
        WHERE o.resultado IN ('win','loss') AND p.run_id IN ({ph})
        GROUP BY p.activo ORDER BY n DESC""", run_ids)]
    return d


def consultar(db, dias_inactivo=3):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    grupos = _runs_activos(con, dias_inactivo)
    salida = {"bots": [consultar_run(con, g) for g in grupos]}
    con.close()
    return salida


def resumir(d):
    # Ganada/perdida se decide por el SIGNO DEL DINERO, no por la etiqueta
    # `resultado`. En martingala un ciclo puede cerrar etiquetado "tie" (el
    # ultimo paso empato) y aun asi haber perdido lo de los pasos anteriores:
    # contarlo como empate daba un win rate de 100% con saldo negativo.
    ops = [o for o in d["operaciones"]
           if o["resultado"] in ("win", "loss", "tie") and o["ganancia"] is not None]
    wins = sum(1 for o in ops if o["ganancia"] > 0)
    losses = sum(1 for o in ops if o["ganancia"] < 0)
    empates = sum(1 for o in ops if o["ganancia"] == 0)
    n = len(ops)
    pnl = sum(o["ganancia"] or 0 for o in d["operaciones"])
    abiertas = sum(1 for o in d["operaciones"] if o["resultado"] is None)
    lat = [o["latencia_ms"] for o in d["operaciones"] if o["latencia_ms"] is not None]
    pays = [o["payout_real"] for o in d["operaciones"] if o["payout_real"]]
    return {
        "cerradas": n, "wins": wins, "losses": losses, "empates": empates,
        "abiertas": abiertas,
        "wr": round(100 * wins / (wins + losses), 2) if (wins + losses) else None,
        "pnl": round(pnl, 2),
        "latencia_mediana": int(sorted(lat)[len(lat) // 2]) if lat else None,
        "payout_medio": round(100 * sum(pays) / len(pays), 1) if pays else None,
        "breakeven": round(BREAKEVEN, 2),
        "curva": _acumulado(d["operaciones"]),
        "matriz_pasos": _matriz_pasos(d["operaciones"]),
        "extra": _estadisticas_extra(d["operaciones"]),
    }


def _acumulado(ops):
    """Curva de P&L acumulado, pero cada punto lleva de que operacion salio:
    el panel dibuja un punto por operacion y muestra su ficha al pasar el cursor.
    Se excluyen las rechazadas (no movieron dinero) y las abiertas."""
    total, out = 0.0, []
    for o in sorted(ops, key=lambda x: x["id"]):
        if o["ganancia"] is None or o["resultado"] not in ("win", "loss", "tie"):
            continue
        total += o["ganancia"]
        out.append({
            "id": o["id"], "acum": round(total, 2), "g": round(o["ganancia"], 2),
            "activo": o["activo"], "dir": o["direccion"], "res": o["resultado"],
            "ts": o.get("t_entrada_epoch") or o["ts_vela"],
            "pasos": o.get("pasos"), "monto": o.get("monto"),
        })
    return out


HTML = r"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bots en vivo</title>
<style>
  :root{
    color-scheme: light;
    --surface-0:#f6f6f4; --surface-1:#fcfcfb; --borde:#e3e2dd;
    --ink-1:#0b0b0b; --ink-2:#52514e; --ink-3:#86857f;
    --serie-1:#2a78d6; --bien:#0ca30c; --mal:#d03b3b; --aviso:#fab219;
  }
  @media (prefers-color-scheme:dark){
    :root:where(:not([data-theme="light"])){
      color-scheme: dark;
      --surface-0:#111110; --surface-1:#1a1a19; --borde:#33332f;
      --ink-1:#ffffff; --ink-2:#c3c2b7; --ink-3:#8a8980;
      --serie-1:#3987e5; --bien:#0ca30c; --mal:#d03b3b; --aviso:#fab219;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;padding:16px;background:var(--surface-0);color:var(--ink-1);
    font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;}
  h1{font-size:18px;margin:0 0 2px}
  h2.bot-titulo{font-size:16px;margin:28px 0 10px;padding-top:14px;border-top:2px solid var(--borde)}
  h2.bot-titulo:first-of-type{border-top:none;padding-top:0;margin-top:12px}
  .sub{color:var(--ink-2);font-size:13px;margin-bottom:16px}
  .wrap{max-width:1180px;margin:0 auto}
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
  .tile{background:var(--surface-1);border:1px solid var(--borde);border-radius:10px;padding:12px 14px}
  .tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3)}
  .tile .v{font-size:26px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
  .tile .n{font-size:12px;color:var(--ink-2);margin-top:2px}
  .card{background:var(--surface-1);border:1px solid var(--borde);border-radius:10px;padding:14px;margin-bottom:16px}
  .card h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);margin:0 0 10px;font-weight:600}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
     color:var(--ink-3);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--borde)}
  td{padding:7px 8px;border-bottom:1px solid var(--borde);font-size:13px}
  tr:last-child td{border-bottom:none}
  .num{text-align:right}
  .win{color:var(--bien);font-weight:600}
  .loss{color:var(--mal);font-weight:600}
  .abierta{color:var(--aviso);font-weight:600}
  .prev-win{color:var(--bien);font-style:italic;border-bottom:1px dotted var(--bien)}
  .prev-loss{color:var(--mal);font-style:italic;border-bottom:1px dotted var(--mal)}
  .pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
    border:1px solid var(--borde);color:var(--ink-2)}
  .vivo{color:var(--bien)} .parado{color:var(--mal)}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media (max-width:800px){.grid2{grid-template-columns:1fr}}
  .muted{color:var(--ink-3)}
  .foot{color:var(--ink-3);font-size:12px;text-align:center;margin-top:8px}
  .totales{font-size:13px;color:var(--ink-2);margin-bottom:18px}
  .totales b{color:var(--ink-1)}
  /* ficha flotante: una sola para toda la pagina, se mueve con el cursor */
  #tt{position:fixed;z-index:50;display:none;pointer-events:none;
    background:var(--surface-1);border:1px solid var(--borde);border-radius:8px;
    padding:8px 10px;font-size:12px;line-height:1.5;color:var(--ink-1);
    box-shadow:0 8px 28px rgba(0,0,0,.3);max-width:280px}
  #tt .k{color:var(--ink-3)}
  .leyenda{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:10px;
    padding-top:10px;border-top:1px solid var(--borde);font-size:12px;color:var(--ink-2)}
  .leyenda b{color:var(--ink-1);font-variant-numeric:tabular-nums}
  .leyenda .sep{color:var(--ink-3)}
  .punto{display:inline-block;width:8px;height:8px;border-radius:99px;margin-right:4px;
    vertical-align:-1px}
  td.celda{cursor:default;transition:outline .08s}
  td.celda:hover{outline:2px solid var(--serie-1);outline-offset:-2px}
  .svgbox{width:100%;overflow:visible}
  .svgbox circle.op{cursor:pointer}
  /* filtros de periodo */
  .filtros{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
  .filtros .lbl{font-size:12px;color:var(--ink-3);margin-right:2px}
  .btn-f{padding:4px 13px;border:1px solid var(--borde);border-radius:20px;
    background:none;color:var(--ink-2);font-size:12px;cursor:pointer;font:inherit}
  .btn-f.activo{background:var(--serie-1);color:#fff;border-color:var(--serie-1);font-weight:600}
  .btn-f:hover:not(.activo){background:var(--surface-0)}
  .filtro-resumen{font-size:12px;color:var(--ink-3);margin-left:8px}
  /* una pestaña por bot: de a uno por vez, el panel ya no se satura a lo ancho */
  .tabs{display:flex;gap:4px;flex-wrap:wrap;margin:0 0 18px;
    border-bottom:1px solid var(--borde)}
  .tab{background:transparent;border:1px solid transparent;border-bottom:none;
    border-radius:9px 9px 0 0;padding:9px 14px;cursor:pointer;color:var(--ink-2);
    font:inherit;font-size:13px;display:flex;align-items:center;gap:9px;
    position:relative;top:1px;white-space:nowrap}
  .tab:hover{background:var(--surface-1);color:var(--ink-1)}
  .tab.activo{background:var(--surface-1);border-color:var(--borde);color:var(--ink-1);font-weight:600}
  .tab .tab-pnl{font-variant-numeric:tabular-nums;font-weight:600}
  .tab .tab-n{color:var(--ink-3);font-size:11px;font-weight:400}
  /* visor: la imagen de la operacion al hacer clic en su punto */
  #visor{position:fixed;inset:0;z-index:80;display:none;
    background:rgba(0,0,0,.72);backdrop-filter:blur(2px);
    align-items:center;justify-content:center;padding:24px}
  #visor.abierto{display:flex}
  #visor .marco{background:var(--surface-1);border:1px solid var(--borde);
    border-radius:12px;padding:12px;max-width:min(1560px,96vw);max-height:94vh;
    display:flex;flex-direction:column;gap:8px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
  #visor .cab{display:flex;align-items:center;justify-content:space-between;gap:16px}
  #visor .cab b{font-size:14px}
  #visor img{max-width:100%;max-height:82vh;object-fit:contain;border-radius:8px;
    background:#131722}
  #visor .cerrar{background:transparent;border:1px solid var(--borde);color:var(--ink-2);
    border-radius:8px;padding:5px 12px;cursor:pointer;font:inherit;font-size:13px}
  #visor .cerrar:hover{color:var(--ink-1);background:var(--surface-0)}
  #visor .cargando{color:var(--ink-3);padding:60px 90px;text-align:center}
  /* ── animaciones nueva operación ── */
  @keyframes ring-expand{0%{transform:scale(1);opacity:.9}100%{transform:scale(5);opacity:0}}
  @keyframes float-up   {0%{transform:translateY(0) scale(1);opacity:1}80%{opacity:1}100%{transform:translateY(-68px) scale(1.05);opacity:0}}
  @keyframes fall-down  {0%{transform:translateY(0) scale(1);opacity:1}80%{opacity:1}100%{transform:translateY(68px)  scale(.95);opacity:0}}
  @keyframes shake-card {0%,100%{transform:translateX(0)}20%{transform:translateX(-6px)}40%{transform:translateX(6px)}60%{transform:translateX(-4px)}80%{transform:translateX(4px)}}
  .anim-tag{position:fixed;font-size:18px;font-weight:700;pointer-events:none;z-index:9999;
    white-space:nowrap;text-shadow:0 1px 6px rgba(0,0,0,.45)}
  .anim-win {color:var(--bien);animation:float-up  1.9s ease-out forwards}
  .anim-loss{color:var(--mal) ;animation:fall-down 1.9s ease-out forwards}
  .shake-card{animation:shake-card .45s ease-in-out}
</style></head>
<body><div class="wrap">
  <h1>Bots en vivo</h1>
  <div class="sub" id="cab">cargando…</div>
  <div class="totales" id="totales"></div>
  <div id="tabs"></div>
  <div id="bots"></div>
  <div class="foot" id="pie"></div>
</div>
<div id="visor" onclick="if(event.target===this)cerrarVisor()">
  <div class="marco">
    <div class="cab">
      <b id="visor-tit">operación</b>
      <button class="cerrar" onclick="cerrarVisor()">cerrar ✕</button>
    </div>
    <div id="visor-cuerpo"><div class="cargando">generando el gráfico…</div></div>
  </div>
</div>
<script>
const f=(v,d=2)=>v===null||v===undefined?'—':Number(v).toFixed(d);
const hora=ts=>new Date(ts*1000).toISOString().slice(11,16);
const hms=ts=>new Date(ts*1000).toISOString().slice(11,19);
const horaCO=ts=>new Date((ts-5*3600)*1000).toISOString().slice(11,16);
const hmsCO=ts=>new Date((ts-5*3600)*1000).toISOString().slice(11,19);
function dur(s){ if(s==null) return '—';
  const m=Math.floor(s/60), r=s%60;
  return m>0? `${m}m ${String(r).padStart(2,'0')}s` : `${r}s`; }
function barra(p,cls){
  const w=Math.round(100*(p||0));
  return `<div style="height:4px;background:var(--borde);border-radius:2px;overflow:hidden;margin-top:3px">
    <div style="height:100%;width:${w}%;background:var(--${cls||'aviso'})"></div></div>`;}

function tile(k,v,n,cls){return `<div class="tile"><div class="k">${k}</div>
  <div class="v ${cls||''}">${v}</div><div class="n">${n||''}</div></div>`;}

/* ───────── ficha flotante (una sola, compartida por todos los graficos) ───────── */
let TT=null, CONGELADO=false;
function ttCaja(){
  if(!TT){ TT=document.createElement('div'); TT.id='tt'; document.body.appendChild(TT); }
  return TT;
}
function ttShow(ev,html){
  const t=ttCaja(); t.innerHTML=html; t.style.display='block';
  let x=ev.clientX+14, y=ev.clientY+14;
  if(x+t.offsetWidth > innerWidth-10)  x=ev.clientX-t.offsetWidth-14;
  if(y+t.offsetHeight> innerHeight-10) y=ev.clientY-t.offsetHeight-14;
  t.style.left=x+'px'; t.style.top=y+'px';
  CONGELADO=true;            // no repintar mientras se esta inspeccionando
}
function ttHide(){ if(TT) TT.style.display='none'; CONGELADO=false; }

/* ───────── visor de la operacion (clic en un punto de la curva) ───────── */
function verOperacion(id,titulo){
  ttHide();
  document.getElementById('visor-tit').textContent=titulo||('operación #'+id);
  const cuerpo=document.getElementById('visor-cuerpo');
  cuerpo.innerHTML='<div class="cargando">generando el gráfico de la operación…<br>'+
    '<span style="font-size:12px">se descargan las velas reales de ese momento</span></div>';
  document.getElementById('visor').classList.add('abierto');
  const img=new Image();
  img.onload =()=>{ cuerpo.innerHTML=''; cuerpo.appendChild(img); };
  img.onerror=()=>{ cuerpo.innerHTML='<div class="cargando">no se pudo generar el gráfico '+
    'de esta operación (sin velas disponibles para ese momento)</div>'; };
  img.src='/api/grafico?op='+id;
  img.alt='gráfico de la operación '+id;
}
function cerrarVisor(){ document.getElementById('visor').classList.remove('abierto'); }
addEventListener('keydown',e=>{ if(e.key==='Escape') cerrarVisor(); });

/* paso de cuadricula "redondo" (1, 2, 5, 10, 20, 50...) para el eje */
function pasoLindo(rango,objetivo){
  const bruto=rango/Math.max(1,objetivo);
  const e=Math.pow(10,Math.floor(Math.log10(bruto||1)));
  const m=bruto/e;
  return (m<=1?1:m<=2?2:m<=5?5:10)*e;
}
const COLRES={win:'var(--bien)',loss:'var(--mal)',tie:'var(--ink-3)'};
const TXTRES={win:'✓ GANA',loss:'✕ PIERDE',tie:'= EMPATE'};

/* ───────── filtros de periodo (zona Colombia = UTC-5) ───────── */
// FILTROS[modelo] = {tipo, desde, hasta}
// tipo: 'todo'|'hoy'|'semana'|'mes'|'año'|'rango'
// desde/hasta: Unix seconds (null = sin límite)
let FILTROS={};
let ULTIMA_OP={}; // modelo -> {id, g} de la última op vista

/* ── zona horaria para los filtros de periodo ── */
let TZ_SEL=localStorage.getItem('tz_modo')||'mercado'; // 'mercado' | 'local'
const TZ_MERCADO=-5*3600;   // Colombia / mercado IQ Option
let TZ_LOCAL=TZ_MERCADO;    // se actualiza al arrancar con /api/tz
let TZ_LOCAL_INFO=null;     // {offset, nombre} del servidor
function _tzOff(){ return TZ_SEL==='local'?TZ_LOCAL:TZ_MERCADO; }

// medianoche del día YYYY-MM-DD en la zona seleccionada → Unix s UTC
function _desdeCO(ymd){
  const[y,m,d]=ymd.split('-').map(Number);
  return Date.UTC(y,m-1,d)/1000 - _tzOff();
}
// fin del día YYYY-MM-DD en la zona seleccionada → Unix s UTC
function _hastaCO(ymd){
  const[y,m,d]=ymd.split('-').map(Number);
  return Date.UTC(y,m-1,d+1)/1000 - _tzOff() - 1;
}
// ts Unix → 'YYYY-MM-DD' en la zona seleccionada
function _tsYmd(ts){
  if(!ts) return '';
  return new Date((ts+_tzOff())*1000).toISOString().slice(0,10);
}
// fecha de hoy en la zona seleccionada como 'YYYY-MM-DD'
function _hoyCO(){return _tsYmd(Date.now()/1000);}

function _rangoPeriodo(tipo){
  const off=_tzOff();
  // hora actual en la zona seleccionada, "tratada como UTC" para manipular con setUTCHours/setUTCDate
  const co=new Date(Date.now()+off*1000);
  let d=new Date(co), desde,hasta;
  if(tipo==='hoy'){
    d.setUTCHours(0,0,0,0);
    desde=d.getTime()/1000-off;
    hasta=desde+86400;
  } else if(tipo==='semana'){
    const dow=d.getUTCDay(); d.setUTCDate(d.getUTCDate()-(dow===0?6:dow-1));
    d.setUTCHours(0,0,0,0);
    desde=d.getTime()/1000-off;
    hasta=desde+7*86400;
  } else if(tipo==='mes'){
    d.setUTCDate(1); d.setUTCHours(0,0,0,0);
    desde=d.getTime()/1000-off;
    const d2=new Date(co); d2.setUTCMonth(d2.getUTCMonth()+1,1); d2.setUTCHours(0,0,0,0);
    hasta=d2.getTime()/1000-off;
  } else if(tipo==='año'){
    d.setUTCMonth(0,1); d.setUTCHours(0,0,0,0);
    desde=d.getTime()/1000-off;
    const d2=new Date(co); d2.setUTCFullYear(d2.getUTCFullYear()+1,0,1); d2.setUTCHours(0,0,0,0);
    hasta=d2.getTime()/1000-off;
  } else {
    return{desde:null,hasta:null};
  }
  return{desde,hasta};
}

function filtrarOps(ops,filtro){
  if(!filtro||filtro.tipo==='todo') return ops;
  const{desde,hasta}=filtro;
  return ops.filter(o=>{
    const ts=o.ts_vela||o.ts_entrada; if(ts==null) return true;
    if(desde!=null&&ts<desde) return false;
    if(hasta!=null&&ts>hasta) return false;
    return true;
  });
}

/* ─── recompute client-side desde ops crudas ─── */
function _acumuladoJS(ops){
  let total=0,out=[];
  for(const o of [...ops].sort((a,b)=>a.id-b.id)){
    if(o.ganancia==null||!['win','loss','tie'].includes(o.resultado)) continue;
    total+=o.ganancia;
    out.push({id:o.id,acum:Math.round(total*100)/100,g:Math.round(o.ganancia*100)/100,
      activo:o.activo,dir:o.direccion,res:o.resultado,
      ts:o.ts_vela||o.ts_entrada,pasos:o.pasos,monto:o.monto});
  }
  return out;
}

function _matrizPasosJS(ops){
  let filas={},maxPaso=0;
  for(const o of ops){
    const p=o.pasos;
    if(p==null||!['win','loss','tie'].includes(o.resultado)) continue;
    maxPaso=Math.max(maxPaso,p);
    if(!filas[o.activo]) filas[o.activo]={activo:o.activo,n:0,ganados:0,perdidos:0,pnl:0,celdas:{}};
    const fila=filas[o.activo];
    fila.n++;
    const g=o.ganancia||0; fila.pnl+=g;
    if(g>0) fila.ganados++; if(g<0) fila.perdidos++;
    const sp=String(p);
    if(!fila.celdas[sp]) fila.celdas[sp]={g:0,p:0,n:0};
    const c=fila.celdas[sp];
    if(g>0) c.g++; else if(g<0) c.p++; else c.n++;
  }
  const arr=Object.values(filas);
  if(!arr.length) return null;
  for(const row of arr) row.pnl=Math.round(row.pnl*100)/100;
  return {max_paso:maxPaso,filas:arr.sort((a,b)=>b.n-a.n)};
}

function _barrasActivoJS(ops){
  const rows={};
  for(const o of ops){
    if(!['win','loss'].includes(o.resultado)) continue;
    if(!rows[o.activo]) rows[o.activo]={activo:o.activo,n:0,wins:0,cerradas:0,pnl:0};
    const r=rows[o.activo]; r.cerradas++; r.n++;
    if((o.ganancia||0)>0) r.wins++; r.pnl+=(o.ganancia||0);
  }
  return Object.values(rows).map(r=>({...r,pnl:Math.round(r.pnl*100)/100})).sort((a,b)=>b.n-a.n);
}

function _extraJS(ops){
  const cerradas=[...ops].sort((a,b)=>a.id-b.id)
    .filter(o=>['win','loss','tie'].includes(o.resultado)&&o.ganancia!=null);
  if(!cerradas.length) return {};
  const gs=cerradas.map(o=>o.ganancia);
  const wins=gs.filter(g=>g>0),perd=gs.filter(g=>g<0);
  let pico=0,acum=0,dd=0;
  for(const g of gs){acum+=g;pico=Math.max(pico,acum);dd=Math.min(dd,acum-pico);}
  let mrG=0,mrP=0,act=0,sgn=0;
  for(const o of cerradas){
    if(o.ganancia===0) continue;
    const s=o.ganancia>0?1:-1;
    act=s===sgn?act+s:s; sgn=s;
    mrG=Math.max(mrG,act); mrP=Math.min(mrP,act);
  }
  const avg=arr=>arr.length?Math.round(arr.reduce((a,b)=>a+b,0)/arr.length*100)/100:null;
  return{mejor:Math.max(...gs),peor:Math.min(...gs),
    ganancia_media:avg(wins),perdida_media:avg(perd),
    drawdown:Math.round(dd*100)/100,
    racha_ganadora:mrG,racha_perdedora:Math.abs(mrP)};
}

function _resumirJS(ops,be){
  const cerradas=ops.filter(o=>['win','loss','tie'].includes(o.resultado)&&o.ganancia!=null);
  const wins=cerradas.filter(o=>o.ganancia>0).length;
  const losses=cerradas.filter(o=>o.ganancia<0).length;
  const empates=cerradas.filter(o=>o.ganancia===0).length;
  const pnl=Math.round(ops.reduce((a,o)=>a+(o.ganancia||0),0)*100)/100;
  const abiertas=ops.filter(o=>o.resultado==null).length;
  const lat=ops.filter(o=>o.latencia_ms!=null).map(o=>o.latencia_ms).sort((a,b)=>a-b);
  const pays=ops.filter(o=>o.payout_real).map(o=>o.payout_real);
  return{cerradas:cerradas.length,wins,losses,empates,abiertas,
    wr:(wins+losses)?Math.round(100*wins/(wins+losses)*100)/100:null,
    pnl,
    latencia_mediana:lat.length?lat[Math.floor(lat.length/2)]:null,
    payout_medio:pays.length?Math.round(pays.reduce((a,b)=>a+b,0)/pays.length*1000)/10:null,
    breakeven:be,
    curva:_acumuladoJS(ops),
    matriz_pasos:_matrizPasosJS(ops),
    extra:_extraJS(ops)};
}

function irFiltro(tipo){
  const r=_rangoPeriodo(tipo);
  FILTROS[TAB]={tipo,desde:r.desde,hasta:r.hasta};
  // sincronizar inputs de fecha
  const id=TAB.replace(/\W/g,'');
  const eD=document.getElementById('ff-d-'+id), eH=document.getElementById('ff-h-'+id);
  if(eD) eD.value=r.desde?_tsYmd(r.desde):'';
  if(eH) eH.value=r.hasta?_tsYmd(r.hasta-1):'';
  if(ULTIMO) pintar(ULTIMO);
}

function irFiltroRango(modelo){
  const id=modelo.replace(/\W/g,'');
  const eD=document.getElementById('ff-d-'+id), eH=document.getElementById('ff-h-'+id);
  const vD=eD?eD.value:'', vH=eH?eH.value:'';
  FILTROS[modelo]={
    tipo:'rango',
    desde:vD?_desdeCO(vD):null,
    hasta:vH?_hastaCO(vH):null
  };
  if(ULTIMO) pintar(ULTIMO);
}

function barraFiltros(modelo,n,nFiltrado){
  const f=FILTROS[modelo]||{tipo:'todo'};
  const tipo=f.tipo||'todo';
  const id=modelo.replace(/\W/g,'');
  const QUICK=[['todo','Todo'],['hoy','Hoy'],['semana','Semana'],['mes','Mes'],['año','Año']];
  const btns=QUICK.map(([k,lbl])=>`<button class="btn-f ${tipo===k?'activo':''}"
    onclick="irFiltro('${k}')">${lbl}</button>`).join('');
  // valores actuales de los inputs en la zona seleccionada
  const vD=tipo==='rango'&&f.desde?_tsYmd(f.desde):(tipo!=='todo'&&f.desde?_tsYmd(f.desde):'');
  const vH=tipo==='rango'&&f.hasta?_tsYmd(f.hasta-1):(tipo!=='todo'&&f.hasta?_tsYmd(f.hasta-1):'');
  const infoCount=tipo!=='todo'&&n>0
    ? `<span class="filtro-resumen">${nFiltrado} de ${n} ops</span>` : '';
  // selector de zona horaria
  const utcStr=h=>{ const s=h>=0?'+':''; return `UTC${s}${h}`; };
  const locH=Math.round(TZ_LOCAL/3600), mercH=-5;
  const locLbl=TZ_LOCAL_INFO?`${TZ_LOCAL_INFO.nombre} ${utcStr(locH)}`:`local ${utcStr(locH)}`;
  const tzSelector=`<span class="lbl" style="margin-left:10px">Zona:</span>
    <button class="btn-f ${TZ_SEL==='mercado'?'activo':''}"
      title="Colombia / mercado IQ Option (UTC-5)"
      onclick="selTZ('mercado')">Mercado ${utcStr(mercH)}</button>
    <button class="btn-f ${TZ_SEL==='local'?'activo':''}"
      title="Hora local del servidor donde corre el panel"
      onclick="selTZ('local')">Local ${TZ_LOCAL_INFO?locLbl:'…'}</button>
    <span style="color:var(--borde);margin:0 8px">│</span>`;
  return `<div class="filtros">
    ${tzSelector}<span class="lbl">Periodo:</span>${btns}
    <span class="lbl" style="margin-left:10px">Del:</span>
    <input type="date" id="ff-d-${id}" value="${vD}" style="font:inherit;font-size:12px;
      padding:3px 6px;border:1px solid var(--borde);border-radius:6px;background:var(--surface-1);
      color:var(--ink-1);cursor:pointer"
      oninput="FILTROS['${modelo}']={tipo:'rango',desde:this.value?_desdeCO(this.value):null,
        hasta:FILTROS['${modelo}']?.hasta??null}; if(ULTIMO) pintar(ULTIMO);">
    <span class="lbl">Al:</span>
    <input type="date" id="ff-h-${id}" value="${vH}" style="font:inherit;font-size:12px;
      padding:3px 6px;border:1px solid var(--borde);border-radius:6px;background:var(--surface-1);
      color:var(--ink-1);cursor:pointer"
      oninput="FILTROS['${modelo}']={tipo:'rango',desde:FILTROS['${modelo}']?.desde??null,
        hasta:this.value?_hastaCO(this.value):null}; if(ULTIMO) pintar(ULTIMO);">
    <button class="btn-f" onclick="FILTROS['${modelo}']={tipo:'todo',desde:null,hasta:null};
      document.getElementById('ff-d-${id}').value='';
      document.getElementById('ff-h-${id}').value='';
      if(ULTIMO) pintar(ULTIMO);" title="Limpiar filtro">✕ Limpiar</button>
    ${infoCount}
  </div>`;
}

function curva(pts,be,extra){
  if(!pts.length) return '<div class="muted">sin operaciones cerradas todavía</div>';
  const W=1100,H=300,L=58,R=24,T=18,B=42;
  const vals=pts.map(p=>p.acum);
  let mn=Math.min(0,...vals), mx=Math.max(0,...vals);
  const colchon=((mx-mn)||2)*0.12; mn-=colchon; mx+=colchon;
  const rg=(mx-mn)||1;
  const x=i=>pts.length<2 ? L+(W-L-R)/2 : L+i*(W-L-R)/(pts.length-1);
  const y=v=>H-B-(v-mn)*(H-T-B)/rg;
  const y0=y(0);

  // cuadricula horizontal + etiquetas en dolares
  let grid='';
  const paso=pasoLindo(rg,5);
  for(let v=Math.ceil(mn/paso)*paso; v<=mx; v+=paso){
    const yy=y(v), cero=Math.abs(v)<1e-9;
    grid+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}"
      stroke="var(--borde)" stroke-width="${cero?1.4:1}" ${cero?'':'stroke-dasharray="2 4"'} opacity="${cero?1:.75}"/>
      <text x="${L-8}" y="${yy+4}" text-anchor="end" font-size="11" fill="var(--ink-3)">${v>0?'+':''}${v.toFixed(0)}</text>`;
  }
  // cuadricula vertical + horas
  const nv=Math.min(8,pts.length);
  for(let k=0;k<nv;k++){
    const i=Math.round(k*(pts.length-1)/Math.max(1,nv-1)), xx=x(i);
    grid+=`<line x1="${xx}" y1="${T}" x2="${xx}" y2="${H-B}" stroke="var(--borde)"
      stroke-width="1" stroke-dasharray="2 4" opacity=".5"/>
      <text x="${xx}" y="${H-B+16}" text-anchor="middle" font-size="10" fill="var(--ink-3)">${hora(pts[i].ts)}</text>
      <text x="${xx}" y="${H-B+29}" text-anchor="middle" font-size="9" fill="var(--ink-3)" opacity=".8">${horaCO(pts[i].ts)} CO</text>`;
  }

  const linea=pts.map((p,i)=>`${x(i)},${y(p.acum)}`).join(' ');
  const area=`${L},${y0} ${linea} ${x(pts.length-1)},${y0}`;
  const ult=pts[pts.length-1];

  // un punto por operacion, con ficha al pasar el cursor
  let puntos='';
  pts.forEach((p,i)=>{
    // color por el DINERO, no por la etiqueta: un ciclo etiquetado "tie" que
    // perdio plata en los pasos previos tiene que verse rojo
    const cx=x(i), cy=y(p.acum);
    const col = p.g>0?'var(--bien)' : p.g<0?'var(--mal)' : 'var(--ink-3)';
    const txtres = p.g>0?'✓ GANA' : p.g<0?'✕ PIERDE' : '= EMPATE';
    const tip=`<b>${p.activo}</b> · ${p.dir.toUpperCase()}`
      +`<br><span class='k'>operación</span> #${p.id}`
      +`<br><span class='k'>hora</span> ${hms(p.ts)} UTC · ${hmsCO(p.ts)} CO`
      +`<br><span style='color:${col}'>${txtres}</span> `
      +`<b>${p.g>0?'+':''}$${p.g.toFixed(2)}</b>`
      +(p.res==='tie'&&p.g<0?`<br><span class='k'>(cerró en empate, pero arrastraba pérdidas)</span>`:'')
      +(p.pasos?`<br><span class='k'>pasos de martingala</span> ${p.pasos}`:'')
      +`<br><span class='k'>acumulado tras ésta</span> <b>$${p.acum.toFixed(2)}</b>`
      +`<br><span class='k' style='font-style:italic'>clic para ver el gráfico de esta operación</span>`;
    const titulo=`${p.activo} · ${p.dir.toUpperCase()} · operación #${p.id} · ${hms(p.ts)} UTC`;
    puntos+=`<circle class="op" cx="${cx}" cy="${cy}" r="${pts.length>90?2.6:3.4}" fill="${col}"
        stroke="var(--surface-1)" stroke-width="1"/>
      <circle cx="${cx}" cy="${cy}" r="10" fill="transparent" style="cursor:pointer"
        onmousemove="ttShow(event,\`${tip}\`)" onmouseleave="ttHide()"
        onclick="verOperacion(${p.id},\`${titulo}\`)"/>`;
  });

  return `<svg class="svgbox" viewBox="0 0 ${W} ${H}" height="${H}" role="img"
      aria-label="P&L acumulado operación por operación, valor final ${ult.acum.toFixed(2)} dólares">
    <defs><linearGradient id="gr" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--serie-1)" stop-opacity=".22"/>
      <stop offset="100%" stop-color="var(--serie-1)" stop-opacity="0"/>
    </linearGradient></defs>
    ${grid}
    <polygon points="${area}" fill="url(#gr)"/>
    <polyline points="${linea}" fill="none" stroke="var(--serie-1)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>
    ${puntos}
    <circle class="last-pt" cx="${x(pts.length-1)}" cy="${y(ult.acum)}" r="5" fill="var(--serie-1)"
      stroke="var(--surface-1)" stroke-width="2"/>
    <text x="${W-R}" y="${y(ult.acum)-12}" text-anchor="end" font-size="13"
      fill="var(--ink-1)" font-weight="600">$${ult.acum.toFixed(2)}</text>
  </svg>${leyendaCurva(pts,extra)}`;
}

function leyendaCurva(pts,x){
  x=x||{};
  const pt=(c,t)=>`<span><span class="punto" style="background:${c}"></span>${t}</span>`;
  const d=(v,p)=>v==null?'—':`<b class="${p&&v>0?'win':p&&v<0?'loss':''}">${v>0&&p?'+':''}${p?'$':''}${v.toFixed? v.toFixed(2):v}</b>`;
  return `<div class="leyenda">
    ${pt('var(--bien)','ganada')} ${pt('var(--mal)','perdida')} ${pt('var(--ink-3)','empate')}
    <span class="sep">|</span>
    <span>operaciones en la curva <b>${pts.length}</b></span>
    <span>ganancia media ${d(x.ganancia_media,1)}</span>
    <span>pérdida media ${d(x.perdida_media,1)}</span>
    <span>mejor ${d(x.mejor,1)}</span>
    <span>peor ${d(x.peor,1)}</span>
    <span>caída máxima ${d(x.drawdown,1)}</span>
    <span>racha ganadora <b>${x.racha_ganadora??'—'}</b></span>
    <span>racha perdedora <b>${x.racha_perdedora??'—'}</b></span>
    <span class="sep">|</span>
    <span class="muted">pasá el cursor por cualquier punto para ver la operación</span>
  </div>`;
}

/* matriz activo x paso donde se resolvio el ciclo (solo bots con martingala) */
function matrizPasos(m){
  if(!m) return '';
  const cols=[]; for(let p=1;p<=m.max_paso;p++) cols.push(p);
  let maxCelda=1;
  for(const f of m.filas) for(const k in f.celdas) maxCelda=Math.max(maxCelda,f.celdas[k].g+f.celdas[k].p);
  let h=`<table><thead><tr><th>activo</th>
    ${cols.map(p=>`<th class="num">paso ${p}</th>`).join('')}
    <th class="num">ciclos</th><th class="num">recupera</th><th class="num">P&amp;L</th></tr></thead><tbody>`;
  for(const f of m.filas){
    h+=`<tr><td>${f.activo}</td>`;
    for(const p of cols){
      const c=f.celdas[p];
      if(!c){ h+='<td class="num muted">·</td>'; continue; }
      const nn=(c.n||0), n=c.g+c.p+nn, intensidad=Math.round(100*(0.10+0.50*n/maxCelda));
      const tip=`<b>${f.activo}</b> · se resolvió en el <b>paso ${p}</b>`
        +`<br><span class='k'>ciclos</span> ${n}`
        +`<br><span style='color:var(--bien)'>${c.g} recuperó</span>`
        +(c.p?`<br><span style='color:var(--mal)'>${c.p} cerró perdiendo</span>`:'')
        +(nn?`<br><span class='k'>${nn} quedó en cero (empate limpio)</span>`:'')
        +`<br><span class='k'>apuesta en ese paso</span> ~$${(Math.pow(2.16,p-1)).toFixed(2)} (base $1)`;
      const partes=[];
      if(c.g) partes.push(`<span class="win">${c.g}</span>`);
      if(c.p) partes.push(`<span class="loss">${c.p}</span>`);
      if(nn)  partes.push(`<span class="muted">${nn}</span>`);
      h+=`<td class="num celda" style="background:color-mix(in srgb, var(--serie-1) ${intensidad}%, transparent)"
          onmousemove="ttShow(event,\`${tip}\`)" onmouseleave="ttHide()">${partes.join('<span class="muted">/</span>')}</td>`;
    }
    const decididos=f.ganados+f.perdidos;
    const rec=decididos?100*f.ganados/decididos:null;
    h+=`<td class="num">${f.n}</td>
        <td class="num ${rec===null?'muted':rec>=90?'win':rec<70?'loss':''}">${rec===null?'—':rec.toFixed(0)+'%'}</td>
        <td class="num ${f.pnl>0?'win':f.pnl<0?'loss':''}">${(f.pnl>0?'+':'')+f.pnl.toFixed(2)}</td></tr>`;
  }
  h+='</tbody>';

  // ── totales: por columna (en que paso se resuelve) y generales ──
  const tG={}, tP={}, tN={}, tTot={};
  for(const f of m.filas) for(const p of cols){
    const c=f.celdas[p]; if(!c) continue;
    tG[p]=(tG[p]||0)+c.g; tP[p]=(tP[p]||0)+c.p; tN[p]=(tN[p]||0)+(c.n||0);
    tTot[p]=(tTot[p]||0)+c.g+c.p+(c.n||0);
  }
  const nTot=m.filas.reduce((a,f)=>a+f.n,0);
  const gTot=m.filas.reduce((a,f)=>a+f.ganados,0);
  const pTot=m.filas.reduce((a,f)=>a+f.perdidos,0);
  const pnlTot=m.filas.reduce((a,f)=>a+f.pnl,0);
  const recTot=(gTot+pTot)?100*gTot/(gTot+pTot):null;

  h+=`<tfoot><tr style="border-top:2px solid var(--borde)">
    <td><b>TOTAL</b></td>`;
  for(const p of cols){
    const n=tTot[p]||0;
    if(!n){ h+='<td class="num muted">·</td>'; continue; }
    const partes=[];
    if(tG[p]) partes.push(`<span class="win">${tG[p]}</span>`);
    if(tP[p]) partes.push(`<span class="loss">${tP[p]}</span>`);
    if(tN[p]) partes.push(`<span class="muted">${tN[p]}</span>`);
    h+=`<td class="num"><b>${partes.join('<span class="muted">/</span>')}</b></td>`;
  }
  h+=`<td class="num"><b>${nTot}</b></td>
      <td class="num ${recTot===null?'muted':recTot>=90?'win':recTot<70?'loss':''}"><b>${recTot===null?'—':recTot.toFixed(0)+'%'}</b></td>
      <td class="num ${pnlTot>0?'win':pnlTot<0?'loss':''}"><b>${(pnlTot>0?'+':'')+pnlTot.toFixed(2)}</b></td></tr>`;

  h+=`<tr><td class="muted" style="font-size:11px">reparto de los ${nTot} ciclos</td>`;
  for(const p of cols){
    const n=tTot[p]||0;
    h+=`<td class="num muted" style="font-size:11px">${n?(100*n/nTot).toFixed(0)+'%':'·'}</td>`;
  }
  h+=`<td class="num muted" style="font-size:11px">100%</td><td></td><td></td></tr></tfoot>`;

  return h+`</table><div class="leyenda">
    <span><span class="win">verde</span> = ciclos que recuperaron en ese paso ·
      <span class="loss">rojo</span> = terminaron ahí sin recuperar</span>
    <span class="sep">|</span>
    <span class="muted">cuanto más oscura la celda, más ciclos cayeron en ese paso.
      Si se corren a la derecha, la martingala está arrastrando cadenas largas (caro).</span>
  </div>`;
}

/* P&L por activo, barras divergentes */
function barrasActivo(rows,be){
  if(!rows.length) return '<div class="muted">sin datos aún</div>';
  const alto=26, W=1100, EJE=150, H=rows.length*alto+34;
  const mx=Math.max(1,...rows.map(r=>Math.abs(r.pnl)));
  const cx=EJE+(W-EJE-30)/2, semi=(W-EJE-30)/2-10;
  let h=`<svg class="svgbox" viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="P&L por activo">
    <line x1="${cx}" y1="8" x2="${cx}" y2="${H-26}" stroke="var(--borde)" stroke-width="1.4"/>`;
  rows.forEach((r,i)=>{
    const yy=10+i*alto, w=semi*Math.abs(r.pnl)/mx, pos=r.pnl>=0;
    const wr=r.cerradas?100*r.wins/r.cerradas:null;
    const col=pos?'var(--bien)':'var(--mal)';
    const tip=`<b>${r.activo}</b>`
      +`<br><span class='k'>operaciones cerradas</span> ${r.cerradas}`
      +`<br><span class='k'>aciertos</span> ${r.wins} (${wr==null?'—':wr.toFixed(1)+'%'}, breakeven ${be}%)`
      +`<br><span class='k'>P&L</span> <b style='color:${col}'>${pos?'+':''}$${r.pnl.toFixed(2)}</b>`;
    h+=`<g onmousemove="ttShow(event,\`${tip}\`)" onmouseleave="ttHide()">
      <rect x="0" y="${yy-2}" width="${W}" height="${alto-2}" fill="transparent"/>
      <text x="8" y="${yy+13}" font-size="12" fill="var(--ink-1)">${r.activo}</text>
      <text x="${EJE-14}" y="${yy+13}" text-anchor="end" font-size="11" fill="var(--ink-3)">
        ${r.cerradas} ops · ${wr==null?'—':wr.toFixed(0)+'%'}</text>
      <rect x="${pos?cx:cx-w}" y="${yy}" width="${w}" height="${alto-10}" rx="2" fill="${col}" opacity=".85"/>
      <text x="${pos?cx+w+6:cx-w-6}" y="${yy+13}" text-anchor="${pos?'start':'end'}"
        font-size="11.5" font-weight="600" fill="${col}">${pos?'+':''}$${r.pnl.toFixed(2)}</text>
    </g>`;
  });
  h+=`<text x="${cx}" y="${H-8}" text-anchor="middle" font-size="10" fill="var(--ink-3)">$0</text></svg>`;
  return h+`<div class="leyenda">
    <span class="muted">barra a la derecha = ese activo deja dinero; a la izquierda = lo quita.
      El largo es proporcional al P&L, no al número de operaciones.</span></div>`;
}

function tablaOps(ops){
  if(!ops.length) return '<div class="muted">ninguna operación todavía</div>';
  let h=`<table><thead><tr><th>entrada (UTC)</th><th>entrada (CO)</th><th>activo</th><th>dir</th>
    <th class="num">conf</th>
    <th class="num">precio ent.</th><th class="num">actual / salida</th>
    <th class="num">movimiento</th>
    <th class="num">tiempo</th>
    <th>resultado</th><th class="num">P&amp;L</th></tr></thead><tbody>`;
  for(const o of ops.slice(0,14)){
    const cerrada = o.resultado==='win'||o.resultado==='loss';
    let cls, txt, precioDer, pnl;

    if(cerrada){
      cls = o.resultado==='win'?'win':'loss';
      txt = o.resultado==='win'?'✓ GANA':'✕ PIERDE';
      precioDer = f(o.precio_salida,5);
      pnl = o.ganancia!=null?(o.ganancia>0?'+':'')+f(o.ganancia):'—';
    } else if(o.resultado==='rechazada'){
      cls='muted'; txt='⊘ rechazada'; precioDer='—'; pnl='—';
    } else if(o.resultado){
      cls='muted'; txt=o.resultado; precioDer='—'; pnl='—';
    } else if(o.huerfana){
      cls='muted'; txt='? sin resultado'; precioDer='—'; pnl='—';
    } else {
      if(o.va==='ganando'){      cls='prev-win';  txt='▲ ganando'; }
      else if(o.va==='perdiendo'){cls='prev-loss'; txt='▼ perdiendo'; }
      else if(o.va==='empate'){   cls='abierta';   txt='= empate'; }
      else {                      cls='abierta';   txt='◷ en curso'; }
      precioDer = o.precio_ahora!=null?f(o.precio_ahora,5):'—';
      if(o.va==='ganando')       pnl='~+'+f((o.monto||1)*((o.payout_ofrecido||86)/100));
      else if(o.va==='perdiendo')pnl='~-'+f(o.monto||1);
      else pnl='—';
    }
    let mov='—';
    if(!cerrada && o.favor_pips!=null){
      const sg=o.favor_pips>0?'+':'';
      mov=`<span class="${o.favor_pips>0?'prev-win':o.favor_pips<0?'prev-loss':''}">${sg}${o.favor_pips.toFixed(1)}p</span>`;
    } else if(cerrada && o.precio_salida!=null && o.precio_entrada!=null){
      const pip=o.activo.toUpperCase().includes('JPY')?0.01:0.0001;
      const d=(o.precio_salida-o.precio_entrada)*(o.direccion==='call'?1:-1)/pip;
      mov=`<span class="${cls}">${d>0?'+':''}${d.toFixed(1)}p</span>`;
    }
    let tiempo;
    if(cerrada){
      tiempo=`<span class="muted">${dur(o.duracion_seg)}</span>`;
    } else {
      const restante=o.restante_seg;
      const urgente=restante!=null&&restante<60;
      tiempo=`<div><span class="${urgente?'abierta':''}">${dur(restante)}</span>`+
             `<span class="muted" style="font-size:11px"> rest.</span>`+
             barra(o.progreso, o.va==='ganando'?'bien':o.va==='perdiendo'?'mal':'aviso')+`</div>`;
    }
    const tOp=`${o.activo} · ${o.direccion.toUpperCase()} · operación #${o.id}`;
    h+=`<tr style="cursor:pointer" title="clic para ver el gráfico de esta operación"
          onclick="verOperacion(${o.id},\`${tOp}\`)">
      <td>${o.t_entrada_epoch?hms(o.t_entrada_epoch):hora(o.ts_vela)}</td>
      <td>${o.t_entrada_epoch?hmsCO(o.t_entrada_epoch):horaCO(o.ts_vela)}</td>
      <td>${o.activo}</td>
      <td><span class="pill">${o.direccion.toUpperCase()}</span></td>
      <td class="num muted">${f(o.confianza,4)}</td>
      <td class="num">${f(o.precio_entrada,5)}</td>
      <td class="num">${precioDer}</td>
      <td class="num">${mov}</td>
      <td class="num">${tiempo}</td>
      <td class="${cls}">${txt}</td>
      <td class="num ${cls}">${pnl}</td></tr>`;
  }
  return h+'</tbody></table>';
}

function tablaPreds(ps){
  if(!ps.length) return '<div class="muted">sin evaluaciones todavía</div>';
  let h=`<table><thead><tr><th>vela (UTC)</th><th>vela (CO)</th><th>activo</th><th>dir</th>
    <th class="num">prob</th><th class="num">conf</th><th>estado</th>
    <th>habría</th></tr></thead><tbody>`;
  for(const p of ps.slice(0,14)){
    const op=p.operada===1;
    let vc='muted', vt='◷ pendiente';
    if(p.resultado_virtual==='win'){  vc=op?'win':'prev-win';   vt=op?'✓ ganó':'✓ habría ganado'; }
    else if(p.resultado_virtual==='loss'){ vc=op?'loss':'prev-loss'; vt=op?'✕ perdió':'✕ habría perdido'; }
    else if(p.resultado_virtual==='tie'){  vc='abierta'; vt='= empate'; }
    h+=`<tr><td>${hora(p.ts_vela)}</td><td>${horaCO(p.ts_vela)}</td><td>${p.activo}</td>
      <td><span class="pill">${p.direccion.toUpperCase()}</span></td>
      <td class="num">${f(p.prob,4)}</td>
      <td class="num">${f(p.confianza,5)}</td>
      <td class="${op?'win':'muted'}">${op?'OPERADA':p.motivo_no_operada}</td>
      <td class="${vc}">${vt}</td></tr>`;
  }
  return h+'</tbody></table>';
}

function tablaUmbral(us){
  if(!us||!us.length) return '<div class="muted">sin predicciones resueltas todavía</div>';
  let h=`<table><thead><tr><th>grupo</th><th class="num">n</th>
    <th class="num">resueltas</th><th class="num">acierto</th>
    <th class="num">confianza</th></tr></thead><tbody>`;
  for(const r of us){
    const sel=r.grupo==='operada';
    h+=`<tr><td class="${sel?'win':'muted'}">${r.grupo}</td>
      <td class="num">${r.n}</td><td class="num">${r.resueltas}</td>
      <td class="num">${r.wr_virtual!=null?r.wr_virtual.toFixed(2)+'%':'—'}</td>
      <td class="num">${f(r.confianza_media,5)}</td></tr>`;
  }
  return h+`</tbody></table><div class="muted" style="margin-top:8px">
    Si <b>descartada</b> acierta tanto como <b>operada</b>, el umbral no está
    seleccionando nada: solo reduce el número de operaciones.</div>`;
}

function tablaActivos(rows,be){
  if(!rows.length) return '<div class="muted">sin datos aún</div>';
  let h=`<table><thead><tr><th>activo</th><th class="num">n</th>
    <th class="num">aciertos</th><th class="num">WR</th><th class="num">P&amp;L</th></tr></thead><tbody>`;
  for(const r of rows){
    const wr=r.cerradas?100*r.wins/r.cerradas:null;
    const cls=wr===null?'':(wr>be?'win':'loss');
    h+=`<tr><td>${r.activo}</td><td class="num">${r.cerradas}</td>
      <td class="num">${r.wins}</td>
      <td class="num ${cls}">${wr===null?'—':wr.toFixed(1)+'%'}</td>
      <td class="num ${r.pnl>0?'win':r.pnl<0?'loss':''}">${(r.pnl>0?'+':'')+f(r.pnl)}</td></tr>`;
  }
  return h+'</tbody></table>';
}

function tablaMotivos(rows,total){
  if(!rows.length) return '<div class="muted">sin datos aún</div>';
  let h=`<table><thead><tr><th>motivo</th><th class="num">n</th><th class="num">%</th></tr></thead><tbody>`;
  for(const r of rows){
    h+=`<tr><td>${r.motivo}</td><td class="num">${r.n}</td>
      <td class="num muted">${(100*r.n/total).toFixed(1)}%</td></tr>`;
  }
  return h+'</tbody></table>';
}

function bloqueBot(b){
  const r=b.run||{}, be=(b.resumen||{}).breakeven||54.36;
  const modelo=r.modelo_nombre;

  // ── aplicar filtro de periodo ──
  const filtro=FILTROS[modelo]||{tipo:'todo'};
  const opsF=filtrarOps(b.operaciones||[],filtro);

  // recompute stats desde ops filtradas (client-side)
  const s=_resumirJS(opsF,be);
  const porActivoF=_barrasActivoJS(opsF);

  const wrCls=s.wr===null?'':(s.wr>be?'win':'loss');
  const saldoCls=s.pnl>0?'win':s.pnl<0?'loss':'';
  const runsLabel = r._runs_agrupados
    ? 'runs '+r._runs_agrupados.join('+')
    : 'run '+r.id;

  return `
  <h2 class="bot-titulo">${modelo||'bot'}
    <span class="muted" style="font-weight:400;font-size:13px">
      · ${runsLabel} · ${r.tipo_cuenta||'—'} · tf ${(r.timeframe||0)/60}m
      · hash <code>${r.modelo_hash||'—'}</code>
      · última evaluación hace ${b.segundos_ultima!=null?Math.round(b.segundos_ultima/60)+' min':'—'}
    </span>
  </h2>
  ${barraFiltros(modelo,(b.operaciones||[]).length,opsF.length)}
  <div class="tiles">
    ${tile('Saldo virtual',(s.pnl>0?'+':'')+'$'+f(s.pnl),'cuenta propia de este bot, desde $0',saldoCls)}
    ${tile('Operaciones cerradas',s.cerradas,
       `${s.wins} ganadas · ${s.losses} perdidas${s.empates?' · '+s.empates+' empate(s)':''} · ${s.abiertas} en curso`)}
    ${tile('Win rate',s.wr===null?'—':s.wr.toFixed(1)+'%',
       `breakeven ${be}% · cuenta por dinero ganado, no por etiqueta`,wrCls)}
    ${tile('Evaluaciones',b.total_predicciones,`${(100*s.cerradas/Math.max(1,b.total_predicciones)).toFixed(1)}% operadas`)}
    ${tile('Latencia',s.latencia_mediana!=null?(s.latencia_mediana/1000).toFixed(1)+'s':'—','mediana')}
    ${tile('Payout real',s.payout_medio!=null?s.payout_medio+'%':'—','cobrado')}
  </div>
  <div class="muted" style="font-size:11px;margin:-8px 0 14px">
    "Saldo virtual" es propio de este bot (solo sus operaciones) — no es el saldo real de la cuenta
    PRACTICE, que es uno solo y lo comparten los 3 bots.
  </div>
  <div class="card"><h2>P&amp;L acumulado · una marca por operación</h2>
    <div>${curva(s.curva,be,s.extra)}</div></div>
  ${s.matriz_pasos?`<div class="card">
    <h2>Matriz de martingala · en qué paso se resuelve cada ciclo, por moneda</h2>
    ${matrizPasos(s.matriz_pasos)}</div>`:''}
  <div class="card"><h2>P&amp;L por activo</h2>${barrasActivo(porActivoF,be)}</div>
  <div class="grid2">
    <div class="card"><h2>Operaciones</h2>${tablaOps(opsF)}
      <div class="muted" style="font-size:11px;margin-top:8px">
        <span class="prev-win">▲ ganando</span> / <span class="prev-loss">▼ perdiendo</span>
        = previsualización, aún puede cambiar ·
        <span class="win">✓ GANA</span> / <span class="loss">✕ PIERDE</span> = resultado cerrado
      </div></div>
    <div class="card"><h2>Últimas evaluaciones</h2>${tablaPreds(b.predicciones)}</div>
  </div>
  <div class="grid2">
    <div class="card"><h2>Por activo</h2>${tablaActivos(porActivoF,be)}</div>
    <div class="card"><h2>Por qué no opera</h2>${tablaMotivos(b.motivos,b.total_predicciones)}</div>
  </div>
  <div class="card"><h2>¿Sirve el umbral de confianza?</h2>${tablaUmbral(b.umbral)}</div>`;
}

/* ───────── pestañas: un bot a la vez ───────── */
let TAB=null, ULTIMO=null;

function barraTabs(bots){
  return '<div class="tabs">'+bots.map(b=>{
    const r=b.run||{}, be=(b.resumen||{}).breakeven||54.36;
    const modelo=r.modelo_nombre;
    const viva=b.segundos_ultima!=null && b.segundos_ultima<2400;
    // mostrar stats del periodo seleccionado en la pestaña
    const filtro=FILTROS[modelo]||{tipo:'todo'};
    const opsF=filtrarOps(b.operaciones||[],filtro);
    const sf=_resumirJS(opsF,be);
    const cls=sf.pnl>0?'win':sf.pnl<0?'loss':'muted';
    const ftipo=filtro.tipo||'todo';
    const filtLabel=ftipo!=='todo'?` <span style="font-size:10px;opacity:.7">[${ftipo}]</span>`:'';
    return `<button class="tab ${TAB===modelo?'activo':''}"
      onclick="irTab('${modelo}')" title="run ${r.id} · ${r.tipo_cuenta||''}">
      <span class="${viva?'vivo':'parado'}">●</span>
      <span>${modelo}${filtLabel}</span>
      <span class="tab-pnl ${cls}">${sf.pnl>0?'+':''}$${sf.pnl.toFixed(2)}</span>
      <span class="tab-n">${sf.cerradas} ops</span>
    </button>`;
  }).join('')+'</div>';
}
function irTab(n){ TAB=n; ttHide(); if(ULTIMO) pintar(ULTIMO); }

function _animarNuevaOp(b){
  if(!b) return;
  const modelo=(b.run||{}).modelo_nombre;
  const filtro=FILTROS[modelo]||{tipo:'todo'};
  const curva=_acumuladoJS(filtrarOps(b.operaciones||[],filtro));
  const prev=ULTIMA_OP[modelo];
  if(!curva.length){ ULTIMA_OP[modelo]=null; return; }
  const ult=curva[curva.length-1];
  ULTIMA_OP[modelo]={id:ult.id,g:ult.g};
  if(!prev||prev.id===ult.id) return;   // primera carga o sin cambio

  const esWin=ult.g>0;
  const sg=ult.g>0?'+':'';
  const lbl=`${esWin?'✓':'✕'} ${sg}$${Math.abs(ult.g).toFixed(2)}`;
  const color=esWin?'var(--bien)':'var(--mal)';

  const botsDiv=document.getElementById('bots');
  if(!botsDiv) return;
  const svg=botsDiv.querySelector('svg.svgbox');
  if(!svg) return;

  // coordenadas del último punto (circle.last-pt)
  const lp=svg.querySelector('.last-pt');
  const cx=lp?parseFloat(lp.getAttribute('cx')):null;
  const cy=lp?parseFloat(lp.getAttribute('cy')):null;

  // 3 anillos concéntricos que se expanden desde el último punto
  if(cx!==null&&cy!==null){
    for(let i=0;i<3;i++){
      const c=document.createElementNS('http://www.w3.org/2000/svg','circle');
      c.setAttribute('cx',cx); c.setAttribute('cy',cy);
      c.setAttribute('r','7'); c.setAttribute('fill','none');
      c.setAttribute('stroke',color); c.setAttribute('stroke-width','2');
      c.style.transformBox='fill-box'; c.style.transformOrigin='center';
      c.style.animation=`ring-expand ${0.7+i*.22}s ${i*.18}s ease-out forwards`;
      svg.appendChild(c);
      setTimeout(()=>c.remove(), 1600+i*250);
    }
  }

  // etiqueta flotante encima del punto (+$X.XX sube / -$X.XX baja)
  const rect=svg.getBoundingClientRect();
  const scX=rect.width/(svg.viewBox.baseVal.width||1100);
  const scY=rect.height/(svg.viewBox.baseVal.height||300);
  const px=(cx!=null?cx:550)*scX, py=(cy!=null?cy:150)*scY;
  const tag=document.createElement('div');
  tag.className=`anim-tag ${esWin?'anim-win':'anim-loss'}`;
  tag.textContent=lbl;
  tag.style.left=(rect.left+px-36)+'px';
  tag.style.top =(rect.top+py-10)+'px';
  document.body.appendChild(tag);
  setTimeout(()=>tag.remove(), 2300);

  // en pérdidas: el primer card del panel tiembla levemente
  if(!esWin){
    const card=botsDiv.querySelector('.card');
    if(card){
      card.classList.add('shake-card');
      card.addEventListener('animationend',()=>card.classList.remove('shake-card'),{once:true});
    }
  }
}

function pintar(d){
  const nombres=d.bots.map(b=>(b.run||{}).modelo_nombre);
  if(TAB===null || !nombres.includes(TAB)) TAB=nombres[0];
  const bot=d.bots.find(b=>(b.run||{}).modelo_nombre===TAB);

  const algunaViva=d.bots.some(b=>b.segundos_ultima!=null && b.segundos_ultima<2400);
  document.getElementById('cab').innerHTML=
    `<span class="${algunaViva?'vivo':'parado'}">●</span> ${d.bots.length} bot(s) con actividad reciente
     · mostrando <b>${TAB}</b>`;

  let totCerradas=0, totPnl=0, totOps=0;
  for(const b of d.bots){
    const be=(b.resumen||{}).breakeven||54.36;
    const modelo=(b.run||{}).modelo_nombre;
    const filt=FILTROS[modelo]||{tipo:'todo'};
    const sf=_resumirJS(filtrarOps(b.operaciones||[],filt),be);
    totCerradas+=sf.cerradas; totPnl+=sf.pnl; totOps+=sf.abiertas;
  }
  document.getElementById('totales').innerHTML=
    `<b>${totCerradas}</b> operaciones cerradas sumando los ${d.bots.length} bots · P&amp;L combinado
     <b class="${totPnl>0?'win':totPnl<0?'loss':''}">${(totPnl>0?'+':'')}$${totPnl.toFixed(2)}</b> ·
     <b>${totOps}</b> en curso ahora mismo`;

  document.getElementById('tabs').innerHTML = barraTabs(d.bots);
  document.getElementById('bots').innerHTML = bot?bloqueBot(bot):'';
  _animarNuevaOp(bot);
  document.getElementById('pie').textContent=
    'actualizado '+new Date().toLocaleTimeString()+' · se refresca cada 5 s';
}

async function tick(){
  // si el cursor esta sobre un punto/celda, no repintar: el refresco cada 5s
  // borraria la ficha justo cuando la estas leyendo
  if(CONGELADO) return;
  let d; try{ d=await (await fetch('/api/estado')).json(); }
  catch(e){ document.getElementById('cab').innerHTML='<span class="parado">sin conexión con el panel</span>'; return; }

  if(d.error){ document.getElementById('cab').innerHTML=`<span class="parado">error: ${d.error}</span>`; return; }
  if(!d.bots.length){ document.getElementById('cab').innerHTML='<span class="parado">ningún bot con actividad reciente</span>'; return; }

  ULTIMO=d;
  pintar(d);
}
function selTZ(modo){
  TZ_SEL=modo;
  localStorage.setItem('tz_modo',modo);
  if(ULTIMO) pintar(ULTIMO);
}

async function cargarTZ(){
  try{
    const r=await fetch('/api/tz');
    const d=await r.json();
    TZ_LOCAL_INFO=d;
    TZ_LOCAL=d.offset||0;
  }catch(e){}
}

cargarTZ().then(()=>{ if(ULTIMO) pintar(ULTIMO); });
tick(); setInterval(tick,5000);
</script></body></html>"""


def crear_handler(db, dias_inactivo):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, tipo):
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            ruta = urlparse(self.path).path
            if ruta == "/api/estado":
                try:
                    data = consultar(db, dias_inactivo)
                    ahora = datetime.now(timezone.utc)
                    for b in data["bots"]:
                        marcar_abiertas(b["operaciones"], (b["run"] or {}).get("timeframe") or 60)
                        b["resumen"] = resumir(b)
                        ult = max((p["ts_decision"] for p in b["predicciones"]), default=None)
                        b["segundos_ultima"] = (
                            (ahora - datetime.fromisoformat(ult)).total_seconds() if ult else None)
                    self._send(json.dumps(data, default=str).encode(), "application/json")
                except Exception as e:
                    self._send(json.dumps({"error": str(e)}).encode(), "application/json")
            elif ruta == "/api/grafico":
                # imagen de UNA operacion, generada al vuelo cuando haces clic
                # en su punto de la curva. Se cachea en disco, asi que el
                # segundo clic sobre la misma operacion es instantaneo.
                try:
                    op_id = int(parse_qs(urlparse(self.path).query).get("op", [""])[0])
                except (TypeError, ValueError):
                    self.send_response(400); self.end_headers(); return
                try:
                    import grafico_operacion
                    png = grafico_operacion.png_operacion(db, op_id, api=_conectar())
                except Exception as e:
                    png = None
                    print(f"grafico op{op_id}: {e}")
                if png:
                    self._send(png, "image/png")
                else:
                    self.send_response(404); self.end_headers()
            elif ruta == "/api/tz":
                local_now = datetime.now().astimezone()
                off = int(local_now.utcoffset().total_seconds())
                nombre = local_now.tzname() or "local"
                self._send(json.dumps({"offset": off, "nombre": nombre}).encode(),
                           "application/json")
            elif ruta in ("/", "/index.html"):
                self._send(HTML.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self.send_response(404)
                self.end_headers()
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="live.db")
    ap.add_argument("--puerto", type=int, default=8090)
    ap.add_argument("--dias-inactivo", type=float, default=3,
                    help="ocultar runs sin ninguna evaluación en los últimos N días")
    ap.add_argument("--no-abrir", action="store_true")
    a = ap.parse_args()

    try:
        consultar(a.db, a.dias_inactivo)
    except Exception as e:
        raise SystemExit(f"no se pudo leer {a.db}: {e}\n"
                         f"Crea el esquema con: py registro.py --crear")

    url = f"http://localhost:{a.puerto}"
    srv = ThreadingHTTPServer(("", a.puerto), crear_handler(a.db, a.dias_inactivo))
    print(f"Panel en vivo (todos los bots): {url}")
    print("Ctrl+C para cerrarlo. Ningún bot se ve afectado (lectura sola).")
    if not a.no_abrir:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\npanel cerrado")


if __name__ == "__main__":
    main()
