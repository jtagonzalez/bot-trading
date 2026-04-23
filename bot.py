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
    "max_pasos": 6,                   # Máx entradas seguidas (1 original + 5 martingala)

    # --- Indicadores ---
    "sma_rapida": 3,
    "sma_tendencia": 50,

    # --- Filtros de la estrategia ---
    "porcentaje_cuerpo": 0.60,          # % mínimo del cuerpo sobre/bajo SMA3 (0.95 era demasiado estricto)
    "max_mecha_ratio": 2.0,             # mechas máx como múltiplo del cuerpo (2.0 = mechas < 200% cuerpo)
    "velas_inclinacion": 3,
    "separacion_minima": 0.0000,

    # --- Telemetría ---
    "log_file": "telemetria.json",
    "stats_file": "estadisticas.json",
    "db_file": "gold_bot.db",
    "dashboard_port": 8080,

    # --- Gestión de riesgo ---
    "max_perdidas_diarias": 10,       # Detener tras N pérdidas en el día
    "max_operaciones_dia": 50,        # Máx operaciones por día
    "ganancia_objetivo_dia": 50,      # Meta diaria en USD (0 = sin límite)

    # --- Observadores ---
    "rescan_intervalo": 15,           # Minutos entre rescaneos de payout
    "payout_minimo": 80,              # % mínimo para mantener observador activo
}


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

    df["COMPRAR"] = (
        df["tendencia_alcista"] & df["verde_encima_cian"] &
        df["lineas_separadas"] & df["es_alcista"] &
        (df["pct_encima_sma3"] >= umbral) & df["mechas_ok"]
    )
    df["VENDER"] = (
        df["tendencia_bajista"] & df["verde_debajo_cian"] &
        df["lineas_separadas"] & df["es_bajista"] &
        (df["pct_debajo_sma3"] >= umbral) & df["mechas_ok"]
    )

    return df


# ═══════════════════════════════════════════════
# TELEMETRÍA — Registro de operaciones
# ═══════════════════════════════════════════════

