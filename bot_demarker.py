"""
BOT DEMARKER — valida en PRACTICE la regla encontrada en esta conversacion:
DeMarker(periodo) sale de la banda [sobreventa, sobrecompra] por MAS de
min_velas seguidas y reingresa -> opera a favor del reingreso.

Validado en backtest SOLO para cripto (BTCUSD/ETHUSD): 62.07% de acierto,
IC95 [54.0, 69.6], periodo 17, niveles 80/20, min_velas>17. En forex el
mismo patron no funciona (ver ESTRATEGIA_GOLD4_BOLLINGER.md seccion 13-14).

Por eso este bot, igual que bot_validacion.py con EURUSD, EVALUA los 12
activos (10 forex + 2 cripto) para seguir acumulando datos, pero OPERA
dinero real solo en los que si tienen ventaja demostrada (--solo-operar,
por defecto BTCUSD-op,ETHUSD-op).

Es el mismo esqueleto de seguridad que bot_validacion.py (conexion con
timeout, vigilante que reinicia si se cuelga, catalogo de suspendidos,
registro completo en live.db) — lo unico que cambia es como se decide la
señal: una regla directa sobre el DeMarker, no un modelo entrenado.

MARTINGALA: NINGUNA, a proposito. La ventaja vive solo en la vela del
reingreso; las velas siguientes no tienen ninguna ventaja, y encadenar
apuestas ahi (como se probo en el backtest) arruina el resultado. Una sola
apuesta de monto fijo por señal.

Uso:
    py bot_demarker.py --probar      # sin conectar: valida la señal con velas historicas
    py bot_demarker.py --seco        # conecta y evalua, pero NO opera
    py bot_demarker.py               # opera en PRACTICE (BTCUSD-op, ETHUSD-op)
"""
import argparse
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from registro import Registro

VELAS_CONTEXTO = 220
ESPERA_RESULTADO = 2.5
ESPERA_DECISION = 45
TF = 60                        # 1 minuto, fijo: la regla se valido asi

CRIPTO_OP = {"BTCUSD-op", "ETHUSD-op"}
ACTIVOS_DEFECTO = ("EURUSD-op,GBPUSD-op,USDJPY-op,AUDUSD-op,USDCAD-op,NZDUSD-op,"
                   "EURJPY-op,GBPJPY-op,EURGBP-op,USDCHF-op,BTCUSD-op,ETHUSD-op")

_candles_lock = threading.Lock()
CANDLES_TIMEOUT = 25


class TiempoAgotado(RuntimeError):
    """La API no contesto. No es un fallo de red: es un hilo que no vuelve."""


def _lock_de_velas():
    return _candles_lock


def reiniciar_lock_velas():
    global _candles_lock
    _candles_lock = threading.Lock()


def get_candles_seguro(api, activo, tf, n, hasta=None):
    """Igual que en bot_validacion.py: get_candles serializado y CON TIMEOUT.
    Ver ahi la explicacion completa de por que hace falta."""
    lock = _lock_de_velas()
    if not lock.acquire(timeout=CANDLES_TIMEOUT):
        raise TiempoAgotado(f"lock de velas ocupado >{CANDLES_TIMEOUT}s ({activo})")
    caja = {}

    def _pedir():
        try:
            caja["v"] = api.get_candles(activo, tf, n, hasta if hasta is not None else time.time())
        except Exception as e:
            caja["e"] = e
        finally:
            lock.release()

    h = threading.Thread(target=_pedir, daemon=True)
    h.start()
    h.join(CANDLES_TIMEOUT)
    if h.is_alive():
        raise TiempoAgotado(f"get_candles sin respuesta en {CANDLES_TIMEOUT}s ({activo})")
    if "e" in caja:
        raise caja["e"]
    return caja.get("v")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ─────────────────────── señal (lo unico que cambia respecto a bot_validacion.py) ───────────────────────

def demarker(h, l, n):
    demax = (h - h.shift(1)).clip(lower=0)
    demin = (l.shift(1) - l).clip(lower=0)
    ma_max, ma_min = demax.rolling(n).mean(), demin.rolling(n).mean()
    return ma_max / (ma_max + ma_min).replace(0, np.nan)


def racha(serie_bool):
    grupo = (serie_bool != serie_bool.shift()).cumsum()
    r = serie_bool.groupby(grupo).cumcount() + 1
    return r.where(serie_bool, 0)


