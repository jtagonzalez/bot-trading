"""
BOT WEIS WAVE VOLUME (acumulativo) + BOLLINGER(14,2) — prueba en PRACTICE
de la estrategia descartada en backtest_weis_bollinger.py.

Backtest completo (~2 meses, 1 minuto, 10 pares forex -op + cripto):
n=7.022 señales, WR=47.6%, por debajo de breakeven (53.76%) y de un control
aleatorio del mismo tamaño. Ninguna variante de parametros sobrevive
Benjamini-Hochberg. CON martingala el EV es TODAVIA PEOR, no mejor:
- 4 pasos:  92.0% de los ciclos recupera, 8.0% agota -> EV=-0.0843/$
- 6 pasos:  97.3-97.8% recupera, 2.2-2.7% agota -> EV=-0.0538 a -0.0967/$
  (el 2-3% que agota pierde en promedio -$15 a -$70 por ciclo, mas de lo
  que ganan juntos todos los ciclos que si recuperan)

Se implementa de todas formas A PEDIDO EXPLICITO del usuario, en CUENTA
PRACTICE (dinero virtual). Expectativa realista: el bot va a perder saldo
de forma sostenida, con caidas fuertes cada vez que un ciclo agota los
intentos de martingala.

MARTINGALA: en el MISMO activo (igual que se valido via
portafolio_multi_edge.ciclos_generico), no la martingala flotante
multi-activo de bot.py. Multiplicador = (1+payout)/payout, el que
exactamente recupera lo apostado en todos los pasos anteriores mas la
ganancia de un solo paso base — igual formula que se uso en TODO el
backtest, no un numero fijo arbitrario.

Esqueleto de seguridad identico a bot_demarker.py / bot_validacion.py
(conexion con timeout, vigilante, catalogo de suspendidos, registro
completo en live.db).

Uso:
    py bot_weis_bollinger.py --probar      # sin conectar: valida la señal con velas historicas
    py bot_weis_bollinger.py --seco        # conecta y evalua, pero NO opera
    py bot_weis_bollinger.py               # opera los 10 pares forex -op, martingala 6 pasos
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

from registro import Registro
from backtest_weis_bollinger import ATR_MULT_DEF, RACHA_MIN_DEF, detectar_senales

VELAS_CONTEXTO = 300
ESPERA_RESULTADO = 2.5
ESPERA_DECISION = 45
TF = 60                        # 1 minuto, igual que el backtest

ACTIVOS_DEFECTO = ("EURUSD-op,GBPUSD-op,USDJPY-op,AUDUSD-op,USDCAD-op,NZDUSD-op,"
                   "EURJPY-op,GBPJPY-op,EURGBP-op,USDCHF-op")

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
    """Igual que en bot_demarker.py: get_candles serializado y CON TIMEOUT."""
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


# ─────────────────────── señal (Weis Wave acumulativo + Bollinger) ───────────────────────

def _armar_df(velas):
    df = pd.DataFrame(velas)
    df = df.rename(columns={"from": "ts", "max": "high", "min": "low"})
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["volume"] = df["volume"].fillna(0).astype(float)
    df = df[["ts", "open", "high", "low", "close", "volume"]].astype(
        {"ts": "int64", "open": float, "high": float, "low": float, "close": float})
    df = df.sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df.ts, unit="s", utc=True)
    df["hora"] = df.dt.dt.hour
    df["dow"] = df.dt.dt.dayofweek
    return df


def _wwv_estado_actual(df, atr_mult, racha_min):
    """Replica detectar_senales() pero solo para leer el ESTADO de la ultima
    vela (color de la onda en curso, acumulado, umbral vigente) -- para
    logging/diagnostico, no para decidir (eso lo hace detectar_senales)."""
    c = df.close.values.astype(float)
    v = df.volume.values.astype(float)
    n = len(df)
    tr_close = np.empty(n)
    tr_close[0] = np.nan
    tr_close[1:] = np.abs(np.diff(c))
    atr = pd.Series(tr_close).ewm(alpha=1 / 14, adjust=False).mean().values
    brick_size = atr_mult * atr

    extremo = c[0]
    direccion = 0
    onda_inicio = 0
    vol_acum = 0.0
    ultima_onda_color, ultima_onda_vol_total = 0, None

    for i in range(1, n):
        b = brick_size[i]
        if np.isnan(b):
            continue
        reversion, inicio_absoluto = False, False
        if direccion == 0:
            if c[i] - c[onda_inicio] >= b:
                direccion, inicio_absoluto = 1, True
            elif c[onda_inicio] - c[i] >= b:
                direccion, inicio_absoluto = -1, True
            else:
                continue
        elif direccion == 1:
            if c[i] >= extremo:
                extremo = c[i]
            elif extremo - c[i] >= b:
                reversion = True
        else:
            if c[i] <= extremo:
                extremo = c[i]
            elif c[i] - extremo >= b:
                reversion = True

        if reversion:
            dur_onda = i - onda_inicio
            if dur_onda >= racha_min:
                ultima_onda_color, ultima_onda_vol_total = direccion, vol_acum
            else:
                ultima_onda_color = 0
            direccion = -direccion
            extremo = c[i]
            onda_inicio = i
            vol_acum = 0.0
        elif inicio_absoluto:
            onda_inicio = i
            vol_acum = 0.0
        vol_acum += v[i]

    umbral = (ultima_onda_vol_total if (ultima_onda_color != 0 and direccion == -ultima_onda_color)
              else None)
    return int(direccion), round(float(vol_acum), 2), (round(float(umbral), 2) if umbral else None)


def calcular_senal(velas, atr_mult, racha_min):
    """Evalua la señal en la ULTIMA vela cerrada, MISMA regla (variante
    ESTRICTA) que backtest_weis_bollinger.detectar_senales. Devuelve
    (direccion_o_None, df, color_onda_actual, vol_acum_actual, umbral_vigente)."""
    df = _armar_df(velas)
    if len(df) < 60:
        return None, df, 0, 0.0, None

    eventos = detectar_senales(df, atr_mult, racha_min, "estricta")
    n = len(df)
    direccion = None
    for idx, d in eventos:
        if idx == n - 1:
            direccion = "call" if d == 1 else "put"
            break

    color, vol_acum, umbral = _wwv_estado_actual(df, atr_mult, racha_min)
    return direccion, df, color, vol_acum, umbral


# ─────────────────────── bot ───────────────────────

class BotWeisBollinger:
    def __init__(self, args):
        self.args = args
        self.atr_mult = args.atr_mult
        self.racha_min = args.racha_min
        self.max_pasos = args.max_pasos
        self.tf = TF
        self.tipo_opcion = "turbo"
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
    def operable(self, hora_utc, dow):
        """Forex -op: 01-17h UTC, lunes a viernes (misma ventana validada en
        el backtest y en el resto del proyecto)."""
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

        direccion, df, color, vol_acum, umbral = calcular_senal(velas, self.atr_mult, self.racha_min)

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
            return

        fila = df.iloc[-1]
        hora_utc, dow, cierre = int(fila["hora"]), int(fila["dow"]), float(fila["close"])

        motivo = None
        if not self.operable(hora_utc, dow):
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
            indicadores={"atr_mult": self.atr_mult, "racha_min": self.racha_min,
                        "wwv_color_onda": color, "wwv_vol_acum": vol_acum,
                        "wwv_umbral_onda_anterior": umbral, "max_pasos": self.max_pasos},
            payout=payout, hora_utc=hora_utc, dia_semana=dow)
        self.n_pred += 1
        log(f"  {activo} {direccion.upper()} WWV_acum={vol_acum} umbral_onda_ant={umbral} "
            f"{'' if opera else f'(no opera: {motivo})'}")

        if not opera:
            return
        try:
            self.operar(pid, activo, direccion, cierre, payout, t_cierre)
        finally:
            self.abiertas.release()

    def operar(self, pid, activo, direccion, cierre_vela, payout_pct, t_cierre):
        """Martingala en el MISMO activo, hasta max_pasos. Multiplicador =
        (1+payout)/payout -- igual formula que portafolio_multi_edge.ciclos_generico,
        la que se uso para TODOS los numeros reportados en el backtest."""
        payout_frac = (payout_pct or self.args.payout_asumido or 86.0) / 100
        mult = (1 + payout_frac) / payout_frac
        monto = self.args.monto

        ganancia_neta, arriesgado = 0.0, 0.0
        pasos_detalle = []
        precio_entrada_1 = cierre_vela
        precio_ref_prev = cierre_vela
        precio_salida_final = None
        resultado_final = None

        for paso in range(1, self.max_pasos + 1):
            try:
                ok, detalle = self.api.buy(monto, activo, direccion, 1)
            except Exception as e:
                log(f"  {activo} paso{paso}: buy excepcion ({e})")
                if paso == 1:
                    self.reg.registrar_rechazo(pid, motivo=f"excepcion en buy: {e}")
                    return
                resultado_final = "error"
                break
            if not ok:
                motivo = str(detalle) if detalle else "buy devolvio False sin mensaje"
                log(f"  {activo} paso{paso}: orden RECHAZADA -> {motivo}")
                if paso == 1:
                    self.reg.registrar_rechazo(pid, motivo=motivo)
                    return
                resultado_final = "error"
                break

            order_id = detalle
            t0 = time.time()
            latencia = int((t0 - t_cierre) * 1000) if paso == 1 else None
            if paso == 1:
                self.reg.registrar_entrada(pid, order_id=order_id, precio_entrada=precio_entrada_1,
                                           monto=monto, payout_contratado=payout_frac,
                                           latencia_ms=latencia)
                self.n_ops += 1

            arriesgado += monto
            log(f"  {activo} paso {paso}/{self.max_pasos} {direccion.upper()} ${monto:.2f} "
                f"orden={order_id}")

            # IMPORTANTE: no se pide ningun precio de referencia aca en el
            # camino critico -- esperar_resultado() ya da el resultado REAL
            # (viene del socket de IQ Option). Pedir un precio extra antes de
            # reentrar solo demora la siguiente apuesta sin necesidad. Un
            # get_candles solo se pide si hace falta para el fallback
            # (resultado is None, caso raro) o al final del ciclo para dejar
            # un precio de referencia en el registro.
            ganancia, resultado = self.esperar_resultado(order_id, activo)
            if resultado is None:
                precio_ref = self.precio_actual(activo)
                base = precio_entrada_1 if paso == 1 else precio_ref_prev
                resultado = self.deducir(direccion, base, precio_ref)
                ganancia = (monto * payout_frac if resultado == "win"
                            else (0.0 if resultado == "tie" else -monto))
                precio_ref_prev = precio_ref

            ganancia_neta += ganancia or 0.0
            pasos_detalle.append({"paso": paso, "monto": monto, "resultado": resultado,
                                  "ganancia": round(ganancia or 0.0, 4)})
            icono = {"win": "GANA", "loss": "PIERDE", "tie": "EMPATE"}.get(resultado, resultado)
            log(f"  {activo} paso {paso} -> {icono} {ganancia:+.2f}  neto_ciclo={ganancia_neta:+.2f}")

            if resultado == "win":
                resultado_final = resultado
                break
            if resultado == "tie":
                # NO cuenta como perdida ni gandancia, y NO resetea ni escala
                # el monto -- sigue la MISMA cadena con la MISMA apuesta en
                # el siguiente paso (igual que ciclos_generico: r==0 no
                # multiplica la apuesta ni cierra el ciclo). Si el empate
                # ocurre justo en el ultimo paso disponible, el ciclo
                # termina sin haber recuperado nada: cuenta como "loss"
                # (gano=False), igual que en el backtest.
                if paso >= self.max_pasos:
                    resultado_final = "loss"
                    break
                continue
            if paso >= self.max_pasos:
                resultado_final = "loss"
                break
            monto = round(monto * mult, 2)

        if precio_salida_final is None:
            precio_salida_final = self.precio_actual(activo)  # solo para el registro, ya termino el ciclo

        # Guardamos las velas que cubren TODO el ciclo (señal + cada paso). El
        # panel las necesita para dibujar la operacion despues, y bajarlas en
        # ese momento no siempre funciona: la cuenta ya tiene varias sesiones
        # abiertas y la conexion extra suele ser rechazada. Aca si tenemos una
        # conexion que anda, asi que se captura una sola vez y queda guardado.
        velas_ciclo = None
        try:
            ts_senal = int(t_cierre - self.tf)
            crudas = get_candles_seguro(self.api, activo, self.tf,
                                        24 + len(pasos_detalle) + 2,
                                        ts_senal + (len(pasos_detalle) + 2) * self.tf)
            if crudas:
                velas_ciclo = {"velas": [[int(v["from"]), float(v["open"]), float(v["max"]),
                                          float(v["min"]), float(v["close"])]
                                         for v in sorted(crudas, key=lambda c: c["from"])]}
        except Exception as e:
            log(f"  {activo}: no se pudieron guardar las velas del ciclo ({e})")

        self.reg.registrar_salida(
            pid, precio_salida=precio_salida_final, resultado=resultado_final,
            ganancia=ganancia_neta, vela_resultado=velas_ciclo,
            error=json.dumps({"ciclo_pasos": pasos_detalle, "arriesgado": round(arriesgado, 2)}))
        with self._lock:
            self.pnl_dia += ganancia_neta
        icono = {"win": "GANA", "loss": "PIERDE", "tie": "EMPATE"}.get(resultado_final, resultado_final)
        log(f"  {activo} CICLO -> {icono} neto {ganancia_neta:+.2f} "
            f"({len(pasos_detalle)} pasos, arriesgado ${arriesgado:.2f}) | "
            f"dia: {self.pnl_dia:+.2f} en {self.n_ops} señales")

    def precio_actual(self, activo):
        try:
            v = get_candles_seguro(self.api, activo, self.tf, 1)
            if v:
                return float(v[-1]["close"])
        except Exception:
            pass
        return None

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
        log(f"Weis Wave Volume(acum) ATR_mult={self.atr_mult} racha_min={self.racha_min} "
            f"+ Bollinger(14,2) | tf 1 min")
        log(f"activos evaluados: {', '.join(activos)}")
        if self.solo_operar:
            resto = [a for a in activos if a not in self.solo_operar]
            log(f"se OPERA en: {', '.join(sorted(self.solo_operar))}")
            if resto:
                log(f"solo se evalua (sin dinero): {', '.join(resto)}")
        log(f"monto base ${self.args.monto} | CON MARTINGALA hasta {self.max_pasos} pasos "
            f"(mult=(1+payout)/payout) | max simultaneas {self.args.max_simultaneas} | "
            f"{'MODO SECO (no opera)' if self.args.seco else 'OPERANDO'}")
        log("AVISO: backtest de 7.022 señales da WR=47.6% (bajo breakeven) y con martingala "
            "el EV es peor (-5% a -10% por $ arriesgado). Se espera que este bot pierda saldo.")

        huerfanas = self.reg.cerrar_huerfanas(self.tf)
        if huerfanas:
            log(f"{huerfanas} operacion(es) de runs anteriores vencieron sin resultado; marcadas como error")

        self.reg.iniciar_run(
            tipo_cuenta="REAL" if self.args.permitir_real else "PRACTICE",
            modelo_nombre=f"weisbollinger_m{self.max_pasos}",
            modelo_hash=f"atr{self.atr_mult}_racha{self.racha_min}",
            modelo_entrenado="2026-10-01 (backtest 1m, ~2 meses, descartado, prueba en vivo a pedido)",
            umbral=0.5, timeframe=self.tf, horas="1-17 UTC lun-vie", monto=self.args.monto,
            wr_esperado=self.args.wr_esperado or 47.6,
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
                if ahora.weekday() >= 5:
                    log("fin de semana (forex cerrado); esperando 30 min")
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
                    t.join(timeout=ESPERA_DECISION + self.max_pasos * self.tf)

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
        log(f"detenido. {self.n_pred} evaluaciones, {self.n_ops} señales operadas, P&L {self.pnl_dia:+.2f}")


def probar(args):
    """Comprueba la señal con velas historicas, sin conectar a IQ Option."""
    import sqlite3
    print(f"Comprobando señal Weis Wave(acum ATR_mult={args.atr_mult}, racha_min={args.racha_min}) "
          f"+ Bollinger(14,2)...")
    c = sqlite3.connect("velas.db")
    activo_prueba = "EURUSD"
    df = pd.read_sql("SELECT ts,open,high,low,close,volume FROM velas WHERE activo=? AND timeframe=60 "
                     "ORDER BY ts DESC LIMIT ?", c, params=(activo_prueba, VELAS_CONTEXTO))
    df = df.sort_values("ts")
    velas = [{"from": int(r.ts), "open": r.open, "max": r.high, "min": r.low, "close": r.close,
             "volume": r.volume} for r in df.itertuples()]
    direccion, dfc, color, vol_acum, umbral = calcular_senal(velas, args.atr_mult, args.racha_min)
    print(f"  ultima vela de {activo_prueba}: {dfc.dt.iloc[-1]}  color_onda={color}  "
          f"vol_acum={vol_acum}  umbral_onda_anterior={umbral}")
    print(f"  señal ahora mismo: {direccion or 'ninguna'}")
    print("\nComprobando registro...")
    reg = Registro(args.db)
    print(f"  esquema listo en {args.db}")
    print("\nTodo correcto. Para arrancar de verdad:")
    print("  py bot_weis_bollinger.py --seco      (evalua y registra, sin operar)")
    print("  py bot_weis_bollinger.py             (opera los 10 pares forex -op, martingala 6 pasos)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="live.db")
    ap.add_argument("--atr-mult", type=float, default=ATR_MULT_DEF)
    ap.add_argument("--racha-min", type=int, default=RACHA_MIN_DEF)
    ap.add_argument("--max-pasos", type=int, default=6, help="pasos de martingala, incluyendo el inicial")
    ap.add_argument("--activos", default=None, help=f"por defecto: {ACTIVOS_DEFECTO}")
    ap.add_argument("--solo-operar", default=ACTIVOS_DEFECTO, metavar="LISTA",
                    help="activos en los que SI se opera; el resto solo se evalua")
    ap.add_argument("--monto", type=float, default=1.0, help="apuesta BASE (paso 1) del ciclo de martingala")
    ap.add_argument("--max-simultaneas", type=int, default=10)
    ap.add_argument("--max-perdida-dia", type=float, default=100.0)
    ap.add_argument("--payout-minimo", type=float, default=85.0)
    ap.add_argument("--max-antiguedad-seg", type=float, default=25.0)
    ap.add_argument("--payout-asumido", type=float, default=86.0)
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
        print("  Vas a operar con DINERO REAL una estrategia que el backtest")
        print("  mostro NEGATIVA (47.6% WR, peor aun con martingala). NO ES LO")
        print("  RECOMENDADO bajo ninguna circunstancia.")
        print("!" * 70)
        if input("\n  Escribe 'ENTIENDO EL RIESGO' para continuar: ") != "ENTIENDO EL RIESGO":
            raise SystemExit("cancelado")

    bot = BotWeisBollinger(args)

    def parar(_s, _f):
        log("deteniendo (Ctrl+C)...")
        bot.parar.set()
    signal.signal(signal.SIGINT, parar)

    bot.conectar()
    bot.correr()


if __name__ == "__main__":
    main()