class Telemetria:
    def __init__(self):
        self.operaciones = []
        self.sesion_inicio = datetime.now().isoformat()
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
        """Guarda todo el historial a disco"""
        with open(CONFIG["log_file"], "w") as f:
            json.dump(self.operaciones, f, indent=2, default=str)

    def registrar(self, operacion: dict):
        """Registra una operación completa"""
        op = {
            "id": len(self.operaciones) + 1,
            "timestamp": datetime.now().isoformat(),
            "activo": operacion.get("activo", ""),
            "direccion": operacion.get("direccion", ""),
            "monto": operacion.get("monto", 0),
            "payout": operacion.get("payout", 0),
            "resultado": operacion.get("resultado", ""),     # "win", "loss", "tie"
            "ganancia": operacion.get("ganancia", 0),
            "paso_martingala": operacion.get("paso", 1),
            "balance_despues": operacion.get("balance", 0),
            # Datos del análisis
            "sma3": operacion.get("sma3", 0),
            "sma50": operacion.get("sma50", 0),
            "pct_cuerpo": operacion.get("pct_cuerpo", 0),
            "ratio_mechas": operacion.get("ratio_mechas", 0),
            "close": operacion.get("close", 0),
        }
        self.operaciones.append(op)
        self.guardar()
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
                    payout_inicial   REAL
                );

                CREATE TABLE IF NOT EXISTS operaciones (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ciclo_id         INTEGER REFERENCES ciclos(id),
                    timestamp        TEXT    NOT NULL,
                    activo           TEXT    NOT NULL,
                    direccion        TEXT,
                    monto            REAL,
                    payout           REAL,
                    resultado        TEXT,   -- win / loss / tie
                    ganancia         REAL,
                    paso_martingala  INTEGER DEFAULT 1,
                    balance_despues  REAL,
                    sma3             REAL,
                    sma50            REAL,
                    pct_cuerpo       REAL,
                    ratio_mechas     REAL,
                    close_price      REAL
                );
            """)

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

    def iniciar_ciclo(self, activo: str, direccion: str, payout: float, monto: float) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO ciclos (activo, direccion, timestamp_inicio, payout_inicial, monto_inicial, monto_maximo)
                   VALUES (?,?,?,?,?,?)""",
                (activo, direccion, datetime.now().isoformat(), payout, monto, monto)
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

    def registrar_operacion(self, ciclo_id: int, datos: dict):
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO operaciones
                   (ciclo_id, timestamp, activo, direccion, monto, payout, resultado,
                    ganancia, paso_martingala, balance_despues, sma3, sma50,
                    pct_cuerpo, ratio_mechas, close_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ciclo_id, datetime.now().isoformat(),
                 datos["activo"], datos["direccion"], datos["monto"], datos["payout"],
                 datos["resultado"], datos["ganancia"], datos["paso"],
                 datos["balance"], datos["sma3"], datos["sma50"],
                 datos["pct_cuerpo"], datos["ratio_mechas"], datos["close"])
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
            gan    = sum(r.get("ganancia",0) or 0 for r in rows)
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
                p = str(r.get("paso_martingala", 1) or 1)
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
        self._candles_lock = threading.Lock()  # solo para fallback HTTP
        self._candles_sem  = threading.Semaphore(5)  # máx 5 get_candles simultáneos
        self._trades_activos = set()
        self._stop_event = threading.Event()

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

            if califica:
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
        with self._candles_sem:          # máx 5 hilos simultáneos
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

    def _obtener_precio_actual(self, activo: str) -> float | None:
        """Obtiene el precio actual del activo (último close disponible)."""
        try:
            velas = self.api.get_candles(activo, CONFIG["timeframe"], 2, time.time())
            if velas:
                return float(velas[-1].get("close", 0) or velas[-1].get("close", 0))
        except Exception:
            pass
        return None

    def _obtener_resultado_orden(self, order_id, activo: str,
                                 precio_entrada: float, direccion: str,
                                 monto: float, payout: float) -> float:
        """
        Calcula WIN/LOSS comparando precio_entrada vs precio_cierre de la vela.
        No depende del API de resultado — cálculo local inmediato.

        CALL: WIN si close_final > precio_entrada
        PUT:  WIN si close_final < precio_entrada
        Empate (igual precio): TIE → devuelve 0.0

        Retorna: ganancia neta (positivo=WIN, 0=TIE, negativo=LOSS)
        """
        tf = CONFIG["timeframe"]
        ahora = time.time()

        # Calcular cuándo expira la opción (siguiente cierre de vela × expiracion)
        segs_hasta_cierre = tf - (ahora % tf)
        expira_en = segs_hasta_cierre + (CONFIG["expiracion"] - 1) * tf

        # — Countdown segundo a segundo —
        t_inicio = time.time()
        while True:
            if self._stop_event.is_set():
                break
            elapsed   = time.time() - t_inicio
            restante  = expira_en - elapsed
            if restante <= 0:
                break
            print(f"\r   ⏳ EN OPERACIÓN [{activo}] {direccion.upper()} "
                  f"entrada={precio_entrada:.5f} — vence en {restante:5.1f}s ...",
                  end="", flush=True)
            time.sleep(0.5)
        print()

        # — Obtener precio de cierre de la vela que acaba de cerrar —
        # Pequeño margen para que la vela esté disponible en el API
        time.sleep(1.5)
        precio_cierre = None
        for intento in range(1, 6):
            try:
                velas = self.api.get_candles(activo, tf, 3, time.time())
                if velas:
                    # Buscar la vela que acaba de cerrar (timestamp <= ahora)
                    ts_expira = t_inicio + expira_en
                    cerrada = None
                    for v in reversed(velas):
                        if v.get("from", 0) < ts_expira:
                            cerrada = v
                            break
                    if cerrada:
                        precio_cierre = float(cerrada.get("close", 0))
                        print(f"   🕯️  [{activo}] Vela cerrada: "
                              f"open={cerrada.get('open'):.5f} "
                              f"close={precio_cierre:.5f} "
                              f"(intento {intento})")
                        break
            except Exception as e:
                print(f"   ⚠️ [{activo}] Error obteniendo vela (intento {intento}): {e}")
            time.sleep(1)

        if precio_cierre is None or precio_cierre == 0:
            print(f"   ⚠️ [{activo}] No se pudo obtener precio cierre — asumiendo LOSS")
            return -monto

        # — Calcular resultado —
        diferencia = precio_cierre - precio_entrada
        if abs(diferencia) < 0.000001:   # precio idéntico = empate
            resultado = "TIE"
            ganancia  = 0.0
        elif (direccion == "call" and diferencia > 0) or \
             (direccion == "put"  and diferencia < 0):
            resultado = "WIN"
            ganancia  = round(monto * (payout / 100), 4)
        else:
            resultado = "LOSS"
            ganancia  = -monto

        signo = "+" if ganancia >= 0 else ""
        print(f"   📊 [{activo}] entrada={precio_entrada:.5f} → cierre={precio_cierre:.5f} "
              f"| diff={diferencia:+.5f} | {resultado} {signo}{ganancia:.4f}")
        return ganancia

    # ─────────────────────────────────────
    # EJECUTAR UNA OPERACIÓN INDIVIDUAL
    # ─────────────────────────────────────

    def _ejecutar_paso(self, activo: str, direccion: str, monto: float,
                       payout: float, paso: int, ciclo_id: int,
                       vela_data: dict) -> dict:
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
            status, order_raw = self.api.buy(monto, activo, direccion, CONFIG["expiracion"])
            if status:
                order_id = self._extraer_order_id(order_raw)
                print(f"   🛠️  [{activo}] buy() intento {intento_buy} → raw={order_raw!r} → id={order_id!r}")
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

        # Precio de entrada = precio actual en el momento de compra
        precio_entrada = self._obtener_precio_actual(activo) or vela_data.get("close", 0)
        print(f"   ✅ [{activo}] Orden #{order_id} colocada | precio entrada: {precio_entrada:.5f}")

        # — Esperar y calcular resultado por precio —
        resultado_valor = self._obtener_resultado_orden(
            order_id, activo, precio_entrada, direccion, monto, payout
        )

        balance = self.api.get_balance()

        if resultado_valor > 0:
            resultado = "win"
            print(f"   🏆 [{activo}] WIN +${resultado_valor:.4f} | Balance: ${balance:.2f}")
        elif resultado_valor == 0:
            resultado = "tie"
            print(f"   🟡 [{activo}] TIE (empate) | Balance: ${balance:.2f}")
        else:
            resultado = "loss"
            print(f"   💔 [{activo}] LOSS -${abs(resultado_valor):.2f} | Balance: ${balance:.2f}")

        # — Mover de vivos a recientes en dashboard —
        with self._lock:
            info = self._trades_vivos.pop(activo, {})
            info.update({
                "resultado": resultado,
                "ganancia": resultado_valor,
                "ts_fin": datetime.now().isoformat(),
                "balance_despues": balance,
            })
            self._trades_recientes.insert(0, info)
            self._trades_recientes[:] = self._trades_recientes[:20]

        # — Guardar en DB y telemetría —
        datos = {
            "activo": activo, "direccion": direccion, "monto": monto,
            "payout": payout, "resultado": resultado, "ganancia": resultado_valor,
            "paso": paso, "balance": balance,
            "sma3": vela_data.get("sma3", 0), "sma50": vela_data.get("sma50", 0),
            "pct_cuerpo": vela_data.get("pct_cuerpo", 0),
            "ratio_mechas": vela_data.get("ratio_mechas", 0),
            "close": vela_data.get("close", 0),
        }
        with self._lock:
            self.telemetria.registrar(datos)
            self.operaciones_hoy += 1
            self.ganancia_hoy += resultado_valor
            if resultado == "loss":
                self.perdidas_hoy += 1
        try:
            self.db.registrar_operacion(ciclo_id, datos)
            print(f"   💾 [{activo}] DB guardado — {resultado.upper()} ${resultado_valor:+.2f}")
        except Exception as e_db:
            print(f"   ⚠️ [{activo}] Error DB: {e_db}")

        return {
            "ejecutada": True, "resultado": resultado,
            "ganancia": resultado_valor, "balance": balance,
        }

    # ─────────────────────────────────────
    # CICLO COMPLETO: TRADE + MARTINGALA
    # ─────────────────────────────────────

    def _ciclo_trade(self, activo: str, direccion: str, vela_data: dict):
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

        ciclo_id = self.db.iniciar_ciclo(activo, direccion, payout, CONFIG["monto_base"])
        print(f"\n{'─'*55}")
        print(f"   🎯 CICLO INICIADO [{activo}] {direccion.upper()} | payout: {payout}% | ciclo_id: {ciclo_id}")
        print(f"{'─'*55}")

        ganancia_neta = 0.0
        monto_maximo  = CONFIG["monto_base"]
        resultado_final = "loss"
        paso_final = 1

        for paso in range(1, CONFIG["max_pasos"] + 1):
            puede, razon = self.verificar_limites()
            if not puede:
                print(f"   🛡️ [{activo}] Límite alcanzado: {razon} — ciclo cancelado")
                resultado_final = "cancelado"
                break

            monto = round(CONFIG["monto_base"] * (CONFIG["multiplicador"] ** (paso - 1)), 2)
            monto_maximo = max(monto_maximo, monto)
            paso_final   = paso

            if paso > 1:
                print(f"\n   🔄 [{activo}] MARTINGALA PASO {paso}/{CONFIG['max_pasos']} → ${monto:.2f} — ejecutando inmediatamente")

            res = self._ejecutar_paso(
                activo, direccion, monto, payout, paso, ciclo_id, vela_data
            )

            if not res["ejecutada"]:
                print(f"   ❌ [{activo}] Paso {paso} no ejecutado — ciclo terminado")
                resultado_final = "error"
                break

            ganancia_neta  += res["ganancia"]
            resultado_final = res["resultado"]

            print(f"   📈 [{activo}] Paso {paso} → {res['resultado'].upper()} | "
                  f"Neto acumulado: ${ganancia_neta:+.2f}")

            if res["resultado"] == "win":
                print(f"\n   ✅ [{activo}] CICLO GANADO en paso {paso} | Neto: ${ganancia_neta:+.2f}")
                break

            # LOSS o TIE: continuar martingala si está activa
            if not CONFIG["martingala_activa"]:
                print(f"   🔴 [{activo}] {res['resultado'].upper()} | Martingala OFF — ciclo terminado")
                break

            if paso == CONFIG["max_pasos"]:
                print(f"\n   💀 [{activo}] CICLO PERDIDO — {paso} pasos agotados | Neto: ${ganancia_neta:+.2f}")

        self.db.cerrar_ciclo(ciclo_id, resultado_final, ganancia_neta, paso_final, monto_maximo)
        print(f"   📁 [{activo}] Ciclo #{ciclo_id} cerrado — {resultado_final.upper()} | Neto: ${ganancia_neta:+.2f}")

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
        return " · ".join(motivo) if motivo else "condiciones mixtas"

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

            print(f"\n{'─'*62}")
            print(f"  👁️  ESCANEANDO SEÑALES  [{datetime.now().strftime('%H:%M:%S')}]  ({len(activos_scan)} activos)")
            print(f"{'─'*62}")

            # ── 4. Evaluar señal en cada activo (mayor payout primero) ──
            señal_encontrada = False
            for activo_info in activos_scan:
                activo = activo_info["nombre"]
                if self._stop_event.is_set():
                    break

                try:
                    df = self.obtener_velas(activo)
                    df = calcular_señales(df)
                    self._guardar_cache(activo, df)
                    vela = df.iloc[-2]   # última vela cerrada

                    vela_data = {
                        "sma3":        round(float(vela["sma_rapida"]),    5) if not pd.isna(vela["sma_rapida"])    else 0,
                        "sma50":       round(float(vela["sma_tendencia"]), 5) if not pd.isna(vela["sma_tendencia"]) else 0,
                        "pct_cuerpo":  round(float(max(vela["pct_encima_sma3"], vela["pct_debajo_sma3"])), 3),
                        "ratio_mechas":round(float(vela["suma_mechas"] / vela["cuerpo"]), 3) if vela["cuerpo"] > 0 else 0,
                        "close":       round(float(vela["close"]), 5),
                    }

                    if vela["COMPRAR"]:
                        print(f"\n  🟢 SEÑAL CALL → {activo} ({activo_info['payout']}%)")
                        self._ciclo_trade(activo, "call", vela_data)
                        señal_encontrada = True
                        # Después del ciclo → rescanear payout
                        ultimo_scan = 0
                        break

                    elif vela["VENDER"]:
                        print(f"\n  🔴 SEÑAL PUT  → {activo} ({activo_info['payout']}%)")
                        self._ciclo_trade(activo, "put", vela_data)
                        señal_encontrada = True
                        ultimo_scan = 0
                        break

                    else:
                        motivo = self._log_señal(activo, vela)
                        print(f"  ⬜ {activo:<26} sin señal  ({motivo})")

                except Exception as e:
                    print(f"  ⚠️ [{activo}] Error evaluando señal: {e}")

            if not señal_encontrada:
                print(f"\n  ⏸️  Sin señales esta vela — esperando próxima...")

            # Mostrar estadísticas de sesión
            stats = self.telemetria.stats_hoy()
            if stats["total_operaciones"] > 0:
                print(f"  📊 Sesión: {stats['wins']}W / {stats['losses']}L  |  "
                      f"WR: {stats['winrate']:.0f}%  |  "
                      f"P/L: ${stats['ganancia_total']:+.2f}")

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

        print("\n" + "═" * 62)
        print("  🚀 GOLD 4.0 v3 — Bot Activo")
        print(f"  💵 Monto base    : ${CONFIG['monto_base']}")
        print(f"  🔄 Martingala    : {'SÍ' if CONFIG['martingala_activa'] else 'NO'} "
              f"(x{CONFIG['multiplicador']}, máx {CONFIG['max_pasos']} pasos)")
        print(f"  🎯 Payout mínimo : {CONFIG['payout_minimo']}%")
        print(f"  ⏰ Rescaneo cada : {CONFIG['rescan_intervalo']} min")
        print(f"  📋 Cuenta        : {CONFIG['tipo_cuenta']}")
        print("═" * 62)

        try:
            self._coordinador()
        except KeyboardInterrupt:
            print("\n\n🛑 Bot detenido")
            self.mostrar_resumen_final()

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
                    SELECT activo,
                           COUNT(*)                                          AS total_ops,
                           SUM(CASE WHEN resultado='win'  THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN resultado='loss' THEN 1 ELSE 0 END) AS losses,
                           SUM(CASE WHEN resultado='tie'  THEN 1 ELSE 0 END) AS ties,
                           ROUND(AVG(payout), 1)                             AS payout_promedio,
                           ROUND(SUM(ganancia), 2)                           AS ganancia_total,
                           ROUND(AVG(CASE WHEN resultado='win' THEN 1.0 ELSE 0.0 END)*100, 1) AS winrate,
                           MAX(timestamp)                                    AS ultima_op
                    FROM operaciones
                    GROUP BY activo
                    ORDER BY ganancia_total DESC
                """)
                json_resp(self, rows)

            elif path == "/api/db/resumen" and db:
                ops = db.query("""
                    SELECT resultado, COUNT(*) as n, ROUND(SUM(ganancia),2) as ganancia
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
                        # caché vacío → fetch usando el semáforo compartido
                        try:
                            got = bot._candles_sem.acquire(timeout=15)
                            if not got:
                                json_resp(self, [])
                                return
                            try:
                                raw = bot.api.get_candles(
                                    activo, CONFIG["timeframe"], n, time.time()
                                )
                            finally:
                                bot._candles_sem.release()
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