def calcular_senal(velas, periodo, sobrecompra, sobreventa, min_velas):
    """Evalua la señal en la ULTIMA vela cerrada. Devuelve
    (direccion_o_None, df, dm_actual, racha_previa)."""
    df = pd.DataFrame(velas)
    df = df.rename(columns={"from": "ts", "max": "high", "min": "low"})
    df = df[["ts", "open", "high", "low", "close"]].astype(
        {"ts": "int64", "open": float, "high": float, "low": float, "close": float})
    df = df.sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df.ts, unit="s", utc=True)
    df["hora"] = df.dt.dt.hour
    df["dow"] = df.dt.dt.dayofweek

    if len(df) < periodo + min_velas + 10:
        return None, df, None, 0

    dm = demarker(df.high, df.low, periodo) * 100
    fuera_abajo = dm < sobreventa
    fuera_arriba = dm > sobrecompra
    racha_abajo = racha(fuera_abajo)
    racha_arriba = racha(fuera_arriba)
    reing_abajo = fuera_abajo.shift(1, fill_value=False) & ~fuera_abajo
    reing_arriba = fuera_arriba.shift(1, fill_value=False) & ~fuera_arriba

    i = len(df) - 1
    direccion, r_prev = None, 0
    if bool(reing_abajo.iloc[i]) and racha_abajo.iloc[i - 1] > min_velas:
        direccion, r_prev = "call", int(racha_abajo.iloc[i - 1])
    elif bool(reing_arriba.iloc[i]) and racha_arriba.iloc[i - 1] > min_velas:
        direccion, r_prev = "put", int(racha_arriba.iloc[i - 1])
    dm_actual = None if pd.isna(dm.iloc[i]) else round(float(dm.iloc[i]), 2)
    return direccion, df, dm_actual, r_prev


# ─────────────────────── bot ───────────────────────

