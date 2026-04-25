"""
╔══════════════════════════════════════════════════════════════════╗
║          ESTRATEGIA GOLD 4.0 v3 — Bot Completo IQ Option        ║
║                                                                  ║
║  Features:                                                       ║
║  • Filtro de payout (solo opera si ganancia > 80%)               ║
║  • Martingala x2.4, máximo 6 entradas                            ║
║  • Telemetría completa (JSON log)                                ║
║  • Selección automática del mejor activo                         ║
║  • Estadísticas en tiempo real                                   ║
║  • Servidor web para dashboard visual                            ║
║                                                                  ║
║  ⚠️  Prueba SIEMPRE en cuenta DEMO.                              ║
╚══════════════════════════════════════════════════════════════════╝
"""

import sys
import time
import json
import os
import sqlite3
import threading

import signal

# Forzar UTF-8 en Windows para soportar emojis y caracteres especiales
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Logging a archivo ──────────────────────────────────────
class _Tee:
    """Escribe simultáneamente en consola y en archivo de log."""
    def __init__(self, stream, filepath):
        self._stream = stream
        self._file   = open(filepath, "a", encoding="utf-8", errors="replace", buffering=1)
    def write(self, data):
        self._stream.write(data)
        self._file.write(data)
    def flush(self):
        self._stream.flush()
        self._file.flush()
    def __getattr__(self, attr):
        return getattr(self._stream, attr)

_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_log.txt")
sys.stdout = _Tee(sys.stdout, _log_path)
sys.stderr = _Tee(sys.stderr, _log_path)
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
import socketserver
from urllib.parse import urlparse, parse_qs

# pip install numpy pandas
# pip install -U git+https://github.com/iqoptionapi/iqoptionapi.git
from iqoptionapi.stable_api import IQ_Option


# ═══════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════

CONFIG = {
    # --- Credenciales ---
    "email": "joseneki94@gmail.com",
    "password": "8e9e0ddcbf02",

    # --- Operación ---
    "tipo_cuenta": "PRACTICE",        # "PRACTICE" o "REAL"
    "monto_base": 1,                  # Monto inicial (USD)
    "expiracion": 1,                  # Minutos
    "timeframe": 60,                  # Segundos (60 = 1 min)
    "num_velas": 200,

    # --- Martingala ---
    "martingala_activa": True,
    "multiplicador": 2.4,             # Multiplicador tras pérdida
    "max_pasos": 4,                   # Máx entradas: 1 original + 3 martingala

    # --- Indicadores ---
    "sma_rapida": 3,
    "sma_tendencia": 50,

    # --- Filtros de la estrategia ---
    "porcentaje_cuerpo": 0.60,          # % mínimo del cuerpo sobre/bajo SMA3
    "max_mecha_ratio": 1.0,             # mechas máx como múltiplo del cuerpo
    "velas_inclinacion": 3,
    "separacion_minima": 0.0000,

    # --- Telemetría ---
    "log_file": "telemetria.json",
    "stats_file": "estadisticas.json",
    "db_file": "gold_bot.db",
    "dashboard_port": 8080,

    # --- Gestión de riesgo ---
    "max_perdidas_diarias": 9999,      # Sin límite (cuenta PRACTICE)
    "max_operaciones_dia": 9999,      # Sin límite (cuenta PRACTICE)
    "ganancia_objetivo_dia": 0,       # Sin límite (0 = desactivado)

    # --- Observadores ---
    "rescan_intervalo": 15,           # Minutos entre rescaneos de payout
    "payout_minimo": 80,              # % mínimo para mantener observador activo
}

# Sin exclusiones — corrida limpia para comparar con run anterior (run_id=0)
# Los resultados quedan separados por run_id en la DB
ACTIVOS_EXCLUIDOS: set = set()


# ═══════════════════════════════════════════════
# INDICADORES TÉCNICOS
# ═══════════════════════════════════════════════

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


# ═══════════════════════════════════════════════
# SEÑALES DE LA ESTRATEGIA
# ═══════════════════════════════════════════════

