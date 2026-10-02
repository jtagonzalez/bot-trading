"""
BOT DE VALIDACION — comprueba en PRACTICE si el edge del backtest aparece en vivo.

Hace una sola cosa y la hace entera: en cada cierre de vela evalua el modelo,
opera las senales de mayor confianza, y REGISTRA TODO en live.db (incluidas las
senales que descarta y el precio de salida de cada operacion).

No toca bot_indicadores.py. Es deliberado: aquel arrastra la estrategia vieja,
la martingala y el dashboard. Aqui solo esta lo que hace falta para responder
"?el 59.67% aparece en vivo?".

Seguridad:
  * solo opera en PRACTICE; se niega a arrancar en REAL salvo --permitir-real
  * sin martingala: un monto fijo por senal
  * limite de operaciones simultaneas y de perdida diaria

Uso:
    py bot_validacion.py --probar          # sin conectar: valida modelo y registro
    py bot_validacion.py --seco            # conecta y evalua, pero NO opera
    py bot_validacion.py                   # opera en PRACTICE
    py bot_validacion.py --horas todas --monto 1
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import simulador as S
from registro import Registro

VELAS_CONTEXTO = 220          # velas que se piden para calcular los indicadores
ESPERA_RESULTADO = 2.5        # multiplicador del timeframe para esperar el cierre
ESPERA_DECISION = 45          # lo que esperamos a que un activo termine de decidir

# Lock global que serializa TODAS las llamadas a get_candles del proceso.
# iqoptionapi usa un buffer de clase COMPARTIDO: dos hilos pidiendo velas a la
# vez se contaminan entre si y uno recibe los datos del otro. Detectado en la
# primera corrida seca — USDJPY quedo registrado con el precio de USDCHF
# (0.82314, cuando USDJPY cotiza sobre 147).
_candles_lock = threading.Lock()

# Cuanto esperamos una respuesta de velas antes de darla por perdida.
CANDLES_TIMEOUT = 25


class TiempoAgotado(RuntimeError):
    """La API no contesto. No es un fallo de red: es un hilo que no vuelve."""


def _lock_de_velas():
    return _candles_lock


def reiniciar_lock_velas():
    """Descarta el lock actual y crea uno nuevo.

    Solo tiene sentido cuando un hilo se quedo colgado DENTRO del lock y ya
    nunca lo soltara. El hilo zombi liberara el objeto viejo, que para
    entonces ya no usa nadie.
    """
    global _candles_lock
    _candles_lock = threading.Lock()


def get_candles_seguro(api, activo, tf, n, hasta=None):
    """get_candles serializado y CON TIMEOUT.

    El timeout no es una precaucion teorica. iqoptionapi espera la respuesta
    asi:

        while self.api.candles.candles_data is None:
            time.sleep(0.1)

    Sin salida. Si esa respuesta no llega nunca, el hilo se queda dentro del
    lock global y bloquea a todos los demas: el bot sigue vivo, con 0% de CPU,
    pero deja de evaluar velas. Medido el 22/09: 30 minutos sin una sola
    prediccion, proceso arriba, sin un solo error en el log.

    Por eso la llamada va en un hilo aparte del que si se puede desistir.
    """
    lock = _lock_de_velas()
    if not lock.acquire(timeout=CANDLES_TIMEOUT):
        raise TiempoAgotado(f"lock de velas ocupado >{CANDLES_TIMEOUT}s ({activo})")

    caja = {}

    def _pedir():
        try:
            caja["v"] = api.get_candles(
                activo, tf, n, hasta if hasta is not None else time.time())
        except Exception as e:
            caja["e"] = e
        finally:
            lock.release()

    h = threading.Thread(target=_pedir, daemon=True)
    h.start()
    h.join(CANDLES_TIMEOUT)
    if h.is_alive():
        # No liberamos el lock aqui a proposito: el hilo colgado todavia puede
        # escribir en el buffer compartido, y soltarlo dejaria que otra
        # peticion se mezclara con la suya. Quien decide abandonarlo es el
        # bucle principal, llamando a reiniciar_lock_velas().
        raise TiempoAgotado(f"get_candles sin respuesta en {CANDLES_TIMEOUT}s ({activo})")
    if "e" in caja:
        raise caja["e"]
    return caja.get("v")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ─────────────────────── modelo ───────────────────────

def cargar_modelo(ruta):
    import joblib
    d = joblib.load(ruta)
    return d["modelo"], d["medianas"], d["meta"]


def features_de_velas(velas, feats, medianas):
    """Calcula los features de la ULTIMA vela cerrada, igual que en entrenamiento."""
    df = pd.DataFrame(velas)
    df = df.rename(columns={"from": "ts", "max": "high", "min": "low"})
    df = df[["ts", "open", "high", "low", "close"]].astype(
        {"ts": "int64", "open": float, "high": float, "low": float, "close": float})
    df = df.sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df.ts, unit="s", utc=True)
    df["hora"] = df.dt.dt.hour
    df["dow"] = df.dt.dt.dayofweek
    df = S.indicadores(df)
    c = df.close
    for k in (1, 2, 3, 5, 10, 20):
        df[f"ret{k}"] = c.pct_change(k) * 100
    df["vol20"] = c.pct_change().rolling(20).std() * 100
    df["vol_rel"] = df.vol20 / df.vol20.rolling(100).mean()
    df["rango_rel"] = (df.high - df.low) / c * 100
    df["cierre_en_rango"] = (c - df.low) / (df.high - df.low).replace(0, np.nan)

    fila = df.iloc[-1]
    x = pd.Series({f: fila.get(f, np.nan) for f in feats})
    x = x.replace([np.inf, -np.inf], np.nan)
    faltan = int(x.isna().sum())
    x = x.fillna(medianas)
    return x.values.reshape(1, -1), df, faltan


# ─────────────────────── bot ───────────────────────

class BotValidacion:
    def __init__(self, args):
        self.args = args
        self.modelo, self.medianas, self.meta = cargar_modelo(args.modelo)
        self.feats = self.meta["features"]
        self.umbral = args.umbral if args.umbral is not None else self.meta["umbral"]
        self.tf = self.meta["timeframe"]
        self.horas = (None if args.horas == "todas"
                      else tuple(int(v) for v in args.horas.split("-")))
        self.reg = Registro(args.db)
        self.api = None
        self.parar = threading.Event()
        self.abiertas = threading.Semaphore(args.max_simultaneas)
        self._lock = threading.Lock()
        self.pnl_dia = 0.0
        self.n_ops = 0
        self.n_pred = 0
        self.ultima_vela = {}      # activo -> ts de la ultima vela procesada
        self.estado_activos = {}   # activo -> {tipo: (habilitado, suspendido)}
        self.fallos_velas = 0      # timeouts seguidos pidiendo velas
        # Que clase de opcion compramos. La expiracion la decide el timeframe
        # del modelo, y de ella depende el tipo: hasta 5 minutos es "turbo",
        # a partir de ahi "binary". Importa porque un activo puede estar
        # suspendido en turbo y abierto en binary a la misma hora.
        self.tipo_opcion = "turbo" if self.tf // 60 <= 5 else "binary"
        # Activos que ademas de evaluarse se OPERAN. El resto se evalua, se
        # registra y se resuelve su desenlace virtual, pero no se arriesga
        # dinero en ellos. Sirve para acumular datos etiquetados de todo el
        # universo mientras solo se opera donde hay edge demostrado.
        self.solo_operar = (set(a.strip() for a in args.solo_operar.split(",") if a.strip())
                            if args.solo_operar else None)
        self.latido = time.time()   # ultima vuelta completa del bucle principal

    # ── conexion ──
    def conectar(self):
        from iqoptionapi.stable_api import IQ_Option
        self.api = IQ_Option(self.args.email, self.args.password)
        self.api.connect()
        for _ in range(30):
            if self.api.check_connect():
                break
            time.sleep(1)
        if not self.api.check_connect():
            raise SystemExit("no se pudo conectar a IQ Option")
        modo = "REAL" if self.args.permitir_real else "PRACTICE"
        self.api.change_balance(modo)
        log(f"conectado ({modo})  balance ${self.api.get_balance():.2f}")
        self.refrescar_catalogo(inicial=True)

    def refrescar_catalogo(self, inicial=False):
        """Lee init_v2: ids de activo y, sobre todo, si estan SUSPENDIDOS.

        Dos cosas que el bot no sabia:

        1. `iqoptionapi.constants.ACTIVES` no incluye los activos '-op' (el
           forex real). Sin su id, `buy` lanza KeyError y parecia que la
           cuenta no podia operarlos. Si puede: init_v2 trae el id, y basta
           con inyectarlo en el diccionario de la libreria.

        2. Un activo suspendido sigue publicando payout. El bot miraba el
           payout, lo veia en 82%, mandaba la orden y se la rechazaban. Asi se
           perdieron 12 senales seguidas el 22/09 sin dejar rastro: los OTC se
           suspenden mientras el mercado real esta abierto.
        """
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
            log(f"catalogo: {len(estado)} activos | {nuevos} ids inyectados "
                f"(la libreria no los traia)")

    def vigilante(self):
        """Mata el proceso si el bucle principal deja de dar vueltas.

        No es paranoia: iqoptionapi tiene esperas sin salida que congelan el
        bot entero sin lanzar una excepcion ni escribir una linea en el log.
        `get_all_init()` reintenta con `while True` y `connect()` espera el
        balance con `while balance_id is None: pass`. Medido dos veces: la
        noche del 22 al 23 de septiembre el bot desperto a las 01:14 UTC,
        evaluo un ciclo y se quedo helado 15 horas.

        Un proceso muerto se reinicia solo. Un proceso vivo y congelado no se
        nota hasta que alguien mira, y para entonces ya se perdio la sesion.
        """
        limite = self.args.watchdog_seg
        while not self.parar.is_set():
            self.parar.wait(30)
            if self.parar.is_set():
                return
            quieto = time.time() - self.latido
            if quieto > limite:
                log(f"VIGILANTE: {quieto:.0f}s sin completar un ciclo "
                    f"(limite {limite:.0f}s). El proceso esta colgado; saliendo "
                    f"con codigo 3 para que el supervisor lo reinicie.")
                os._exit(3)

    def suspendido(self, activo):
        """True si el broker no acepta ordenes ahora. None si no lo sabemos."""
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
    def en_horario(self, hora_utc):
        if self.horas is None:
            return True
        return self.horas[0] <= hora_utc <= self.horas[1]

    def payout_de(self, activo):
        """Devuelve el payout en % o None si la API no lo publica.

        Distinguir None de 0 importa: `get_all_profit()` devuelve 0 con el
        mercado ABIERTO y cotizando (comprobado un lunes a las 05:02 UTC con
        velas de hace 40 segundos). Tratar ese 0 como "payout bajo" haria que
        el bot descartara todas las senales sin que se notara el motivo real.
        """
        for intento in range(3):
            try:
                p = self.api.get_all_profit().get(activo, {})
                if isinstance(p, dict):
                    # El payout del tipo de opcion que vamos a comprar de
                    # verdad. Coger el maximo de los dos mentia: en los '-op'
                    # turbo y binary pagan distinto y ademas se suspenden en
                    # horarios distintos.
                    v = round(100 * (p.get(self.tipo_opcion, 0) or 0), 1)
                    if v <= 0:
                        v = round(100 * max(p.get("binary", 0) or 0,
                                            p.get("turbo", 0) or 0), 1)
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

        # Resolver las predicciones anteriores de este activo cuyo desenlace ya
        # se conoce. Las velas que hacen falta son estas mismas, asi que sale
        # gratis — y convierte cada señal descartada en un dato etiquetado.
        try:
            self.reg.resolver_virtuales(activo, self.tf, velas)
        except Exception as e:
            log(f"  {activo}: no se pudo resolver el historico ({e})")

        # la ultima vela puede estar aun abierta: usar la anterior
        velas = sorted(velas, key=lambda v: v["from"])
        ahora = time.time()
        if velas[-1]["from"] + self.tf > ahora:
            velas = velas[:-1]
        if not velas:
            return
        ts_vela = int(velas[-1]["from"])

        with self._lock:
            if self.ultima_vela.get(activo) == ts_vela:
                return                      # ya procesada
            self.ultima_vela[activo] = ts_vela

        t_cierre = ts_vela + self.tf        # momento en que cerro la vela

        # No operar velas rancias. El modelo predice la direccion DESDE el cierre
        # de la vela senal; si entramos varios minutos despues, parte del
        # movimiento ya ocurrio y la prediccion no aplica al precio que pagamos.
        # Sin esta guardia, cada reinicio del bot colocaba una orden sobre la
        # ultima vela cerrada aunque hubiera cerrado hace 7 minutos (medido:
        # 440.927 ms de latencia en el primer arranque tras un reinicio).
        antiguedad = time.time() - t_cierre
        if antiguedad > self.args.max_antiguedad_seg:
            log(f"  {activo}: vela de hace {antiguedad:.0f}s, demasiado rancia; se omite")
            return

        X, df, faltan = features_de_velas(velas, self.feats, self.medianas)
        if faltan > len(self.feats) // 3:
            return                          # demasiados huecos, no fiarse

        # Guardia contra contaminacion entre hilos: si el precio de este activo
        # se aparta muchisimo de su propia historia reciente, algo se cruzo.
        cierres = df.close.tail(50)
        if len(cierres) > 10:
            med = float(cierres.median())
            ult = float(df.close.iloc[-1])
            if med > 0 and not (0.5 < ult / med < 2.0):
                log(f"  {activo}: precio incoherente ({ult} vs mediana {med}); se descarta")
                with self._lock:
                    self.ultima_vela.pop(activo, None)
                return

        prob = float(self.modelo.predict_proba(X)[0, 1])
        conf = abs(prob - 0.5)
        direccion = "call" if prob > 0.5 else "put"
        fila = df.iloc[-1]
        hora_utc = int(fila["hora"])
        cierre = float(fila["close"])

        ventana = df.tail(20)[["open", "high", "low", "close"]].values.round(6).tolist()
        indic = {f: (None if pd.isna(fila.get(f)) else round(float(fila.get(f)), 6))
                 for f in self.feats}

        # decidir
        motivo = None
        if conf < self.umbral:
            motivo = "baja_confianza"
        elif not self.en_horario(hora_utc):
            motivo = "fuera_horario"
        elif self.args.seco:
            motivo = "modo_seco"
        elif self.pnl_dia <= -abs(self.args.max_perdida_dia):
            motivo = "limite_perdida_dia"

        if motivo is None and self.solo_operar and activo not in self.solo_operar:
            motivo = "no_seleccionado"

        # Un activo suspendido acepta la evaluacion pero rechaza la orden.
        # Comprobarlo aqui convierte 12 rechazos invisibles en 12 senales
        # correctamente etiquetadas.
        if motivo is None and self.suspendido(activo):
            motivo = "activo_suspendido"

        payout = self.payout_de(activo) if motivo is None else None
        if motivo is None:
            if payout is None:
                # la API no lo publica; usar el asumido si se indico, si no, no operar
                if self.args.payout_asumido:
                    payout = self.args.payout_asumido
                else:
                    motivo = "payout_desconocido"
            if motivo is None and payout < self.args.payout_minimo:
                motivo = "payout_bajo"

        opera = motivo is None
        if opera and not self.abiertas.acquire(blocking=False):
            motivo, opera = "limite_simultaneas", False

        pid = self.reg.registrar_prediccion(
            ts_vela=ts_vela, activo=activo, timeframe=self.tf, prob=prob,
            umbral=self.umbral, direccion=direccion, operada=opera,
            motivo_no_operada=motivo, cierre_vela=cierre, ventana_ohlc=ventana,
            indicadores=indic, payout=payout, hora_utc=hora_utc,
            dia_semana=int(fila["dow"]))
        self.n_pred += 1

        if not opera:
            if conf >= self.umbral:
                log(f"  {activo} {direccion} conf={conf:.4f} descartada: {motivo}")
            return

        try:
            self.operar(pid, activo, direccion, cierre, payout, t_cierre, ts_vela)
        finally:
            self.abiertas.release()

    def operar(self, pid, activo, direccion, cierre_vela, payout, t_cierre, ts_vela):
        monto = self.args.monto
        t0 = time.time()
        try:
            # La expiracion DEBE coincidir con el timeframe del modelo.
            # Estaba fijada a 1 minuto mientras el modelo predice la direccion de
            # la proxima vela de 30 min: eran dos apuestas distintas. Ademas, 1 min
            # compra una opcion "turbo", que paga menos que la "binary" (82% vs 86%
            # medido en la primera operacion real).
            exp_min = max(1, self.tf // 60)
            ok, detalle = self.api.buy(monto, activo, direccion, exp_min)
        except Exception as e:
            self.reg.registrar_rechazo(pid, motivo=f"excepcion en buy: {e}")
            log(f"  {activo}: buy fallo ({e})")
            return
        if not ok:
            # `detalle` trae el motivo que da el broker. Antes se descartaba y
            # el log solo decia "orden rechazada", que no permite distinguir
            # un activo suspendido de un saldo insuficiente.
            motivo = str(detalle) if detalle else "buy devolvio False sin mensaje"
            self.reg.registrar_rechazo(pid, motivo=motivo)
            log(f"  {activo}: orden RECHAZADA -> {motivo}")
            return
        order_id = detalle

        latencia = int((time.time() - t_cierre) * 1000)
        # precio de entrada real: el primer tick tras la orden
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

        # esperar el vencimiento y recoger resultado + PRECIO DE SALIDA
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
        """Lee el resultado del socket. Devuelve (ganancia, resultado) o (None, None)."""
        limite = time.time() + self.tf * ESPERA_RESULTADO
        while time.time() < limite and not self.parar.is_set():
            try:
                raw = self.api.api.socket_option_closed.get(order_id)
                if raw:
                    msg = raw.get("msg", {}) if isinstance(raw, dict) else {}
                    w = str(msg.get("win", "")).lower()
                    if w == "win":
                        return round(float(msg.get("win_amount", 0)) -
                                     float(msg.get("sum", 0)), 4), "win"
                    if w == "equal":
                        return 0.0, "tie"
                    if w:
                        return -float(msg.get("sum", self.args.monto)), "loss"
            except Exception:
                pass
            time.sleep(0.5)
        return None, None

    def vela_de_resultado(self, activo, ts_vela):
        """La vela SIGUIENTE a la de decision: la que determina el resultado."""
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
        activos = self.args.activos.split(",") if self.args.activos else self.meta["activos"]
        log(f"modelo {self.meta['hash']} | tf {self.meta['timeframe_nombre']} | "
            f"umbral {self.umbral:.6f} | horas {self.args.horas}")
        log(f"activos: {', '.join(activos)}")
        if self.solo_operar:
            resto = [a for a in activos if a not in self.solo_operar]
            log(f"se OPERA en: {', '.join(sorted(self.solo_operar))}")
            log(f"solo se evalua (sin dinero): {', '.join(resto)}")
        log(f"monto ${self.args.monto} | max simultaneas {self.args.max_simultaneas} | "
            f"{'MODO SECO (no opera)' if self.args.seco else 'OPERANDO'}")

        huerfanas = self.reg.cerrar_huerfanas(self.tf)
        if huerfanas:
            log(f"{huerfanas} operacion(es) de runs anteriores vencieron sin "
                f"resultado; marcadas como error para que no contaminen el analisis")

        self.reg.iniciar_run(
            tipo_cuenta="REAL" if self.args.permitir_real else "PRACTICE",
            modelo_nombre=f"gb_{self.meta['timeframe_nombre']}_{self.args.horas}",
            modelo_hash=self.meta["hash"],
            modelo_entrenado=str(self.meta["datos_hasta"]),
            umbral=self.umbral, timeframe=self.tf, horas=self.args.horas,
            monto=self.args.monto,
            wr_esperado=self.args.wr_esperado or self.meta.get("wr_esperado") or 59.67,
            config=vars(self.args), notas=self.args.notas,
            reanudar=self.args.reanudar)

        threading.Thread(target=self.vigilante, daemon=True).start()

        while not self.parar.is_set():
            try:
                if not self.api.check_connect() and not self.reconectar():
                    log("sin conexion; reintentando en 60s")
                    self.parar.wait(60)
                    continue
                self.latido = time.time()
                ahora = datetime.now(timezone.utc)
                if self.horas and not self.en_horario(ahora.hour):
                    # dormir hasta que entre el horario, comprobando cada 5 min
                    log(f"fuera de horario ({ahora:%H:%M} UTC); esperando...")
                    self.parar.wait(300)
                    continue
                if ahora.weekday() >= 5 and not self.args.fines_de_semana:
                    log("fin de semana; esperando 30 min")
                    self.parar.wait(1800)
                    continue

                # el catalogo cambia durante el dia: los OTC se suspenden
                # cuando abre el mercado real, y los -op al reves
                self.refrescar_catalogo()

                hilos = []
                for a in activos:
                    if self.parar.is_set():
                        break
                    t = threading.Thread(target=self.procesar, args=(a,), daemon=True)
                    t.start()
                    hilos.append(t)
                    time.sleep(0.4)          # no saturar la API
                # Solo esperamos la fase de DECISION. Los hilos que abrieron
                # una operacion siguen vivos hasta el vencimiento, pero
                # esperarlos aqui hacia que el bot se saltara el ciclo
                # siguiente entero: con velas de 30 min, abrir una operacion
                # costaba la evaluacion de los 30 minutos posteriores.
                for t in hilos:
                    t.join(timeout=ESPERA_DECISION)

                if self.fallos_velas >= 3:
                    log(f"{self.fallos_velas} timeouts de velas seguidos; "
                        f"reconectando y descartando el lock bloqueado")
                    reiniciar_lock_velas()
                    self.fallos_velas = 0
                    self.reconectar()

                # esperar al proximo cierre de vela
                self.latido = time.time()
                espera = self.tf - (time.time() % self.tf) + 3
                self.parar.wait(max(5, espera))
            except Exception as e:
                log(f"error en el bucle: {e}")
                self.parar.wait(30)

        self.reg.cerrar_run()
        log(f"detenido. {self.n_pred} evaluaciones, {self.n_ops} operaciones, "
            f"P&L {self.pnl_dia:+.2f}")
        log(f"analiza con:  py analizar_live.py --db {self.args.db}")


def probar(args):
    """Comprueba modelo y registro sin conectar a IQ Option."""
    print("Comprobando modelo...")
    modelo, medianas, meta = cargar_modelo(args.modelo)
    print(f"  hash {meta['hash']}  tf {meta['timeframe_nombre']}  "
          f"umbral {meta['umbral']:.6f}")
    print(f"  {len(meta['features'])} features, entrenado con {meta['n_entrenamiento']:,} muestras")
    print(f"  datos hasta {meta['datos_hasta']}")

    print("\nComprobando calculo de features con velas historicas...")
    df = S.cargar_tf("velas.db", meta["activos"][0], meta["timeframe"]).tail(VELAS_CONTEXTO)
    velas = [{"from": int(r.ts), "open": r.open, "max": r.high,
              "min": r.low, "close": r.close} for r in df.itertuples()]
    X, dfc, faltan = features_de_velas(velas, meta["features"], medianas)
    p = float(modelo.predict_proba(X)[0, 1])
    print(f"  features calculadas: {X.shape[1]}   huecos rellenados: {faltan}")
    print(f"  prediccion de prueba: prob={p:.4f}  conf={abs(p - 0.5):.4f}  "
          f"-> {'operaria' if abs(p - 0.5) >= meta['umbral'] else 'descartaria'}")

    print("\nComprobando registro...")
    reg = Registro(args.db)
    print(f"  esquema listo en {args.db}")
    print("\nTodo correcto. Para arrancar de verdad:")
    print(f"  py bot_validacion.py --seco      (evalua y registra, sin operar)")
    print(f"  py bot_validacion.py             (opera en PRACTICE)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modelo", default="modelo_live.joblib")
    ap.add_argument("--db", default="live.db")
    ap.add_argument("--horas", default="20-23", help="franja UTC o 'todas'")
    ap.add_argument("--activos", default=None)
    ap.add_argument("--solo-operar", default=None, metavar="LISTA",
                    help="activos en los que SI se opera; el resto solo se evalua "
                         "y se registra (datos etiquetados sin arriesgar dinero)")
    ap.add_argument("--monto", type=float, default=1.0)
    ap.add_argument("--umbral", type=float, default=None)
    ap.add_argument("--max-simultaneas", type=int, default=3)
    ap.add_argument("--max-perdida-dia", type=float, default=50.0)
    ap.add_argument("--payout-minimo", type=float, default=75.0)
    ap.add_argument("--max-antiguedad-seg", type=float, default=180.0,
                    help="no operar velas que cerraron hace mas de N segundos "
                         "(evita ordenes rancias al arrancar). 180s para velas de 30m.")
    ap.add_argument("--payout-asumido", type=float, default=None,
                    help="payout a usar si la API no lo publica (mirarlo en la web de IQ). "
                         "Sin esto, las senales se descartan como payout_desconocido.")
    ap.add_argument("--wr-esperado", type=float, default=None)
    ap.add_argument("--watchdog-seg", type=float, default=900.0,
                    help="si el bucle principal no completa una vuelta en este "
                         "tiempo, el proceso se suicida para que lo reinicien")
    ap.add_argument("--reanudar", action="store_true",
                    help="reutiliza el ultimo run con el mismo modelo y notas, "
                         "para que los reinicios no fragmenten los datos")
    ap.add_argument("--seco", action="store_true", help="evalua y registra, pero no opera")
    ap.add_argument("--fines-de-semana", action="store_true",
                    help="operar sabados y domingos (cripto cotiza 24x7)")
    ap.add_argument("--probar", action="store_true", help="comprueba todo sin conectar")
    ap.add_argument("--permitir-real", action="store_true",
                    help="PELIGRO: opera con dinero real")
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

    bot = BotValidacion(args)

    def parar(_s, _f):
        log("deteniendo (Ctrl+C)...")
        bot.parar.set()
    signal.signal(signal.SIGINT, parar)

    bot.conectar()
    bot.correr()


if __name__ == "__main__":
    main()