class BotDemarker:
    def __init__(self, args):
        self.args = args
        self.periodo, self.sobrecompra, self.sobreventa = args.periodo, args.sobrecompra, args.sobreventa
        self.min_velas = args.min_velas
        self.tf = TF
        self.tipo_opcion = "turbo"           # tf=60s siempre compra turbo
        self.reg = Registro(args.db)
        self.api = None
        self.parar = threading.Event()
        self.abiertas = threading.Semaphore(args.max_simultaneas)
        self._lock = threading.Lock()
        self.pnl_dia = 0.0
        self.n_ops = 0
        self.n_pred = 0
        self.ultima_vela = {}
        self.estado_activos = {}
        self.fallos_velas = 0
        self.solo_operar = (set(a.strip() for a in args.solo_operar.split(",") if a.strip())
                            if args.solo_operar else None)
        self.latido = time.time()

    # ── conexion ──
    def conectar(self):
        from iqoptionapi.stable_api import IQ_Option
        self.api = IQ_Option(self.args.email, self.args.password)
        for intento in range(1, 6):
            try:
                self.api.connect()
            except Exception as e:
                # la libreria de IQ Option no maneja bien sus propios
                # timeouts de red (ConnectTimeout sin .cookies/.text revienta
                # connect() entero) -- hay que atajarlo aca, si no el proceso
                # se cae de una con cualquier blip transitorio de red.
                log(f"conectar intento {intento}/5: excepcion ({e})")
                time.sleep(5 * intento)
                continue
            for _ in range(30):
                if self.api.check_connect():
                    break
                time.sleep(1)
            if self.api.check_connect():
                break
            log(f"conectar intento {intento}/5: no quedo conectado, reintentando")
            time.sleep(5 * intento)
        if not self.api.check_connect():
            raise SystemExit("no se pudo conectar a IQ Option tras 5 intentos")
        modo = "REAL" if self.args.permitir_real else "PRACTICE"
        self.api.change_balance(modo)
        log(f"conectado ({modo})  balance ${self.api.get_balance():.2f}")
        self.refrescar_catalogo(inicial=True)

    def refrescar_catalogo(self, inicial=False):
        try:
            init = self.api.get_all_init_v2() or {}
        except Exception as e:
            if inicial:
                log(f"no se pudo leer init_v2: {e}")
            return
        import iqoptionapi.constants as OP
        estado, nuevos = {}, 0
        for tipo in ("turbo", "binary"):
            for k, v in ((init.get(tipo) or {}).get("actives") or {}).items():
                nom = (v.get("name") or "").split(".")[-1]
                if not nom:
                    continue
                if nom not in OP.ACTIVES:
                    OP.ACTIVES[nom] = int(k)
                    nuevos += 1
                estado.setdefault(nom, {})[tipo] = (
                    bool(v.get("enabled")), bool(v.get("is_suspended")))
        with self._lock:
            self.estado_activos = estado
        if inicial:
            log(f"catalogo: {len(estado)} activos | {nuevos} ids inyectados")

    def vigilante(self):
        limite = self.args.watchdog_seg
        while not self.parar.is_set():
            self.parar.wait(30)
            if self.parar.is_set():
                return
            quieto = time.time() - self.latido
            if quieto > limite:
                log(f"VIGILANTE: {quieto:.0f}s sin completar un ciclo "
                    f"(limite {limite:.0f}s); saliendo con codigo 3 para que el supervisor lo reinicie.")
                os._exit(3)

    def suspendido(self, activo):
        d = (self.estado_activos or {}).get(activo)
        if not d:
            return None
        est = d.get(self.tipo_opcion)
        if est is None:
            return None
        habilitado, susp = est
        return susp or not habilitado

    def reconectar(self):
        log("reconectando...")
        for intento in range(1, 6):
            try:
                self.api.connect()
                time.sleep(2)
                if self.api.check_connect():
                    self.api.change_balance("REAL" if self.args.permitir_real else "PRACTICE")
                    log("reconectado")
                    return True
            except Exception as e:
                log(f"  intento {intento}: {e}")
            time.sleep(5 * intento)
        return False

    # ── utilidades ──
    def operable(self, activo, hora_utc, dow):
        """Cripto: 24x7. Forex: 01-17h UTC, lunes a viernes (la ventana en
        que se valido la regla y en la que la sesion -op acepta ordenes)."""
        if activo in CRIPTO_OP:
            return True
        return 1 <= hora_utc <= 17 and dow < 5

    def payout_de(self, activo):
        for intento in range(3):
            try:
                p = self.api.get_all_profit().get(activo, {})
                if isinstance(p, dict):
                    v = round(100 * (p.get(self.tipo_opcion, 0) or 0), 1)
                    if v <= 0:
                        v = round(100 * max(p.get("binary", 0) or 0, p.get("turbo", 0) or 0), 1)
                    if v > 0:
                        return v
            except Exception:
                pass
            if intento < 2:
                time.sleep(1.5)
        return None

    # ── ciclo por activo ──
    def procesar(self, activo):
        try:
            velas = get_candles_seguro(self.api, activo, self.tf, VELAS_CONTEXTO)
            with self._lock:
                self.fallos_velas = 0
        except TiempoAgotado as e:
            with self._lock:
                self.fallos_velas += 1
            log(f"  {activo}: {e}")
            return
        except Exception as e:
            log(f"  {activo}: get_candles fallo ({e})")
            return
        if not velas or len(velas) < 120:
            return

        try:
            self.reg.resolver_virtuales(activo, self.tf, velas)
        except Exception as e:
            log(f"  {activo}: no se pudo resolver el historico ({e})")

        velas = sorted(velas, key=lambda v: v["from"])
        ahora = time.time()
        if velas[-1]["from"] + self.tf > ahora:
            velas = velas[:-1]
        if not velas:
            return
        ts_vela = int(velas[-1]["from"])

        with self._lock:
            if self.ultima_vela.get(activo) == ts_vela:
                return
            self.ultima_vela[activo] = ts_vela

        t_cierre = ts_vela + self.tf
        antiguedad = time.time() - t_cierre
        if antiguedad > self.args.max_antiguedad_seg:
            log(f"  {activo}: vela de hace {antiguedad:.0f}s, demasiado rancia; se omite")
            return

        direccion, df, dm_actual, racha_prev = calcular_senal(
            velas, self.periodo, self.sobrecompra, self.sobreventa, self.min_velas)

        # guardia de precio incoherente (contaminacion entre hilos)
        cierres = df.close.tail(50)
        if len(cierres) > 10:
            med = float(cierres.median())
            ult = float(df.close.iloc[-1])
            if med > 0 and not (0.5 < ult / med < 2.0):
                log(f"  {activo}: precio incoherente ({ult} vs mediana {med}); se descarta")
                with self._lock:
                    self.ultima_vela.pop(activo, None)
                return

        if direccion is None:
            return                      # sin señal en esta vela: nada que registrar

        fila = df.iloc[-1]
        hora_utc, dow, cierre = int(fila["hora"]), int(fila["dow"]), float(fila["close"])

        motivo = None
        if not self.operable(activo, hora_utc, dow):
            motivo = "fuera_horario"
        elif self.args.seco:
            motivo = "modo_seco"
        elif self.pnl_dia <= -abs(self.args.max_perdida_dia):
            motivo = "limite_perdida_dia"
        if motivo is None and self.solo_operar and activo not in self.solo_operar:
            motivo = "no_seleccionado"
        if motivo is None and self.suspendido(activo):
            motivo = "activo_suspendido"

        payout = self.payout_de(activo) if motivo is None else None
        if motivo is None:
            if payout is None:
                if self.args.payout_asumido:
                    payout = self.args.payout_asumido
                else:
                    motivo = "payout_desconocido"
            if motivo is None and payout < self.args.payout_minimo:
                motivo = "payout_bajo"

        opera = motivo is None
        if opera and not self.abiertas.acquire(blocking=False):
            motivo, opera = "limite_simultaneas", False

        ventana = df.tail(20)[["open", "high", "low", "close"]].values.round(6).tolist()
        pid = self.reg.registrar_prediccion(
            ts_vela=ts_vela, activo=activo, timeframe=self.tf,
            prob=(0.99 if direccion == "call" else 0.01), umbral=0.5,
            direccion=direccion, operada=opera, motivo_no_operada=motivo,
            cierre_vela=cierre, ventana_ohlc=ventana,
            indicadores={"demarker": dm_actual, "racha_previa": racha_prev,
                        "periodo": self.periodo, "min_velas": self.min_velas},
            payout=payout, hora_utc=hora_utc, dia_semana=dow)
        self.n_pred += 1
        log(f"  {activo} {direccion.upper()} DM={dm_actual} racha_previa={racha_prev} "
            f"{'' if opera else f'(no opera: {motivo})'}")

        if not opera:
            return
        try:
            self.operar(pid, activo, direccion, cierre, payout, t_cierre, ts_vela)
        finally:
            self.abiertas.release()

    def operar(self, pid, activo, direccion, cierre_vela, payout, t_cierre, ts_vela):
        """Monto fijo, SIN martingala: una sola apuesta por señal."""
        monto = self.args.monto
        t0 = time.time()
        try:
            ok, detalle = self.api.buy(monto, activo, direccion, 1)   # 1 min, turbo
        except Exception as e:
            self.reg.registrar_rechazo(pid, motivo=f"excepcion en buy: {e}")
            log(f"  {activo}: buy fallo ({e})")
            return
        if not ok:
            motivo = str(detalle) if detalle else "buy devolvio False sin mensaje"
            self.reg.registrar_rechazo(pid, motivo=motivo)
            log(f"  {activo}: orden RECHAZADA -> {motivo}")
            return
        order_id = detalle

        latencia = int((time.time() - t_cierre) * 1000)
        precio_entrada = cierre_vela
        try:
            v = get_candles_seguro(self.api, activo, self.tf, 1)
            if v:
                precio_entrada = float(v[-1]["open"])
        except Exception:
            pass

        self.reg.registrar_entrada(pid, order_id=order_id, precio_entrada=precio_entrada,
                                   monto=monto, payout_contratado=(payout or 0) / 100,
                                   latencia_ms=latencia)
        self.n_ops += 1
        log(f"  {activo} {direccion.upper()} ${monto} orden={order_id} "
            f"latencia={latencia}ms entrada={precio_entrada:.5f}")

        ganancia, resultado = self.esperar_resultado(order_id, activo)
        precio_salida, vela_res = self.vela_de_resultado(activo, ts_vela)
        if resultado is None:
            resultado = self.deducir(direccion, precio_entrada, precio_salida)
            ganancia = (monto * (payout or 84) / 100 if resultado == "win"
                        else (0.0 if resultado == "tie" else -monto))

        self.reg.registrar_salida(pid, precio_salida=precio_salida, resultado=resultado,
                                  ganancia=ganancia, vela_resultado=vela_res)
        with self._lock:
            self.pnl_dia += ganancia or 0.0
        icono = {"win": "GANA", "loss": "PIERDE", "tie": "EMPATE"}.get(resultado, resultado)
        log(f"  {activo} -> {icono} {ganancia:+.2f}  salida={precio_salida}  "
            f"| dia: {self.pnl_dia:+.2f} en {self.n_ops} ops")

    def esperar_resultado(self, order_id, activo):
        limite = time.time() + self.tf * ESPERA_RESULTADO
        while time.time() < limite and not self.parar.is_set():
            try:
                raw = self.api.api.socket_option_closed.get(order_id)
                if raw:
                    msg = raw.get("msg", {}) if isinstance(raw, dict) else {}
                    w = str(msg.get("win", "")).lower()
                    if w == "win":
                        return round(float(msg.get("win_amount", 0)) - float(msg.get("sum", 0)), 4), "win"
                    if w == "equal":
                        return 0.0, "tie"
                    if w:
                        return -float(msg.get("sum", self.args.monto)), "loss"
            except Exception:
                pass
            time.sleep(0.5)
        return None, None

    def vela_de_resultado(self, activo, ts_vela):
        objetivo = ts_vela + self.tf
        for _ in range(6):
            try:
                velas = get_candles_seguro(self.api, activo, self.tf, 5)
                for v in velas or []:
                    if int(v["from"]) == objetivo:
                        return float(v["close"]), {
                            "o": float(v["open"]), "h": float(v["max"]),
                            "l": float(v["min"]), "c": float(v["close"])}
            except Exception:
                pass
            time.sleep(2)
        return None, None

    @staticmethod
    def deducir(direccion, entrada, salida):
        if salida is None or entrada is None:
            return "error"
        if salida == entrada:
            return "tie"
        subio = salida > entrada
        return "win" if (subio == (direccion == "call")) else "loss"

    # ── bucle principal ──
    def correr(self):
        activos = self.args.activos.split(",") if self.args.activos else ACTIVOS_DEFECTO.split(",")
        log(f"DeMarker({self.periodo}) niveles {self.sobrecompra}/{self.sobreventa} "
            f"min_velas>{self.min_velas} | tf 1 min")
        log(f"activos evaluados: {', '.join(activos)}")
        if self.solo_operar:
            resto = [a for a in activos if a not in self.solo_operar]
            log(f"se OPERA en: {', '.join(sorted(self.solo_operar))}")
            log(f"solo se evalua (sin dinero): {', '.join(resto)}")
        log(f"monto ${self.args.monto} fijo, SIN martingala | max simultaneas {self.args.max_simultaneas} | "
            f"{'MODO SECO (no opera)' if self.args.seco else 'OPERANDO'}")

        huerfanas = self.reg.cerrar_huerfanas(self.tf)
        if huerfanas:
            log(f"{huerfanas} operacion(es) de runs anteriores vencieron sin resultado; marcadas como error")

        self.reg.iniciar_run(
            tipo_cuenta="REAL" if self.args.permitir_real else "PRACTICE",
            modelo_nombre=f"demarker{self.periodo}_{int(self.sobrecompra)}{int(self.sobreventa)}",
            modelo_hash=f"regla_min{self.min_velas}",
            modelo_entrenado="2026-09-30 (backtest 1m, 51-61 dias)",
            umbral=0.5, timeframe=self.tf, horas="todas", monto=self.args.monto,
            wr_esperado=self.args.wr_esperado or 62.07,
            config=vars(self.args), notas=self.args.notas, reanudar=self.args.reanudar)

        threading.Thread(target=self.vigilante, daemon=True).start()

        while not self.parar.is_set():
            try:
                if not self.api.check_connect() and not self.reconectar():
                    log("sin conexion; reintentando en 60s")
                    self.parar.wait(60)
                    continue
                self.latido = time.time()
                ahora = datetime.now(timezone.utc)
                if ahora.weekday() >= 5 and not any(a in CRIPTO_OP for a in activos):
                    log("fin de semana y no hay cripto en la lista; esperando 30 min")
                    self.parar.wait(1800)
                    continue

                self.refrescar_catalogo()

                hilos = []
                for a in activos:
                    if self.parar.is_set():
                        break
                    t = threading.Thread(target=self.procesar, args=(a,), daemon=True)
                    t.start()
                    hilos.append(t)
                    time.sleep(0.4)
                for t in hilos:
                    t.join(timeout=ESPERA_DECISION)

                if self.fallos_velas >= 3:
                    log(f"{self.fallos_velas} timeouts de velas seguidos; reconectando")
                    reiniciar_lock_velas()
                    self.fallos_velas = 0
                    self.reconectar()

                self.latido = time.time()
                espera = self.tf - (time.time() % self.tf) + 3
                self.parar.wait(max(5, espera))
            except Exception as e:
                log(f"error en el bucle: {e}")
                self.parar.wait(30)

        self.reg.cerrar_run()
        log(f"detenido. {self.n_pred} evaluaciones, {self.n_ops} operaciones, P&L {self.pnl_dia:+.2f}")