def calcular_señales(df: pd.DataFrame) -> pd.DataFrame:
    df["sma_rapida"] = sma(df["close"], CONFIG["sma_rapida"])
    df["sma_tendencia"] = sma(df["close"], CONFIG["sma_tendencia"])

    n = CONFIG["velas_inclinacion"]
    df["tendencia_alcista"] = df["sma_tendencia"] > df["sma_tendencia"].shift(n)
    df["tendencia_bajista"] = df["sma_tendencia"] < df["sma_tendencia"].shift(n)

    df["distancia_smas"] = abs(df["sma_rapida"] - df["sma_tendencia"])
    df["verde_encima_cian"] = df["sma_rapida"] > df["sma_tendencia"]
    df["verde_debajo_cian"] = df["sma_rapida"] < df["sma_tendencia"]
    df["lineas_separadas"] = df["distancia_smas"] > CONFIG["separacion_minima"]

    df["cuerpo"] = abs(df["close"] - df["open"])
    df["es_alcista"] = df["close"] > df["open"]
    df["es_bajista"] = df["close"] < df["open"]

    df["mecha_superior"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["mecha_inferior"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["suma_mechas"] = df["mecha_superior"] + df["mecha_inferior"]
    df["mechas_ok"] = (df["cuerpo"] > 0) & (df["suma_mechas"] < df["cuerpo"] * CONFIG["max_mecha_ratio"])

    def pct_encima(row):
        if row["cuerpo"] == 0 or pd.isna(row["sma_rapida"]):
            return 0.0
        low_b, high_b = row["open"], row["close"]
        s = row["sma_rapida"]
        if s <= low_b: return 1.0
        if s >= high_b: return 0.0
        return (high_b - s) / row["cuerpo"]

    def pct_debajo(row):
        if row["cuerpo"] == 0 or pd.isna(row["sma_rapida"]):
            return 0.0
        low_b, high_b = row["close"], row["open"]
        s = row["sma_rapida"]
        if s >= high_b: return 1.0
        if s <= low_b: return 0.0
        return (s - low_b) / row["cuerpo"]

    df["pct_encima_sma3"] = df.apply(pct_encima, axis=1)
    df["pct_debajo_sma3"] = df.apply(pct_debajo, axis=1)

    umbral = CONFIG["porcentaje_cuerpo"]

    # Sin cruce reciente de tendencia: en las últimas 15 velas ninguna debe tocar SMA50
    # "tocar" = el rango (low..high) de la vela contiene el valor de SMA50
    def sin_cruce_sma50(i):
        if i < 1:
            return False
        ventana = df.iloc[max(0, i-15):i]
        tocando = (ventana["low"] <= ventana["sma_tendencia"]) & (ventana["sma_tendencia"] <= ventana["high"])
        return not tocando.any()

    df["sin_cruce_reciente"] = pd.Series([sin_cruce_sma50(i) for i in range(len(df))], index=df.index)

    # Pullback: en las 5 velas anteriores debe haber al menos 1 vela contraria
    # CALL → al menos 1 roja (bajista) entre las 5 anteriores a la vela señal
    # PUT  → al menos 1 verde (alcista) entre las 5 anteriores a la vela señal
    df["pullback_call"] = pd.Series([
        df["es_bajista"].iloc[max(0, i-5):i].any() if i >= 1 else False
        for i in range(len(df))
    ], index=df.index)
    df["pullback_put"] = pd.Series([
        df["es_alcista"].iloc[max(0, i-5):i].any() if i >= 1 else False
        for i in range(len(df))
    ], index=df.index)

    # Sin vela dominante contraria: en las últimas 5 velas no debe haber
    # CALL → ninguna vela roja con >50% del cuerpo por debajo de SMA3
    # PUT  → ninguna vela verde con >50% del cuerpo por encima de SMA3
    def sin_roja_dominante(i):
        if i < 1:
            return False
        ventana = df.iloc[max(0, i-5):i]
        return not ((ventana["es_bajista"]) & (ventana["pct_debajo_sma3"] > 0.5)).any()

    def sin_verde_dominante(i):
        if i < 1:
            return False
        ventana = df.iloc[max(0, i-5):i]
        return not ((ventana["es_alcista"]) & (ventana["pct_encima_sma3"] > 0.5)).any()

    df["sin_roja_dominante"] = pd.Series([sin_roja_dominante(i) for i in range(len(df))], index=df.index)
    df["sin_verde_dominante"] = pd.Series([sin_verde_dominante(i) for i in range(len(df))], index=df.index)

    df["COMPRAR"] = (
        df["tendencia_alcista"] & df["verde_encima_cian"] &
        df["lineas_separadas"] & df["es_alcista"] &
        (df["pct_encima_sma3"] >= umbral) & df["mechas_ok"] &
        df["pullback_call"] & df["sin_cruce_reciente"] & df["sin_roja_dominante"]
    )
    df["VENDER"] = (
        df["tendencia_bajista"] & df["verde_debajo_cian"] &
        df["lineas_separadas"] & df["es_bajista"] &
        (df["pct_debajo_sma3"] >= umbral) & df["mechas_ok"] &
        df["pullback_put"] & df["sin_cruce_reciente"] & df["sin_verde_dominante"]
    )

    return df


# ═══════════════════════════════════════════════
# TELEMETRÍA — Registro de operaciones
# ═══════════════════════════════════════════════

class Telemetria:
    def __init__(self):
        self.operaciones = []
        self.sesion_inicio = datetime.now().isoformat()
        self._lock = threading.Lock()
        self.cargar()

    def cargar(self):
        """Carga historial previo si existe"""
        if os.path.exists(CONFIG["log_file"]):
            try:
                with open(CONFIG["log_file"], "r") as f:
                    self.operaciones = json.load(f)
            except:
                self.operaciones = []

    def guardar(self):
        with self._lock:
            with open(CONFIG["log_file"], "w") as f:
                json.dump(self.operaciones, f, indent=2, default=str)

    def registrar(self, operacion: dict):
        with self._lock:
            op = {
                "id": len(self.operaciones) + 1,
                "timestamp": datetime.now().isoformat(),
                "activo": operacion.get("activo", ""),
                "direccion": operacion.get("direccion", ""),
                "monto": operacion.get("monto", 0),
                "payout": operacion.get("payout", 0),
                "resultado": operacion.get("resultado", ""),
                "ganancia": operacion.get("ganancia", 0),
                "paso_martingala": operacion.get("paso", 1),
                "balance_despues": operacion.get("balance", 0),
                "sma3": operacion.get("sma3", 0),
                "sma50": operacion.get("sma50", 0),
                "pct_cuerpo": operacion.get("pct_cuerpo", 0),
                "ratio_mechas": operacion.get("ratio_mechas", 0),
                "close": operacion.get("close", 0),
            }
            self.operaciones.append(op)
            with open(CONFIG["log_file"], "w") as f:
                json.dump(self.operaciones, f, indent=2, default=str)
        self.actualizar_estadisticas()
        return op

    def stats_hoy(self) -> dict:
        """Estadísticas del día actual"""
        hoy = datetime.now().date().isoformat()
        ops_hoy = [o for o in self.operaciones if o["timestamp"][:10] == hoy]

        wins = [o for o in ops_hoy if o["resultado"] == "win"]
        losses = [o for o in ops_hoy if o["resultado"] == "loss"]
        ties = [o for o in ops_hoy if o["resultado"] == "tie"]

        total = len(ops_hoy)
        ganancia_total = sum(o["ganancia"] for o in ops_hoy)
        racha_actual = 0
        racha_max_win = 0
        racha_max_loss = 0
        temp_racha = 0

        for o in ops_hoy:
            if o["resultado"] == "win":
                if temp_racha >= 0:
                    temp_racha += 1
                else:
                    temp_racha = 1
                racha_max_win = max(racha_max_win, temp_racha)
            elif o["resultado"] == "loss":
                if temp_racha <= 0:
                    temp_racha -= 1
                else:
                    temp_racha = -1
                racha_max_loss = max(racha_max_loss, abs(temp_racha))

        racha_actual = temp_racha

        # Análisis por activo
        por_activo = {}
        for o in ops_hoy:
            act = o["activo"]
            if act not in por_activo:
                por_activo[act] = {"wins": 0, "losses": 0, "ganancia": 0}
            if o["resultado"] == "win":
                por_activo[act]["wins"] += 1
            elif o["resultado"] == "loss":
                por_activo[act]["losses"] += 1
            por_activo[act]["ganancia"] += o["ganancia"]

        # Análisis por paso de martingala
        por_paso = {}
        for o in ops_hoy:
            paso = o["paso_martingala"]
            if paso not in por_paso:
                por_paso[paso] = {"wins": 0, "losses": 0, "total": 0}
            por_paso[paso]["total"] += 1
            if o["resultado"] == "win":
                por_paso[paso]["wins"] += 1
            elif o["resultado"] == "loss":
                por_paso[paso]["losses"] += 1

        return {
            "fecha": hoy,
            "total_operaciones": total,
            "wins": len(wins),
            "losses": len(losses),
            "ties": len(ties),
            "winrate": (len(wins) / total * 100) if total > 0 else 0,
            "ganancia_total": round(ganancia_total, 2),
            "racha_actual": racha_actual,
            "racha_max_win": racha_max_win,
            "racha_max_loss": racha_max_loss,
            "por_activo": por_activo,
            "por_paso_martingala": por_paso,
            "operaciones": ops_hoy[-20:]  # Últimas 20
        }

    def stats_global(self) -> dict:
        """Estadísticas globales de todo el historial"""
        total = len(self.operaciones)
        if total == 0:
            return {"total": 0, "mensaje": "Sin operaciones registradas"}

        wins = sum(1 for o in self.operaciones if o["resultado"] == "win")
        losses = sum(1 for o in self.operaciones if o["resultado"] == "loss")
        ganancia = sum(o["ganancia"] for o in self.operaciones)

        # Mejor y peor día
        por_dia = {}
        for o in self.operaciones:
            dia = o["timestamp"][:10]
            if dia not in por_dia:
                por_dia[dia] = 0
            por_dia[dia] += o["ganancia"]

        mejor_dia = max(por_dia.items(), key=lambda x: x[1]) if por_dia else ("N/A", 0)
        peor_dia = min(por_dia.items(), key=lambda x: x[1]) if por_dia else ("N/A", 0)

        return {
            "total_operaciones": total,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / total * 100, 1) if total > 0 else 0,
            "ganancia_total": round(ganancia, 2),
            "mejor_dia": {"fecha": mejor_dia[0], "ganancia": round(mejor_dia[1], 2)},
            "peor_dia": {"fecha": peor_dia[0], "ganancia": round(peor_dia[1], 2)},
            "dias_operados": len(por_dia),
            "promedio_diario": round(ganancia / max(len(por_dia), 1), 2),
        }

    def actualizar_estadisticas(self):
        """Guarda estadísticas actualizadas para el dashboard"""
        stats = {
            "hoy": self.stats_hoy(),
            "global": self.stats_global(),
            "ultima_actualizacion": datetime.now().isoformat(),
            "config": {
                "monto_base": CONFIG["monto_base"],
                "martingala": CONFIG["martingala_activa"],
                "multiplicador": CONFIG["multiplicador"],
                "max_pasos": CONFIG["max_pasos"],
                "payout_minimo": CONFIG["payout_minimo"],
            }
        }
        with open(CONFIG["stats_file"], "w") as f:
            json.dump(stats, f, indent=2, default=str)


# ═══════════════════════════════════════════════
# BASE DE DATOS SQLite
# ═══════════════════════════════════════════════

class Database:
    def __init__(self):
        self.path = CONFIG["db_file"]
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # permite lecturas concurrentes
        return conn

    def _init(self):
        with self._lock, self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS escaneos (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT    NOT NULL,
                    payout_minimo   REAL    NOT NULL,
                    total_revisados INTEGER DEFAULT 0,
                    total_calificados INTEGER DEFAULT 0,
                    activos_json    TEXT    -- lista JSON de activos encontrados
                );

                CREATE TABLE IF NOT EXISTS activos_escaneados (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    escaneo_id  INTEGER REFERENCES escaneos(id),
                    timestamp   TEXT    NOT NULL,
                    activo      TEXT    NOT NULL,
                    payout      REAL,
                    califica    INTEGER -- 1 = pasa filtro, 0 = no
                );

                CREATE TABLE IF NOT EXISTS ciclos (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    activo           TEXT    NOT NULL,
                    direccion        TEXT,
                    timestamp_inicio TEXT    NOT NULL,
                    timestamp_fin    TEXT,
                    pasos_usados     INTEGER DEFAULT 1,
                    resultado_final  TEXT,   -- win / loss / tie
                    ganancia_neta    REAL    DEFAULT 0,
                    monto_inicial    REAL,
                    monto_maximo     REAL,
                    payout_inicial   REAL,
                    run_id           INTEGER REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS operaciones (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT    NOT NULL,
                    hora             TEXT    NOT NULL,
                    par              TEXT    NOT NULL,
                    direccion        TEXT,
                    tipo             TEXT,
                    precio_apertura  REAL,
                    resultado        TEXT,
                    pasos_martingala INTEGER DEFAULT 1,
                    monto_total      REAL,
                    ganancia_neta    REAL,
                    pasos_json       TEXT,
                    sma3             REAL,
                    sma50            REAL,
                    pct_cuerpo       REAL,
                    ratio_mechas     REAL,
                    run_id           INTEGER REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_inicio TEXT NOT NULL,
                    monto_base       REAL,
                    multiplicador    REAL,
                    max_pasos        INTEGER,
                    max_mecha_ratio  REAL,
                    pct_cuerpo_min   REAL,
                    payout_minimo    REAL,
                    activos_excluidos TEXT,
                    notas            TEXT
                );

                CREATE TABLE IF NOT EXISTS indicadores (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ciclo_id         INTEGER REFERENCES ciclos(id),
                    run_id           INTEGER REFERENCES runs(id),
                    timestamp        TEXT NOT NULL,
                    activo           TEXT NOT NULL,
                    direccion        TEXT,
                    resultado_final  TEXT,
                    pasos_usados     INTEGER,
                    ganancia_neta    REAL,
                    sma3             REAL,
                    sma50            REAL,
                    distancia_smas   REAL,
                    pct_cuerpo       REAL,
                    ratio_mechas     REAL,
                    precio_entrada   REAL,
                    payout           REAL,
                    gano_p1          INTEGER,
                    gano_p2          INTEGER
                );
            """)

    # ── Indicadores ───────────────────────────────

    def registrar_indicadores(self, datos: dict):
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO indicadores
                   (ciclo_id, run_id, timestamp, activo, direccion,
                    resultado_final, pasos_usados, ganancia_neta,
                    sma3, sma50, distancia_smas,
                    pct_cuerpo, ratio_mechas, precio_entrada,
                    payout, gano_p1, gano_p2)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (datos.get("ciclo_id"), datos.get("run_id"), datetime.now().isoformat(),
                 datos["activo"], datos["direccion"],
                 datos.get("resultado_final"), datos.get("pasos_usados"), datos.get("ganancia_neta"),
                 datos.get("sma3"), datos.get("sma50"), datos.get("distancia_smas"),
                 datos.get("pct_cuerpo"), datos.get("ratio_mechas"), datos.get("precio_entrada"),
                 datos.get("payout"),
                 1 if datos.get("gano_p1") else 0,
                 1 if datos.get("gano_p2") else 0)
            )

    # ── Runs ──────────────────────────────────────

    def iniciar_run(self, notas: str = "") -> int:
        """Registra una nueva corrida del bot con su configuración. Retorna run_id."""
        with self._lock, self._conn() as conn:
            # Marcar datos sin run_id como run 0 (histórico pre-runs)
            conn.execute("UPDATE ciclos SET run_id=0 WHERE run_id IS NULL")
            conn.execute("UPDATE operaciones SET run_id=0 WHERE run_id IS NULL")
            cur = conn.execute(
                """INSERT INTO runs
                   (timestamp_inicio, monto_base, multiplicador, max_pasos,
                    max_mecha_ratio, pct_cuerpo_min, payout_minimo,
                    activos_excluidos, notas)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (datetime.now().isoformat(),
                 CONFIG["monto_base"], CONFIG["multiplicador"], CONFIG["max_pasos"],
                 CONFIG["max_mecha_ratio"], CONFIG["porcentaje_cuerpo"],
                 CONFIG["payout_minimo"],
                 json.dumps(sorted(ACTIVOS_EXCLUIDOS)),
                 notas)
            )
            return cur.lastrowid

    # ── Escaneos ──────────────────────────────────

    def registrar_escaneo(self, activos_encontrados: list, total_revisados: int) -> int:
        activos_json = json.dumps([{"activo": a["nombre"], "payout": a["payout"]} for a in activos_encontrados])
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO escaneos (timestamp, payout_minimo, total_revisados, total_calificados, activos_json)
                   VALUES (?,?,?,?,?)""",
                (datetime.now().isoformat(), CONFIG["payout_minimo"],
                 total_revisados, len(activos_encontrados), activos_json)
            )
            escaneo_id = cur.lastrowid
            ts = datetime.now().isoformat()
            conn.executemany(
                "INSERT INTO activos_escaneados (escaneo_id, timestamp, activo, payout, califica) VALUES (?,?,?,?,?)",
                [(escaneo_id, ts, a["nombre"], a["payout"], 1) for a in activos_encontrados]
            )
        return escaneo_id

    # ── Ciclos ────────────────────────────────────

    def iniciar_ciclo(self, activo: str, direccion: str, payout: float, monto: float,
                      run_id: int = 0) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO ciclos (activo, direccion, timestamp_inicio, payout_inicial,
                   monto_inicial, monto_maximo, run_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (activo, direccion, datetime.now().isoformat(), payout, monto, monto, run_id)
            )
            return cur.lastrowid

    def cerrar_ciclo(self, ciclo_id: int, resultado: str, ganancia_neta: float,
                     pasos_usados: int, monto_maximo: float):
        with self._lock, self._conn() as conn:
            conn.execute(
                """UPDATE ciclos SET timestamp_fin=?, resultado_final=?, ganancia_neta=?,
                   pasos_usados=?, monto_maximo=? WHERE id=?""",
                (datetime.now().isoformat(), resultado, round(ganancia_neta, 2),
                 pasos_usados, monto_maximo, ciclo_id)
            )

    # ── Operaciones ───────────────────────────────

    def registrar_ciclo_completo(self, datos: dict):
        """Inserta UNA fila por ciclo completo (all martingale steps aggregated)."""
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO operaciones
                   (timestamp, hora, par, direccion, tipo, precio_apertura,
                    resultado, pasos_martingala, monto_total, ganancia_neta,
                    pasos_json, sma3, sma50, pct_cuerpo, ratio_mechas, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (datetime.now().isoformat(),
                 datos["hora"],
                 datos["par"],
                 datos["direccion"],
                 "alza" if datos["direccion"] == "call" else "baja",
                 datos["precio_apertura"],
                 datos["resultado"],
                 datos["pasos_martingala"],
                 round(datos["monto_total"], 2),
                 round(datos["ganancia_neta"], 2),
                 json.dumps(datos.get("pasos_json", [])),
                 datos.get("sma3", 0),
                 datos.get("sma50", 0),
                 datos.get("pct_cuerpo", 0),
                 datos.get("ratio_mechas", 0),
                 datos.get("run_id", 0))
            )

    # ── Consultas para el dashboard ───────────────

    def query(self, sql: str, params=()):
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def stats_live(self) -> dict:
        """Genera estadísticas en tiempo real desde SQLite."""
        hoy = datetime.now().strftime("%Y-%m-%d")

        def agg(rows):
            wins   = sum(1 for r in rows if r["resultado"]=="win")
            losses = sum(1 for r in rows if r["resultado"]=="loss")
            ties   = sum(1 for r in rows if r["resultado"]=="tie")
            total  = len(rows)
            gan    = sum(r.get("ganancia_neta",0) or 0 for r in rows)
            wr     = round(wins/total*100, 1) if total else 0

            # racha actual
            racha = 0
            for r in rows:
                if r["resultado"] == "win":
                    racha = racha + 1 if racha >= 0 else 1
                elif r["resultado"] == "loss":
                    racha = racha - 1 if racha <= 0 else -1
                else:
                    racha = 0

            # martingala por paso
            por_paso = {}
            for r in rows:
                p = str(r.get("pasos_martingala", 1) or 1)
                if p not in por_paso:
                    por_paso[p] = {"wins":0,"losses":0,"ties":0,"total":0}
                por_paso[p]["total"] += 1
                if r["resultado"] == "win":   por_paso[p]["wins"]   += 1
                elif r["resultado"] == "loss": por_paso[p]["losses"] += 1
                else:                          por_paso[p]["ties"]   += 1

            return {
                "total_operaciones": total, "wins": wins, "losses": losses, "ties": ties,
                "ganancia_total": round(gan, 2), "winrate": wr,
                "racha_actual": racha, "racha_max_win": 0, "racha_max_loss": 0,
                "por_paso_martingala": por_paso,
                "operaciones": rows[:20],
            }

        ops_hoy   = self.query("SELECT * FROM operaciones WHERE timestamp LIKE ? ORDER BY id DESC", (f"{hoy}%",))
        ops_todas = self.query("SELECT * FROM operaciones ORDER BY id DESC LIMIT 500")
        return {"hoy": agg(ops_hoy), "global": agg(ops_todas)}


# Lock global que serializa TODOS los get_candles del proceso.
# iqoptionapi usa un buffer de clase compartido — llamadas concurrentes
# desde distintas instancias se contaminan entre sí. El lock garantiza
# que solo un hilo a la vez envía y recibe velas por WebSocket.
_candles_lock = threading.Lock()

# ═══════════════════════════════════════════════
# POOL DE CONEXIONES API
# ═══════════════════════════════════════════════

class ApiPool:
    """
    Pool de N conexiones IQ Option independientes.
    Cada hilo de trading toma una conexión exclusiva del pool,
    evitando completamente la contaminación del buffer WebSocket compartido.
    """
    def __init__(self, size: int, email: str, password: str, tipo_cuenta: str):
        self._size       = size
        self._email      = email
        self._password   = password
        self._tipo_cuenta = tipo_cuenta
        self._pool: list = []          # conexiones disponibles
        self._lock       = threading.Lock()
        self._disponible = threading.Semaphore(0)  # se incrementa al conectar cada instancia

    def conectar_todas(self) -> int:
        """Conecta todas las instancias. Retorna cuántas quedaron activas."""
        conectadas = 0
        for i in range(self._size):
            try:
                print(f"   🔌 Pool API [{i+1}/{self._size}] conectando...")
                api = IQ_Option(self._email, self._password)
                ok, reason = api.connect()
                if not ok and reason == "2FA":
                    codigo = input("   Código 2FA: ").strip()
                    ok, reason = api.connect_2fa(codigo)
                if ok:
                    api.change_balance(self._tipo_cuenta)
                    with self._lock:
                        self._pool.append(api)
                    self._disponible.release()
                    conectadas += 1
                    print(f"   ✅ Pool API [{i+1}] lista")
                else:
                    print(f"   ⚠️ Pool API [{i+1}] falló: {reason}")
            except Exception as e:
                print(f"   ⚠️ Pool API [{i+1}] excepción: {e}")
            time.sleep(1)  # pequeña pausa entre conexiones
        return conectadas

    def acquire(self) -> "IQ_Option":
        """Bloquea hasta que haya una API disponible y la retorna."""
        self._disponible.acquire()
        with self._lock:
            return self._pool.pop()

    def release(self, api: "IQ_Option"):
        """Devuelve una API al pool."""
        with self._lock:
            self._pool.append(api)
        self._disponible.release()


# ═══════════════════════════════════════════════
# BOT PRINCIPAL
# ═══════════════════════════════════════════════

class GoldBot:
    def __init__(self):
        self.api = None
        self.conectado = False
        self.telemetria = Telemetria()
        self.db = Database()

        # Control diario
        self.perdidas_hoy = 0
        self.operaciones_hoy = 0
        self.ganancia_hoy = 0
        self.dia_actual = datetime.now().date()

        # Paralelismo
        self._lock = threading.Lock()
        self._trades_activos = set()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()   # Ctrl+P: pausa búsqueda de nuevas señales
        self._sem_paralelo = threading.Semaphore(10)  # máx 10 ciclos simultáneos

        # Estado explícito por activo:
        #   ausente / "libre"  → sin operación activa
        #   "activo"           → paso 1 en curso
        #   "martingala"       → paso 2+ en curso (martingala vigente)
        # Mientras no sea "libre", el coordinador no puede abrir otra operación
        # para ese activo bajo ninguna circunstancia.
        self._estado_activos: dict[str, str] = {}

        # Pool de conexiones API (una por hilo de trading, sin buffer compartido)
        self._api_pool = ApiPool(
            size        = 10,
            email       = CONFIG["email"],
            password    = CONFIG["password"],
            tipo_cuenta = CONFIG["tipo_cuenta"],
        )

        # Contadores de capturas por dirección (reinician al arrancar el bot)
        self._captura_call = 0
        self._captura_put  = 0

        # Trades en vivo para visualización
        self._trades_vivos    = {}   # {activo: info_dict}
        self._trades_recientes = []  # últimos 10 cerrados

        # Caché de velas por activo (para el dashboard)
        self._candle_cache = {}      # {activo: [{"time","open","high","low","close"}, ...]}

        # Progreso de escaneo en vivo
        self._scan_progress = {
            "en_curso": False,
            "timestamp": None,
            "activos": [],   # [{nombre, payout, califica, estado}]
        }

    def conectar(self):
        print("🔌 Conectando a IQ Option...")
        self.api = IQ_Option(CONFIG["email"], CONFIG["password"])
        check, reason = self.api.connect()

        # ── Manejar 2FA (verificación por SMS) ──
        if not check and reason == "2FA":
            print("🔐 2FA detectado — IQ Option envió un código SMS a tu teléfono")
            codigo = input("   Ingresa el código SMS: ").strip()
            check, reason = self.api.connect_2fa(codigo)

        if check:
            self.api.change_balance(CONFIG["tipo_cuenta"])
            balance = self.api.get_balance()
            print(f"✅ Conectado | Balance: ${balance:.2f} ({CONFIG['tipo_cuenta']})")
            self.conectado = True
            self._run_id = self.db.iniciar_run()

            # Conectar pool de APIs dedicadas para los hilos de trading
            print("\n🔌 Inicializando pool de conexiones para operaciones paralelas...")
            n = self._api_pool.conectar_todas()
            print(f"   ✅ Pool listo: {n}/5 conexiones activas\n")
        else:
            print(f"❌ Error de conexión: {reason}")
            print(f"   Posibles causas:")
            print(f"   • Email o contraseña incorrectos")
            print(f"   • IQ Option abierto en el navegador (ciérralo)")
            print(f"   • Problema de conexión a internet")
            print(f"   • websocket-client incorrecto (instala: pip install websocket-client==0.56)")
            self.conectado = False
        return self.conectado

    # ─────────────────────────────────────
    # SELECCIÓN DE ACTIVOS CON PAYOUT > 80%
    # ─────────────────────────────────────

    def obtener_activos_rentables(self) -> list:
        print("\n🔍 Escaneando activos con payout > 80%...")

        self._scan_progress = {
            "en_curso": True,
            "timestamp": datetime.now().isoformat(),
            "activos": [],
        }

        try:
            self.api.update_ACTIVES_OPCODE()
        except Exception:
            pass
        time.sleep(2)
        todos = self.api.get_all_open_time() or {}

        activos_buenos = []
        payout_data = {}
        try:
            payout_data = self.api.get_all_profit() or {}
        except Exception:
            pass

        binarios = todos.get("binary", {})
        for activo, info in binarios.items():
            if not info.get("open"):
                continue

            try:
                profit = payout_data.get(activo, {})
                if isinstance(profit, dict):
                    # {"turbo": 0.82, "binary": 0.84}
                    payout = profit.get("binary", profit.get("turbo", 0)) * 100
                elif isinstance(profit, (int, float)) and profit > 0:
                    payout = profit * 100
                else:
                    payout = 0
            except Exception:
                payout = 0

            payout = round(payout, 1)
            califica = payout >= CONFIG["payout_minimo"]

            # publicar progreso en tiempo real
            self._scan_progress["activos"].append({
                "nombre": activo,
                "payout": payout,
                "califica": califica,
            })

            if califica and activo not in ACTIVOS_EXCLUIDOS:
                activos_buenos.append({"nombre": activo, "payout": payout})

        activos_buenos.sort(key=lambda x: x["payout"], reverse=True)

        self._scan_progress["en_curso"] = False
        self._scan_progress["activos"].sort(key=lambda x: x["payout"], reverse=True)

        print(f"   📋 Activos con payout ≥ {CONFIG['payout_minimo']}%:")
        for a in activos_buenos[:15]:
            print(f"      {a['nombre']:20s} → {a['payout']}%")
        if not activos_buenos:
            print(f"   ⚠️ No hay activos con payout ≥ {CONFIG['payout_minimo']}%")

        return activos_buenos

    def obtener_payout_actual(self, activo: str) -> float:
        try:
            payout_data = self.api.get_all_profit() or {}
            profit = payout_data.get(activo, {})
            if isinstance(profit, dict):
                val = profit.get("binary", profit.get("turbo", 0))
            elif isinstance(profit, (int, float)):
                val = profit
            else:
                val = 0
            if val > 0:
                return round(val * 100, 1)
        except:
            pass
        return 0

    # ─────────────────────────────────────
    # OBTENER VELAS
    # ─────────────────────────────────────

    def obtener_velas(self, activo: str) -> pd.DataFrame:
        # Siempre descarga 200 velas completas — el fetch incremental de 3 velas
        # era susceptible a contaminación del buffer WebSocket con precios de
        # otros activos, causando entradas falsas y martingalas incorrectas.
        with _candles_lock:
            velas = self.api.get_candles(
                activo, CONFIG["timeframe"],
                CONFIG["num_velas"], time.time()
            )
        df = pd.DataFrame(velas)
        df = df.rename(columns={
            "open": "open", "close": "close",
            "max": "high", "min": "low",
            "volume": "volume", "from": "timestamp"
        })
        df["fecha"] = pd.to_datetime(df["timestamp"], unit="s")
        df = df.sort_values("timestamp").reset_index(drop=True)
        df = df[["fecha", "timestamp", "open", "high", "low", "close", "volume"]]

        # Actualizar caché para el dashboard (convertir numpy → Python nativo)
        self._guardar_cache(activo, df)
        return df

    def _guardar_cache(self, activo: str, df: pd.DataFrame):
        """Guarda velas (con o sin SMAs) en el caché del dashboard."""
        candles = []
        for _, r in df.iterrows():
            c = {
                "time":  int(r["timestamp"]),
                "open":  float(r["open"]),
                "high":  float(r["high"]),
                "low":   float(r["low"]),
                "close": float(r["close"]),
            }
            for col, key in [("sma_rapida","sma3"), ("sma_tendencia","sma50")]:
                if col in df.columns:
                    v = r[col]
                    if not pd.isna(v):
                        c[key] = float(v)
            candles.append(c)
        with self._lock:
            self._candle_cache[activo] = candles

    # ─────────────────────────────────────
    # MARTINGALA
    # ─────────────────────────────────────

    # ─────────────────────────────────────
    # EJECUCIÓN DE OPERACIONES
    # ─────────────────────────────────────

    # ─────────────────────────────────────
    # DETECCIÓN DE RESULTADO (POLLING)
    # ─────────────────────────────────────

    def _extraer_order_id(self, order_raw):
        """Extrae el ID entero de la respuesta de api.buy(), que varía según versión."""
        if isinstance(order_raw, dict):
            return order_raw.get("id") or order_raw.get("order_id") or order_raw.get("act")
        elif isinstance(order_raw, (list, tuple)):
            return order_raw[0]
        return order_raw

    def _extraer_profit(self, raw) -> float:
        """Convierte el valor raw de buy_order_changed a float de profit."""
        if isinstance(raw, (int, float)):
            return float(raw)
        elif isinstance(raw, dict):
            v = raw.get("profit") or raw.get("win") or raw.get("amount") or raw.get("earn") or 0
            return float(v)
        elif isinstance(raw, (list, tuple)):
            return float(raw[0]) if raw else 0.0
        return float(raw)


    def _obtener_resultado_orden(self, order_id, activo: str,
                                 precio_entrada: float, direccion: str,
                                 monto: float, payout: float,
                                 api: "IQ_Option" = None) -> float:
        """
        Espera el vencimiento y obtiene el resultado REAL de IQ Option via
        buy_order_changed[order_id]. Solo usa comparación de velas como fallback.

        Retorna: ganancia neta (positivo=WIN, 0=TIE, negativo=LOSS)
        """
        tf = CONFIG["timeframe"]
        expira_en = CONFIG["expiracion"] * tf  # 60s fijos desde colocación (tick-by-tick IQ Option)

        # — Countdown —
        t_inicio = time.time()
        while True:
            if self._stop_event.is_set():
                break
            elapsed  = time.time() - t_inicio
            restante = expira_en - elapsed
            if restante <= 0:
                break
            print(f"\r   ⏳ EN OPERACIÓN [{activo}] {direccion.upper()} "
                  f"entrada={precio_entrada:.5f} — vence en {restante:5.1f}s ...",
                  end="", flush=True)
            time.sleep(0.5)
        print()

        _api = api if api is not None else self.api

        # — Resultado real de IQ Option (socket_option_closed por order_id) —
        if order_id:
            for intento in range(1, 181):  # hasta 90s (0.5s por iteración)
                try:
                    raw = _api.api.socket_option_closed.get(order_id)
                    if raw is not None:
                        msg     = raw.get("msg", {}) if isinstance(raw, dict) else {}
                        win_str = str(msg.get("win", "")).lower()
                        if win_str == "win":
                            ganancia = round(float(msg.get("win_amount", 0)) - float(msg.get("sum", monto)), 4)
                            print(f"   🏆 [{activo}] WIN +${ganancia:.4f}  (IQ: {win_str})")
                            return ganancia
                        elif win_str == "equal":
                            print(f"   ⚖️  [{activo}] TIE — empate")
                            return 0.0
                        else:  # "loose"
                            print(f"   ❌ [{activo}] LOSS  (IQ: {win_str})")
                            return -monto
                except Exception as e:
                    print(f"   ⚠️ [{activo}] socket_option_closed intento {intento}: {e}")
                time.sleep(0.5)
            print(f"   ⚠️ [{activo}] Timeout esperando resultado IQ Option (30s) — usando velas como fallback")

        # — Fallback: comparación de velas (si IQ Option no envió resultado) —
        ts_expira = t_inicio + expira_en
        precio_cierre = None
        for intento in range(1, 6):
            try:
                with _candles_lock:
                    velas = _api.get_candles(activo, tf, 3, time.time())
                if velas:
                    for v in reversed(velas):
                        if v.get("from", 0) < ts_expira:
                            precio_cierre = float(v.get("close", 0))
                            print(f"   🕯️  [{activo}] fallback cierre={precio_cierre:.5f} (intento {intento})")
                            break
                if precio_cierre:
                    break
            except Exception as e:
                print(f"   ⚠️ [{activo}] get_candles intento {intento}: {e}")
            time.sleep(1)

        if not precio_cierre:
            print(f"   ⚠️ [{activo}] Sin precio de cierre — asumiendo LOSS")
            return -monto

        diferencia = precio_cierre - precio_entrada
        if abs(diferencia) < 0.0001:
            print(f"   ⚠️ [{activo}] diff={diferencia:+.5f} muy pequeño — LOSS")
            return -monto

        if (direccion == "call" and diferencia > 0) or (direccion == "put" and diferencia < 0):
            ganancia = round(monto * (payout / 100), 4)
            res = "WIN"
        else:
            ganancia = -monto
            res = "LOSS"

        print(f"   📊 [{activo}] entrada={precio_entrada:.5f} → cierre={precio_cierre:.5f} "
              f"| diff={diferencia:+.5f} | {res} {'+' if ganancia>=0 else ''}{ganancia:.4f}")
        return ganancia

    # ─────────────────────────────────────
    # EJECUTAR UNA OPERACIÓN INDIVIDUAL
    # ─────────────────────────────────────

    def _ejecutar_paso(self, activo: str, direccion: str, monto: float,
                       payout: float, paso: int, ciclo_id: int,
                       vela_data: dict, api: "IQ_Option" = None) -> dict:
        """
        Coloca la orden con hasta MAX_REINTENTOS si es rechazada.
        Espera resultado inmediato. Registra en DB.
        """
        MAX_REINTENTOS = 3
        ESPERA_REINTENTO = 2   # segundos entre reintentos de buy()

        print(f"\n   {'🟢' if direccion=='call' else '🔴'} PASO {paso}/{CONFIG['max_pasos']} "
              f"| {activo} | {direccion.upper()} | ${monto:.2f} | Payout: {payout}%")

        # — Registrar en dashboard como operación viva —
        ts_expira = (datetime.now() + timedelta(minutes=CONFIG["expiracion"])).isoformat()
        with self._lock:
            self._trades_vivos[activo] = {
                "activo": activo, "direccion": direccion,
                "monto": monto, "payout": payout, "paso": paso,
                "ts_inicio": datetime.now().isoformat(),
                "ts_expira": ts_expira,
                "precio_entrada": vela_data.get("close", 0),
                "ciclo_id": ciclo_id,
                "resultado": "en_espera",
            }

        # — Colocar la orden (con reintentos si es rechazada) —
        order_id = None
        for intento_buy in range(1, MAX_REINTENTOS + 1):
            _api = api if api is not None else self.api
            status, order_raw = _api.buy(monto, activo, direccion, CONFIG["expiracion"])
            if status:
                order_id = self._extraer_order_id(order_raw)
                print(f"   🛠️  [{activo}] buy() intento {intento_buy} → id={order_id!r}")
                if order_id:
                    break
                print(f"   ⚠️ [{activo}] order_id inválido en intento {intento_buy}")
            else:
                razon = str(order_raw) if order_raw else "sin respuesta"
                print(f"   ❌ [{activo}] Orden rechazada (intento {intento_buy}/{MAX_REINTENTOS}): {razon}")

            if intento_buy < MAX_REINTENTOS:
                print(f"   🔄 [{activo}] Reintentando en {ESPERA_REINTENTO}s...")
                time.sleep(ESPERA_REINTENTO)

        if not order_id:
            print(f"   💀 [{activo}] No se pudo colocar la orden tras {MAX_REINTENTOS} intentos")
            with self._lock:
                self._trades_vivos.pop(activo, None)
            return {"ejecutada": False, "razon": "orden rechazada tras reintentos"}

        # Precio de entrada: vela actual justo tras el buy() para que coincida
        # con el precio real de IQ Option — especialmente importante en martingala
        # donde la vela señal original puede tener minutos de antigüedad.
        precio_entrada = float(vela_data.get("close", 0))  # fallback
        try:
            with _candles_lock:
                velas_ahora = _api.get_candles(activo, CONFIG["timeframe"], 1, time.time())
            if velas_ahora:
                precio_entrada = float(velas_ahora[-1].get("close", 0))
        except Exception:
            pass  # si falla, usa el precio de la vela señal como fallback
        print(f"   ✅ [{activo}] Orden #{order_id} colocada | entrada: {precio_entrada:.5f}")

        # Limpiar resultado anterior para este order_id (evita leer stale data)
        try:
            _api.api.socket_option_closed.pop(order_id, None)
        except Exception:
            pass

        # — Esperar y calcular resultado por precio —
        resultado_valor = self._obtener_resultado_orden(
            order_id, activo, precio_entrada, direccion, monto, payout, api=api
        )

        if resultado_valor > 0:
            resultado = "win"
            print(f"   🏆 [{activo}] WIN +${resultado_valor:.4f}")
        elif resultado_valor == 0:
            resultado = "tie"
            print(f"   🟡 [{activo}] TIE (empate)")
        else:
            resultado = "loss"
            print(f"   💔 [{activo}] LOSS -${abs(resultado_valor):.2f}")

        # Actualizar contadores en memoria (instantáneo, sin I/O)
        with self._lock:
            self.operaciones_hoy += 1
            self.ganancia_hoy    += resultado_valor
            if resultado == "loss":
                self.perdidas_hoy += 1
            # Mover de vivos a recientes en dashboard
            info = self._trades_vivos.pop(activo, {})
            info.update({"resultado": resultado, "ganancia": resultado_valor,
                         "ts_fin": datetime.now().isoformat()})
            self._trades_recientes.insert(0, info)
            self._trades_recientes[:] = self._trades_recientes[:20]

        return {
            "ejecutada":       True,
            "resultado":       resultado,
            "ganancia":        resultado_valor,
            "precio_entrada":  precio_entrada,
        }

    # ─────────────────────────────────────
    # CICLO COMPLETO: TRADE + MARTINGALA
    # ─────────────────────────────────────

    def _ciclo_trade(self, activo: str, direccion: str, vela_data: dict, ruta_captura: str = ""):
        """
        Ejecuta el ciclo completo de trading con martingala.
        Flujo:
          paso 1 → resultado?
            WIN  → termina ciclo
            LOSS → martingala paso 2 (espera nueva vela) → resultado?
              WIN  → termina ciclo
              LOSS → martingala paso 3 ...
              (hasta max_pasos)
        """
        payout = self.obtener_payout_actual(activo)
        if payout < CONFIG["payout_minimo"]:
            print(f"   ⚠️ [{activo}] Payout {payout}% insuficiente — ciclo cancelado")
            return

        # Adquirir una conexión API exclusiva para este hilo
        api = self._api_pool.acquire()

        try:
            ciclo_id = self.db.iniciar_ciclo(activo, direccion, payout, CONFIG["monto_base"],
                                              run_id=self._run_id)
            print(f"\n{'─'*55}")
            print(f"   🎯 CICLO INICIADO [{activo}] {direccion.upper()} | payout: {payout}% | ciclo_id: {ciclo_id}")
            print(f"{'─'*55}")

            ganancia_neta   = 0.0
            monto_total     = 0.0
            monto_maximo    = CONFIG["monto_base"]
            resultado_final = "loss"
            paso_final      = 1
            precio_apertura = float(vela_data.get("close", 0))
            hora_inicio     = datetime.now().strftime("%H:%M:%S")
            pasos_detalle   = []

            for paso in range(1, CONFIG["max_pasos"] + 1):
                puede, razon = self.verificar_limites()
                if not puede:
                    print(f"   🛡️ [{activo}] Límite alcanzado: {razon} — ciclo cancelado")
                    resultado_final = "cancelado"
                    break

                monto = round(CONFIG["monto_base"] * (CONFIG["multiplicador"] ** (paso - 1)), 2)
                monto_maximo  = max(monto_maximo, monto)
                monto_total  += monto
                paso_final    = paso

                if paso > 1:
                    # Marcar estado martingala antes de ejecutar el paso
                    with self._lock:
                        self._estado_activos[activo] = "martingala"
                    print(f"\n   🔄 [{activo}] MARTINGALA PASO {paso}/{CONFIG['max_pasos']} → ${monto:.2f}")

                res = self._ejecutar_paso(
                    activo, direccion, monto, payout, paso, ciclo_id, vela_data, api=api
                )

                if not res["ejecutada"]:
                    print(f"   ❌ [{activo}] Paso {paso} no ejecutado — ciclo terminado")
                    resultado_final = "error"
                    break

                ganancia_neta  += res["ganancia"]
                resultado_final = res["resultado"]
                # Use actual fill price from paso 1 as the cycle's entry price
                if paso == 1:
                    precio_apertura = res.get("precio_entrada", precio_apertura)
                pasos_detalle.append({
                    "paso":      paso,
                    "monto":     monto,
                    "resultado": res["resultado"],
                    "ganancia":  round(res["ganancia"], 2),
                })

                print(f"   📈 [{activo}] Paso {paso} → {res['resultado'].upper()} | "
                      f"Neto acumulado: ${ganancia_neta:+.2f}")

                if res["resultado"] == "win":
                    print(f"\n   ✅ [{activo}] CICLO GANADO en paso {paso} | Neto: ${ganancia_neta:+.2f}")
                    break

                # LOSS o TIE: continuar martingala si está activa
                if not CONFIG["martingala_activa"]:
                    print(f"   🔴 [{activo}] {res['resultado'].upper()} | Martingala OFF — ciclo terminado")
                    break

                # Si el bot está pausado (Ctrl+P), no continuar con más pasos de martingala
                if self._pause_event.is_set():
                    print(f"   ⏸️  [{activo}] PAUSADO — martingala detenida en paso {paso} | Neto: ${ganancia_neta:+.2f}")
                    break

                if paso == CONFIG["max_pasos"]:
                    print(f"\n   💀 [{activo}] CICLO PERDIDO — {paso} pasos agotados | Neto: ${ganancia_neta:+.2f}")

            # Balance solo al final del ciclo (evita roundtrip por cada paso)
            try:
                balance_final = api.get_balance()
                print(f"   💰 [{activo}] Balance final: ${balance_final:.2f}")
            except Exception as e_bal:
                print(f"   ⚠️ [{activo}] No se pudo obtener balance: {e_bal}")
            self.db.cerrar_ciclo(ciclo_id, resultado_final, ganancia_neta, paso_final, monto_maximo)
            self._renombrar_captura(ruta_captura, resultado_final)

            # Guardar datos de la señal en tabla indicadores (estrategia 2 pasos)
            try:
                gano_p1 = len(pasos_detalle) >= 1 and pasos_detalle[0]["resultado"] == "win"
                gano_p2 = len(pasos_detalle) >= 2 and pasos_detalle[1]["resultado"] == "win"
                self.db.registrar_indicadores({
                    "ciclo_id":       ciclo_id,
                    "run_id":         self._run_id,
                    "activo":         activo,
                    "direccion":      direccion,
                    "resultado_final":resultado_final,
                    "pasos_usados":   paso_final,
                    "ganancia_neta":  ganancia_neta,
                    "sma3":           vela_data.get("sma3"),
                    "sma50":          vela_data.get("sma50"),
                    "distancia_smas": vela_data.get("distancia_smas"),
                    "pct_cuerpo":     vela_data.get("pct_cuerpo"),
                    "ratio_mechas":   vela_data.get("ratio_mechas"),
                    "precio_entrada": precio_apertura,
                    "payout":         payout,
                    "gano_p1":        gano_p1,
                    "gano_p2":        gano_p2,
                })
            except Exception as e_ind:
                print(f"   ⚠️ [{activo}] Error guardando indicadores: {e_ind}")

            # Guardar ciclo completo en operaciones (una sola fila por ciclo)
            try:
                self.db.registrar_ciclo_completo({
                    "hora":            hora_inicio,
                    "par":             activo,
                    "direccion":       direccion,
                    "precio_apertura": precio_apertura,
                    "resultado":       resultado_final,
                    "pasos_martingala": paso_final,
                    "monto_total":     monto_total,
                    "ganancia_neta":   ganancia_neta,
                    "pasos_json":      pasos_detalle,
                    "sma3":            vela_data.get("sma3", 0),
                    "sma50":           vela_data.get("sma50", 0),
                    "pct_cuerpo":      vela_data.get("pct_cuerpo", 0),
                    "ratio_mechas":    vela_data.get("ratio_mechas", 0),
                    "run_id":          self._run_id,
                })
                print(f"   💾 [{activo}] Ciclo #{ciclo_id} guardado en DB — {resultado_final.upper()} | Neto: ${ganancia_neta:+.2f}")
            except Exception as e_db:
                print(f"   ⚠️ [{activo}] Error guardando en DB: {e_db}")

        finally:
            # Siempre devolver la API al pool, aunque ocurra una excepción
            self._api_pool.release(api)

    # ─────────────────────────────────────
    # GESTIÓN DE RIESGO
    # ─────────────────────────────────────

    def verificar_limites(self) -> tuple:
        """Verifica si se puede seguir operando. Retorna (puede_operar, razon)"""
        # Nuevo día → resetear contadores
        if datetime.now().date() != self.dia_actual:
            self.dia_actual = datetime.now().date()
            self.perdidas_hoy = 0
            self.operaciones_hoy = 0
            self.ganancia_hoy = 0

        if self.perdidas_hoy >= CONFIG["max_perdidas_diarias"]:
            return False, f"Límite de {CONFIG['max_perdidas_diarias']} pérdidas diarias alcanzado"

        if self.operaciones_hoy >= CONFIG["max_operaciones_dia"]:
            return False, f"Límite de {CONFIG['max_operaciones_dia']} operaciones diarias alcanzado"

        if CONFIG["ganancia_objetivo_dia"] > 0 and self.ganancia_hoy >= CONFIG["ganancia_objetivo_dia"]:
            return False, f"Meta diaria de ${CONFIG['ganancia_objetivo_dia']} alcanzada (+${self.ganancia_hoy:.2f})"

        return True, "OK"

    # ─────────────────────────────────────
    # ESPERAR NUEVA VELA
    # ─────────────────────────────────────

    def esperar_nueva_vela(self):
        ahora = time.time()
        tf = CONFIG["timeframe"]
        restante = tf - (ahora % tf)
        print(f"   ⏳ Próxima vela en {restante:.0f}s...")
        time.sleep(restante + 0.5)   # 0.5s de margen para que la vela esté cerrada

    # ─────────────────────────────────────
    # LOOP PRINCIPAL (UN HILO, SECUENCIAL)
    # ─────────────────────────────────────

    def _log_señal(self, activo: str, vela) -> str:
        """Devuelve texto diagnóstico de por qué no hubo señal."""
        umbral = CONFIG["porcentaje_cuerpo"]
        pct_e  = round(float(vela["pct_encima_sma3"]), 2)
        pct_d  = round(float(vela["pct_debajo_sma3"]), 2)
        mecha  = round(float(vela["suma_mechas"] / vela["cuerpo"]), 2) if vela["cuerpo"] > 0 else 99
        motivo = []
        if not vela["tendencia_alcista"] and not vela["tendencia_bajista"]:
            motivo.append("SMA50 plana")
        elif vela["tendencia_alcista"] and not vela["verde_encima_cian"]:
            motivo.append("SMA3<SMA50")
        elif vela["tendencia_bajista"] and not vela["verde_debajo_cian"]:
            motivo.append("SMA3>SMA50")
        if not vela["es_alcista"] and not vela["es_bajista"]:
            motivo.append("doji")
        if vela["tendencia_alcista"] and vela["es_alcista"] and pct_e < umbral:
            motivo.append(f"cuerpo {pct_e:.0%}<{umbral:.0%}")
        if vela["tendencia_bajista"] and vela["es_bajista"] and pct_d < umbral:
            motivo.append(f"cuerpo {pct_d:.0%}<{umbral:.0%}")
        if not vela["mechas_ok"]:
            motivo.append(f"mechas {mecha:.0%}>{CONFIG['max_mecha_ratio']:.0%}")
        if vela["tendencia_alcista"] and vela["es_alcista"] and not vela["pullback_call"]:
            motivo.append("sin pullback (no hay roja en últimas 5)")
        if vela["tendencia_bajista"] and vela["es_bajista"] and not vela["pullback_put"]:
            motivo.append("sin pullback (no hay verde en últimas 5)")
        if not vela["sin_cruce_reciente"]:
            motivo.append("cruce SMA50 en últimas 15 velas")
        if vela["tendencia_alcista"] and vela["es_alcista"] and not vela["sin_roja_dominante"]:
            motivo.append("vela roja >50% bajo SMA3 en últimas 5")
        if vela["tendencia_bajista"] and vela["es_bajista"] and not vela["sin_verde_dominante"]:
            motivo.append("vela verde >50% sobre SMA3 en últimas 5")
        return " · ".join(motivo) if motivo else "condiciones mixtas"

    def _generar_captura(self, df: "pd.DataFrame", activo: str, direccion: str,
                          payout: float, monto: float) -> str:
        """Genera PNG antes de entrar. Retorna la ruta para renombrar al cerrar el ciclo."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import mplfinance as mpf
            import matplotlib.patches as mpatches
            import os

            carpeta = f"capturas_2p/{direccion.upper()}"
            os.makedirs(carpeta, exist_ok=True)

            vista = df.iloc[-11:-1].copy()
            vista.index = pd.to_datetime(vista.index)

            ohlc = vista[["open", "high", "low", "close"]].copy()
            ohlc.columns = ["Open", "High", "Low", "Close"]
            ohlc["Volume"] = 0

            sma3_s  = vista["sma_rapida"]
            sma50_s = vista["sma_tendencia"]

            ap = [
                mpf.make_addplot(sma3_s,  color="#00e676", width=1.6, label="SMA 3"),
                mpf.make_addplot(sma50_s, color="#29b6f6", width=2.0, label="SMA 50"),
            ]

            mc = mpf.make_marketcolors(
                up="#26a69a", down="#ef5350", edge="inherit",
                wick={"up": "#26a69a", "down": "#ef5350"}, volume="in"
            )
            style = mpf.make_mpf_style(
                base_mpf_style="nightclouds", marketcolors=mc,
                gridstyle=":", gridcolor="#2a2a2a",
                facecolor="#0d0d0d", figcolor="#0d0d0d", edgecolor="#333333",
                rc={"font.size": 9, "text.color": "#cccccc",
                    "axes.labelcolor": "#cccccc",
                    "xtick.color": "#888888", "ytick.color": "#888888"}
            )

            icono_dir = "PUT" if direccion == "put" else "CALL"
            titulo = f"{icono_dir} — {activo}   |   Payout: {payout:.0f}%   |   Monto: ${monto:.2f}"

            fig, axes = mpf.plot(
                ohlc, type="candle", style=style, addplot=ap,
                title=f"\n{titulo}", ylabel="Precio",
                figsize=(13, 6), returnfig=True, warn_too_much_data=9999,
            )

            ax = axes[0]
            ax.title.set_color("#ff7043" if direccion == "put" else "#00e676")
            ax.title.set_fontsize(12)
            ax.title.set_fontweight("bold")

            patches = [
                mpatches.Patch(color="#00e676", label=f"SMA 3  {float(sma3_s.iloc[-1]):.4f}"),
                mpatches.Patch(color="#29b6f6", label=f"SMA 50  {float(sma50_s.iloc[-1]):.4f}"),
            ]
            ax.legend(handles=patches, loc="upper left", facecolor="#1a1a1a",
                      edgecolor="#444", labelcolor="#cccccc", fontsize=9)

            vela_señal = df.iloc[-2]
            cuerpo = float(vela_señal.get("cuerpo", 0))
            mechas = float(vela_señal.get("suma_mechas", 0))
            ratio_m = mechas / cuerpo if cuerpo > 0 else 0
            info = (f"Mechas/Cuerpo: {ratio_m:.0%}   Dirección: {direccion.upper()}")
            fig.text(0.98, 0.03, info, ha="right", va="bottom", fontsize=8,
                     color="#aaaaaa",
                     bbox=dict(facecolor="#1a1a1a", edgecolor="#444", alpha=0.8))

            with self._lock:
                if direccion == "call":
                    self._captura_call += 1
                    n_seq = self._captura_call
                else:
                    self._captura_put += 1
                    n_seq = self._captura_put

            ruta = os.path.join(carpeta,
                                f"{direccion.upper()}_{n_seq:04d}_{activo.replace('/', '_')}.png")
            fig.savefig(ruta, dpi=120, bbox_inches="tight", facecolor="#0d0d0d")
            import matplotlib.pyplot as plt
            plt.close(fig)
            print(f"  📸 [{activo}] Captura guardada: {ruta}")
            return ruta

        except Exception as e:
            print(f"  ⚠️ [{activo}] Error generando captura: {e}")
            return ""

    def _renombrar_captura(self, ruta: str, resultado: str):
        """Agrega _WIN o _LOSS al nombre del archivo al cerrar el ciclo."""
        if not ruta:
            return
        try:
            import os
            sufijo = "_WIN" if resultado == "win" else "_LOSS"
            base, ext = os.path.splitext(ruta)
            nueva = f"{base}{sufijo}{ext}"
            os.rename(ruta, nueva)
        except Exception as e:
            print(f"  ⚠️ Error renombrando captura: {e}")

    def _escuchar_teclado(self):
        """Hilo daemon — detecta Ctrl+Q para pausar/reanudar búsqueda de señales."""
        import msvcrt
        while not self._stop_event.is_set():
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    # Ctrl+Q = carácter \x11
                    if ch == "\x11":
                        if self._pause_event.is_set():
                            self._pause_event.clear()
                            print("\n\n  ▶️  BÚSQUEDA REANUDADA — buscando nuevas señales...\n")
                        else:
                            self._pause_event.set()
                            with self._lock:
                                activos_vivos = dict(self._estado_activos)
                            if activos_vivos:
                                nombres = ", ".join(activos_vivos.keys())
                                print(f"\n\n  ⏸️  PAUSADO — no se abrirán nuevas operaciones")
                                print(f"  ⌛ Esperando que finalicen: {nombres}\n")
                            else:
                                print(f"\n\n  ⏸️  PAUSADO — no hay operaciones activas\n")
            except Exception:
                pass
            time.sleep(0.1)

    def _coordinador(self):
        """
        Loop principal — UN hilo, completamente secuencial.
        Flujo:
          1. Escanear activos con payout >= 80%
          2. Esperar cierre de vela
          3. Evaluar señal en cada activo (orden de payout)
          4. Si hay señal → ejecutar ciclo completo (trade + martingala)
          5. Al terminar el ciclo → volver a paso 1 (rescanear)
        Solo hay UNA operación activa a la vez.
        """
        ultimo_scan = 0
        activos_scan = []

        while not self._stop_event.is_set():
            # ── 1. Rescanear payout cada rescan_intervalo minutos ──
            ahora = time.time()
            if ahora - ultimo_scan >= CONFIG["rescan_intervalo"] * 60:
                print(f"\n{'═'*62}")
                print(f"  🔍 ESCANEO DE PAYOUT  [{datetime.now().strftime('%H:%M:%S')}]")
                print(f"{'═'*62}")
                activos_scan = self.obtener_activos_rentables()
                ultimo_scan  = time.time()

                total_revisados = len(self.api.get_all_open_time().get("binary", {})) if activos_scan else 0
                self.db.registrar_escaneo(activos_scan, total_revisados)

                if not activos_scan:
                    print("  ⚠️  Sin activos rentables — esperando 5 min...")
                    for _ in range(300):
                        if self._stop_event.is_set(): break
                        time.sleep(1)
                    continue

                print(f"\n  {'ACTIVO':<26} {'PAYOUT':>7}")
                print(f"  {'─'*36}")
                for a in activos_scan:
                    print(f"  {a['nombre']:<26} {a['payout']:>6.1f}%")

            if not activos_scan:
                time.sleep(10)
                continue

            # ── 2. Verificar límites diarios ──
            puede, razon = self.verificar_limites()
            if not puede:
                print(f"\n  🛡️ Límite alcanzado: {razon} — pausando 10 min...")
                for _ in range(600):
                    if self._stop_event.is_set(): break
                    time.sleep(1)
                continue

            # ── 3. Esperar cierre de vela ──
            self.esperar_nueva_vela()
            if self._stop_event.is_set():
                break

            # ── 3b. Verificar pausa (Ctrl+P) ──
            if self._pause_event.is_set():
                with self._lock:
                    activos_vivos = dict(self._estado_activos)
                if activos_vivos:
                    nombres = ", ".join(activos_vivos.keys())
                    print(f"\n  ⏸️  PAUSADO — esperando que finalicen: {nombres}")
                else:
                    print(f"\n  ⏸️  PAUSADO — sin operaciones activas. Ctrl+P para reanudar.")
                # Esperar hasta que se reanude o se detenga
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    break
                continue

            print(f"\n{'─'*62}")
            print(f"  👁️  ESCANEANDO SEÑALES  [{datetime.now().strftime('%H:%M:%S')}]  ({len(activos_scan)} activos)")
            print(f"{'─'*62}")

            # ── 4. Evaluar señales (hasta 5 en paralelo) ──
            señales_lanzadas = 0
            for activo_info in activos_scan:
                activo = activo_info["nombre"]
                if self._stop_event.is_set():
                    break
                if señales_lanzadas >= 2:
                    break

                try:
                    df = self.obtener_velas(activo)
                    df = calcular_señales(df)
                    self._guardar_cache(activo, df)
                    vela = df.iloc[-2]

                    vela_data = {
                        "sma3":          round(float(vela["sma_rapida"]),    5) if not pd.isna(vela["sma_rapida"])    else 0,
                        "sma50":         round(float(vela["sma_tendencia"]), 5) if not pd.isna(vela["sma_tendencia"]) else 0,
                        "distancia_smas":round(float(vela["distancia_smas"]), 6) if not pd.isna(vela["distancia_smas"]) else 0,
                        "pct_cuerpo":    round(float(max(vela["pct_encima_sma3"], vela["pct_debajo_sma3"])), 3),
                        "ratio_mechas":  round(float(vela["suma_mechas"] / vela["cuerpo"]), 3) if vela["cuerpo"] > 0 else 0,
                        "close":         round(float(vela["close"]), 5),
                    }

                    if vela["COMPRAR"]:
                        direccion = "call"
                    elif vela["VENDER"]:
                        direccion = "put"
                    else:
                        motivo = self._log_señal(activo, vela)
                        print(f"  ⬜ {activo:<26} sin señal  ({motivo})")
                        continue

                    # Atómico: verificar estado + adquirir slot + marcar "activo"
                    with self._lock:
                        estado = self._estado_activos.get(activo, "libre")
                        if estado != "libre":
                            print(f"  ⏭️  {activo:<26} estado={estado} — omitido")
                            continue
                        if not self._sem_paralelo.acquire(blocking=False):
                            print(f"  ⏭️  Máximo de operaciones paralelas alcanzado ({self._max_paralelo})")
                            break
                        self._estado_activos[activo] = "activo"

                    icono = "🟢" if direccion == "call" else "🔴"
                    print(f"\n  {icono} SEÑAL {direccion.upper()} → {activo} | estado=activo ({activo_info['payout']}%)")
                    print(f"  📋 Estados: { {k: v for k,v in self._estado_activos.items()} }")

                    # Captura antes de entrar — bloqueante (necesitamos la ruta antes del ciclo)
                    ruta_captura = self._generar_captura(
                        df, activo, direccion, activo_info["payout"], CONFIG["monto_base"]
                    )

                    def _lanzar(act=activo, dir=direccion, vd=vela_data, ruta=ruta_captura):
                        try:
                            self._ciclo_trade(act, dir, vd, ruta_captura=ruta)
                        finally:
                            with self._lock:
                                self._estado_activos.pop(act, None)
                                print(f"  🗑️  [{act}] liberado | Estados: {dict(self._estado_activos)}")
                            self._sem_paralelo.release()

                    hilo = threading.Thread(target=_lanzar, daemon=True)
                    hilo.start()
                    señales_lanzadas += 1
                    ultimo_scan = 0

                except Exception as e:
                    print(f"  ⚠️ [{activo}] Error evaluando señal: {e}")

            if señales_lanzadas == 0:
                print(f"\n  ⏸️  Sin señales esta vela — esperando próxima...")
            else:
                print(f"\n  🚀 {señales_lanzadas} operación(es) lanzadas en paralelo")

            # Mostrar estadísticas de sesión
            stats = self.telemetria.stats_hoy()
            if stats["total_operaciones"] > 0:
                print(f"  📊 Sesión: {stats['wins']}W / {stats['losses']}L  |  "
                      f"WR: {stats['winrate']:.0f}%  |  "
                      f"P/L: ${stats['ganancia_total']:+.2f}")
                self._mostrar_tabla_ciclos()

        print(f"\n{'═'*62}")
        print(f"  🛑 Coordinador detenido")

    def detener(self):
        """Detiene el bot de forma limpia"""
        print("\n🛑 Señal de parada recibida — deteniendo bot...")
        self._stop_event.set()

    # ─────────────────────────────────────
    # BUCLE PRINCIPAL
    # ─────────────────────────────────────

    def iniciar(self):
        if not self.conectado:
            print("❌ Ejecuta conectar() primero")
            return

        # ── Preguntar número de operaciones simultáneas ──
        while True:
            try:
                resp = input("\n  ¿Cuántas operaciones simultáneas? (1-10): ").strip()
                n = int(resp)
                if 1 <= n <= 10:
                    break
                print("  ⚠️  Ingresa un número entre 1 y 10.")
            except ValueError:
                print("  ⚠️  Número inválido.")
        self._max_paralelo = n
        self._sem_paralelo = threading.Semaphore(n)
        # El pool se conectó con 10 slots; el semáforo limita cuántos usamos

        print("\n" + "═" * 62)
        print("  🚀 GOLD 4.0 v3 — Bot Activo")
        print(f"  💵 Monto base    : ${CONFIG['monto_base']}")
        print(f"  🔄 Martingala    : {'SÍ' if CONFIG['martingala_activa'] else 'NO'} "
              f"(x{CONFIG['multiplicador']}, máx {CONFIG['max_pasos']} pasos)")
        print(f"  🎯 Payout mínimo : {CONFIG['payout_minimo']}%")
        print(f"  ⏰ Rescaneo cada : {CONFIG['rescan_intervalo']} min")
        print(f"  📋 Cuenta        : {CONFIG['tipo_cuenta']}")
        print(f"  🔀 Simultáneas   : {n}")
        print(f"  ⏸️  Pausar búsqueda: Ctrl+Q  (las operaciones activas finalizan solas)")
        print("═" * 62)

        # Hilo daemon para capturar Ctrl+P
        hilo_kb = threading.Thread(target=self._escuchar_teclado, daemon=True)
        hilo_kb.start()

        try:
            self._coordinador()
        except KeyboardInterrupt:
            print("\n\n🛑 Bot detenido")
            self.mostrar_resumen_final()

    def _mostrar_tabla_ciclos(self):
        """Imprime tabla de ciclos completados hoy — una fila por ciclo."""
        hoy = datetime.now().strftime("%Y-%m-%d")
        filas = self.db.query(
            """SELECT id, activo, direccion, timestamp_inicio, pasos_usados,
                      monto_inicial, monto_maximo, ganancia_neta, resultado_final
               FROM ciclos
               WHERE resultado_final IS NOT NULL
                 AND resultado_final != 'cancelado'
                 AND timestamp_inicio LIKE ?
               ORDER BY id""",
            (f"{hoy}%",)
        )
        if not filas:
            return

        print(f"\n  {'─'*80}")
        print(f"  {'#':>4}  {'Hora':>8}  {'Activo':<22}  {'Dir':>4}  {'Pasos':>5}  {'Monto$':>7}  {'Neto$':>7}  {'Res':>4}")
        print(f"  {'─'*80}")
        for f in filas:
            hora  = (f.get("timestamp_inicio") or "")[:19][11:19]  # HH:MM:SS
            dir_s = (f.get("direccion") or "").upper()[:4]
            res   = f.get("resultado_final", "")
            icono = "✅" if res == "win" else ("💀" if res == "loss" else "──")
            neto  = f.get("ganancia_neta") or 0
            print(
                f"  {f['id']:>4}  {hora:>8}  {f['activo']:<22}  {dir_s:>4}  "
                f"{f.get('pasos_usados',1):>5}  "
                f"{f.get('monto_maximo',0):>7.2f}  "
                f"{neto:>+7.2f}  {icono}"
            )
        print(f"  {'─'*80}")

    def mostrar_resumen_final(self):
        """Muestra resumen al detener el bot"""
        stats = self.telemetria.stats_hoy()
        glob = self.telemetria.stats_global()

        print("\n" + "═" * 55)
        print("  📊 RESUMEN DE SESIÓN")
        print("═" * 55)
        print(f"  Operaciones hoy: {stats['total_operaciones']}")
        print(f"  Wins: {stats['wins']} | Losses: {stats['losses']}")
        print(f"  Win Rate: {stats['winrate']:.1f}%")
        print(f"  Ganancia hoy: ${stats['ganancia_total']:+.2f}")
        print(f"  Mejor racha: {stats['racha_max_win']} wins")
        print(f"  Peor racha: {stats['racha_max_loss']} losses")

        if stats.get("por_activo"):
            print(f"\n  Por activo:")
            for act, data in stats["por_activo"].items():
                wr = data['wins'] / max(data['wins'] + data['losses'], 1) * 100
                print(f"    {act}: {data['wins']}W/{data['losses']}L "
                      f"({wr:.0f}%) → ${data['ganancia']:+.2f}")

        if stats.get("por_paso_martingala"):
            print(f"\n  Por paso de martingala:")
            for paso, data in sorted(stats["por_paso_martingala"].items()):
                wr = data['wins'] / max(data['total'], 1) * 100
                print(f"    Paso {paso}: {data['wins']}W/{data['losses']}L ({wr:.0f}%)")

        print(f"\n  📁 Log guardado en: {CONFIG['log_file']}")
        print(f"  📁 Stats guardadas en: {CONFIG['stats_file']}")
        print("═" * 55)


# ═══════════════════════════════════════════════
# SERVIDOR WEB PARA DASHBOARD
# ═══════════════════════════════════════════════

def iniciar_servidor_dashboard(db: Database = None, bot=None):
    """Inicia un servidor HTTP para servir el dashboard"""
    port = CONFIG["dashboard_port"]

    def json_resp(handler, data):
        payload = json.dumps(data, default=str).encode()
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.end_headers()
            handler.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def handle_error(self, _request, _client_address):
            pass  # silencia desconexiones del navegador

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/api/stats":
                try:
                    stats = db.stats_live() if db else {}
                    # Agregar config del bot
                    stats["config"] = {
                        "monto_base":    CONFIG["monto_base"],
                        "martingala":    CONFIG["martingala_activa"],
                        "multiplicador": CONFIG["multiplicador"],
                        "max_pasos":     CONFIG["max_pasos"],
                        "payout_minimo": CONFIG["payout_minimo"],
                        "timeframe":     CONFIG["timeframe"],
                        "expiracion":    CONFIG["expiracion"],
                    }
                    # Balance actual desde el bot
                    if bot:
                        try:
                            stats["balance_actual"] = bot.api.get_balance()
                        except Exception:
                            pass
                    json_resp(self, stats)
                except Exception as e:
                    json_resp(self, {"error": str(e)})

            elif path == "/api/operaciones":
                try:
                    with open(CONFIG["log_file"], "r") as f:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(f.read().encode())
                except:
                    json_resp(self, [])

            elif path == "/api/db/operaciones" and db:
                activo = qs.get("activo", [None])[0]
                fecha  = qs.get("fecha",  [None])[0]
                limit  = int(qs.get("limit", [200])[0])
                sql = "SELECT * FROM operaciones WHERE 1=1"
                params = []
                if activo:
                    sql += " AND activo=?"; params.append(activo)
                if fecha:
                    sql += " AND timestamp LIKE ?"; params.append(f"{fecha}%")
                sql += " ORDER BY id DESC LIMIT ?"
                params.append(limit)
                json_resp(self, db.query(sql, params))

            elif path == "/api/db/ciclos" and db:
                activo = qs.get("activo", [None])[0]
                limit  = int(qs.get("limit", [100])[0])
                sql = "SELECT * FROM ciclos WHERE 1=1"
                params = []
                if activo:
                    sql += " AND activo=?"; params.append(activo)
                sql += " ORDER BY id DESC LIMIT ?"
                params.append(limit)
                json_resp(self, db.query(sql, params))

            elif path == "/api/db/escaneos" and db:
                limit = int(qs.get("limit", [50])[0])
                rows = db.query(
                    "SELECT * FROM escaneos ORDER BY id DESC LIMIT ?", (limit,)
                )
                for r in rows:
                    try:
                        r["activos_json"] = json.loads(r["activos_json"] or "[]")
                    except:
                        r["activos_json"] = []
                json_resp(self, rows)

            elif path == "/api/db/activos" and db:
                rows = db.query("""
                    SELECT par AS activo,
                           COUNT(*)                                          AS total_ops,
                           SUM(CASE WHEN resultado='win'  THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN resultado='loss' THEN 1 ELSE 0 END) AS losses,
                           SUM(CASE WHEN resultado='tie'  THEN 1 ELSE 0 END) AS ties,
                           ROUND(SUM(ganancia_neta), 2)                      AS ganancia_total,
                           ROUND(AVG(CASE WHEN resultado='win' THEN 1.0 ELSE 0.0 END)*100, 1) AS winrate,
                           MAX(timestamp)                                     AS ultima_op
                    FROM operaciones
                    GROUP BY par
                    ORDER BY ganancia_total DESC
                """)
                json_resp(self, rows)

            elif path == "/api/db/resumen" and db:
                ops = db.query("""
                    SELECT resultado, COUNT(*) as n, ROUND(SUM(ganancia_neta),2) as ganancia
                    FROM operaciones GROUP BY resultado
                """)
                ciclos = db.query("""
                    SELECT resultado_final, COUNT(*) as n,
                           ROUND(AVG(pasos_usados),1) as pasos_promedio,
                           ROUND(SUM(ganancia_neta),2) as ganancia_total
                    FROM ciclos WHERE resultado_final IS NOT NULL
                    GROUP BY resultado_final
                """)
                escaneos = db.query(
                    "SELECT COUNT(*) as total, MAX(timestamp) as ultimo FROM escaneos"
                )
                json_resp(self, {
                    "operaciones": ops,
                    "ciclos": ciclos,
                    "escaneos": escaneos[0] if escaneos else {}
                })

            elif path == "/api/scan-live" and bot:
                json_resp(self, bot._scan_progress)

            elif path == "/api/trades-vivos" and bot:
                with bot._lock:
                    data = list(bot._trades_vivos.values())
                json_resp(self, data)

            elif path == "/api/trades-recientes" and bot:
                with bot._lock:
                    data = list(bot._trades_recientes)
                json_resp(self, data)

            elif path == "/api/candles" and bot:
                activo = qs.get("activo", [None])[0]
                n      = int(qs.get("n", [60])[0])
                if activo:
                    with bot._lock:
                        cached = list(bot._candle_cache.get(activo, []))
                    if cached:
                        json_resp(self, cached[-n:])
                    else:
                        # caché vacío → fetch con API principal (solo display)
                        try:
                            raw = bot.api.get_candles(
                                activo, CONFIG["timeframe"], n, time.time()
                            )
                            if raw:
                                velas = sorted([{
                                    "time":  int(v["from"]),
                                    "open":  float(v["open"]),
                                    "high":  float(v["max"]),
                                    "low":   float(v["min"]),
                                    "close": float(v["close"]),
                                } for v in raw], key=lambda x: x["time"])
                                with bot._lock:
                                    bot._candle_cache[activo] = velas
                                json_resp(self, velas)
                            else:
                                json_resp(self, [])
                        except Exception:
                            json_resp(self, [])
                else:
                    json_resp(self, [])

            elif path == "/api/bot/status" and bot:
                stopped = bot._stop_event.is_set()
                with bot._lock:
                    trades = len(bot._trades_activos)
                json_resp(self, {"activo": not stopped, "trades_abiertos": trades})

            else:
                # Servir archivos HTML estáticos explícitamente
                base_dir = os.path.dirname(os.path.abspath(__file__))
                allowed = {
                    "/":              "dashboard.html",
                    "/dashboard.html":"dashboard.html",
                    "/chart.html":    "chart.html",
                }
                filename = allowed.get(path)
                if filename:
                    filepath = os.path.join(base_dir, filename)
                    try:
                        with open(filepath, "rb") as f:
                            body = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except FileNotFoundError:
                        self.send_response(404)
                        self.end_headers()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                else:
                    self.send_response(404)
                    self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/bot/stop" and bot:
                bot.detener()
                json_resp(self, {"ok": True, "mensaje": "Bot detenido"})
            else:
                self.send_response(404)
                self.end_headers()

    class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    with ThreadingServer(("", port), Handler) as httpd:
        print(f"🌐 Dashboard: http://localhost:{port}/dashboard.html")
        httpd.serve_forever()


# ═══════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════

_bot_global = None

def _handle_exit(_sig=None, _frame=None):
    print("\n\n🛑 Ctrl+C detectado — cerrando bot...")
    if _bot_global:
        try:
            _bot_global._stop_event.set()
        except Exception:
            pass
        # Cerrar en DB todos los ciclos que quedaron abiertos (resultado_final NULL)
        try:
            _bot_global.db._lock.acquire(timeout=3)
            with _bot_global.db._conn() as conn:
                abiertos = conn.execute(
                    "SELECT id FROM ciclos WHERE resultado_final IS NULL"
                ).fetchall()
                if abiertos:
                    ids = [r[0] for r in abiertos]
                    conn.executemany(
                        "UPDATE ciclos SET resultado_final='cancelado', timestamp_fin=? WHERE id=?",
                        [(datetime.now().isoformat(), cid) for cid in ids]
                    )
                    print(f"   💾 {len(ids)} ciclos sin cerrar marcados como 'cancelado'")
        except Exception as e:
            print(f"   ⚠️ Cleanup DB: {e}")
        finally:
            try: _bot_global.db._lock.release()
            except Exception: pass
        try:
            _bot_global.mostrar_resumen_final()
        except Exception:
            pass
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT,  _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    print("╔══════════════════════════════════════════════╗")
    print("║    ESTRATEGIA GOLD 4.0 v3 — IQ Option        ║")
    print("╚══════════════════════════════════════════════╝\n")
    print("  1. 🤖 Iniciar Bot + Dashboard")
    print("  2. 📊 Ver estadísticas guardadas")
    print("  3. 🌐 Solo Dashboard (sin bot)\n")

    opcion = input("Opción: ").strip()

    if opcion == "1":
        bot = GoldBot()
        _bot_global = bot
        if bot.conectar():
            dashboard_thread = threading.Thread(
                target=iniciar_servidor_dashboard,
                args=(bot.db, bot),
                daemon=True
            )
            dashboard_thread.start()
            print(f"\n🌐 Dashboard en: http://localhost:{CONFIG['dashboard_port']}/dashboard.html")
            print("  Presiona Ctrl+C para detener el bot\n")
            try:
                bot.iniciar()
            except KeyboardInterrupt:
                _handle_exit(None, None)

    elif opcion == "2":
        tel = Telemetria()
        stats_hoy = tel.stats_hoy()
        stats_global = tel.stats_global()
        print("\n📊 ESTADÍSTICAS DE HOY:")
        print(json.dumps(stats_hoy, indent=2, default=str))
        print("\n📊 ESTADÍSTICAS GLOBALES:")
        print(json.dumps(stats_global, indent=2, default=str))

    elif opcion == "3":
        print("Iniciando solo dashboard...")
        try:
            iniciar_servidor_dashboard()
        except KeyboardInterrupt:
            print("\n🛑 Dashboard detenido")
            sys.exit(0)

    else:
        print("Opción no válida.")