def probar(args):
    """Comprueba la señal con velas historicas, sin conectar a IQ Option."""
    import sqlite3
    print(f"Comprobando señal DeMarker({args.periodo}) {args.sobrecompra}/{args.sobreventa} "
          f"min_velas>{args.min_velas}...")
    c = sqlite3.connect("velas.db")
    activo_prueba = "BTCUSD"
    df = pd.read_sql("SELECT ts,open,high,low,close FROM velas WHERE activo=? AND timeframe=60 "
                     "ORDER BY ts DESC LIMIT ?", c, params=(activo_prueba, VELAS_CONTEXTO))
    df = df.sort_values("ts")
    velas = [{"from": int(r.ts), "open": r.open, "max": r.high, "min": r.low, "close": r.close}
             for r in df.itertuples()]
    direccion, dfc, dm, racha_prev = calcular_senal(velas, args.periodo, args.sobrecompra,
                                                     args.sobreventa, args.min_velas)
    print(f"  ultima vela de {activo_prueba}: {dfc.dt.iloc[-1]}  DeMarker={dm}  racha_previa={racha_prev}")
    print(f"  señal ahora mismo: {direccion or 'ninguna'}")
    print("\nComprobando registro...")
    reg = Registro(args.db)
    print(f"  esquema listo en {args.db}")
    print("\nTodo correcto. Para arrancar de verdad:")
    print("  py bot_demarker.py --seco      (evalua y registra, sin operar)")
    print("  py bot_demarker.py             (opera en PRACTICE, BTCUSD-op/ETHUSD-op)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="live.db")
    ap.add_argument("--periodo", type=int, default=17)
    ap.add_argument("--sobrecompra", type=float, default=80)
    ap.add_argument("--sobreventa", type=float, default=20)
    ap.add_argument("--min-velas", type=int, default=17)
    ap.add_argument("--activos", default=None, help=f"por defecto: {ACTIVOS_DEFECTO}")
    ap.add_argument("--solo-operar", default="BTCUSD-op,ETHUSD-op", metavar="LISTA",
                    help="activos en los que SI se opera; el resto solo se evalua")
    ap.add_argument("--monto", type=float, default=1.0)
    ap.add_argument("--max-simultaneas", type=int, default=2)
    ap.add_argument("--max-perdida-dia", type=float, default=20.0)
    ap.add_argument("--payout-minimo", type=float, default=75.0)
    ap.add_argument("--max-antiguedad-seg", type=float, default=25.0,
                    help="no operar velas que cerraron hace mas de N segundos (velas de 1 min)")
    ap.add_argument("--payout-asumido", type=float, default=86.0,
                    help="payout a usar si la API no lo publica (turbo 1 min ~86%)")
    ap.add_argument("--wr-esperado", type=float, default=None)
    ap.add_argument("--watchdog-seg", type=float, default=600.0)
    ap.add_argument("--reanudar", action="store_true")
    ap.add_argument("--seco", action="store_true")
    ap.add_argument("--probar", action="store_true")
    ap.add_argument("--permitir-real", action="store_true")
    ap.add_argument("--email", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--notas", default="")
    args = ap.parse_args()

    if args.probar:
        probar(args)
        return

    if not args.email or not args.password:
        import io, re
        try:
            src = io.open("bot_indicadores.py", encoding="utf-8").read()
            args.email = args.email or re.search(r'"email":\s*"([^"]+)"', src).group(1)
            args.password = args.password or re.search(r'"password":\s*"([^"]+)"', src).group(1)
        except Exception:
            raise SystemExit("faltan credenciales: usa --email y --password")

    if args.permitir_real:
        print("\n" + "!" * 70)
        print("  Vas a operar con DINERO REAL.")
        print("  El edge NO esta confirmado en vivo. Esto no es lo recomendado.")
        print("!" * 70)
        if input("\n  Escribe 'ENTIENDO EL RIESGO' para continuar: ") != "ENTIENDO EL RIESGO":
            raise SystemExit("cancelado")

    bot = BotDemarker(args)

    def parar(_s, _f):
        log("deteniendo (Ctrl+C)...")
        bot.parar.set()
    signal.signal(signal.SIGINT, parar)

    bot.conectar()
    bot.correr()


if __name__ == "__main__":
    main()
