"""
SUPERVISOR en Python — mantiene vivo un bot, sin el enredo de los .sh.

Por que existe:
  - La libreria de IQ Option se cuelga sola (get_all_init_v2 reintenta con
    `while True`, connect espera el balance en un bucle sin salida) y ademas
    cualquier corte de red deja el proceso sin poder reconectar. Los bots
    llevan un vigilante que los mata con codigo 3 cuando el bucle principal se
    detiene; alguien tiene que volver a levantarlos.
  - Los supervisores en .sh duplicaban procesos al lanzarlos (dos arboles
    independientes del mismo bot, apostando doble). Aca eso es IMPOSIBLE: el
    supervisor toma un candado por bot antes de arrancar y, si ya hay uno vivo,
    se va sin hacer nada.

Codigos de salida del bot:
  0  -> parada limpia pedida por el usuario: el supervisor termina tambien.
  !=0 -> se colgo o se cayo: se relanza tras una espera que crece si el fallo
         se repite enseguida (evita el bucle de arranque-crash-arranque).

Uso:
    py supervisar.py weis
    py supervisar.py demarker
    py supervisar.py --estado          # que supervisores hay vivos
    py supervisar.py --parar weis      # detiene supervisor + su bot
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

TEMP = os.environ.get("TEMP") or "/tmp"


def _python_con_deps():
    """Devuelve el ejecutable de Python que tiene scipy e iqoptionapi.

    Necesario en Windows: 'python' en el PATH puede apuntar a un venv
    sin todas las dependencias (ej. hermes-agent). El launcher 'py.exe'
    resuelve al Python del sistema donde estan instaladas."""
    for candidato in [sys.executable, shutil.which("py"), r"C:\Windows\py.exe"]:
        if not candidato or not os.path.exists(candidato):
            continue
        try:
            r = subprocess.run(
                [candidato, "-c", "import scipy, iqoptionapi"],
                capture_output=True, timeout=15)
            if r.returncode == 0:
                return candidato
        except Exception:
            pass
    return sys.executable


_PYTHON = _python_con_deps()

BOTS = {
    "weis": [
        _PYTHON, "bot_weis_bollinger.py",
        "--max-pasos", "6", "--monto", "1", "--max-perdida-dia", "100",
        "--db", "live.db", "--reanudar",
        "--notas", "Weis Wave Volume(acum) ATR_mult=2 racha_min=3 + Bollinger(14,2). "
                   "CON martingala 6 pasos en el MISMO activo, mult=(1+payout)/payout. "
                   "Backtest: n=7022, WR=47.6%, EV negativo sin y con martingala (peor con). "
                   "Desplegado a pedido explicito del usuario pese al resultado negativo, "
                   "en PRACTICE. Opera los 10 pares forex -op.",
    ],
    "demarker": [
        _PYTHON, "bot_demarker.py",
        "--periodo", "17", "--sobrecompra", "80", "--sobreventa", "20",
        "--min-velas", "17", "--solo-operar", "BTCUSD-op,ETHUSD-op",
        "--monto", "1", "--max-simultaneas", "6", "--max-perdida-dia", "20",
        "--max-antiguedad-seg", "25", "--payout-minimo", "75",
        "--payout-asumido", "86", "--watchdog-seg", "600", "--wr-esperado", "62.07",
        "--reanudar", "--db", "live.db",
        "--notas", "DeMarker(17) 80/20 min_velas>17, 1 min, SIN martingala. "
                   "Opera solo BTCUSD-op/ETHUSD-op.",
    ],
}

ESPERA_BASE = 20          # segundos antes de relanzar
ESPERA_MAX = 180
VIDA_CORTA = 120          # si el bot vivio menos que esto, el fallo "cuenta"


def _ruta_candado(nombre):
    return os.path.join(TEMP, f"supervisor_{nombre}.json")


def _vivo(pid):
    if not pid:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) no funciona bien en Windows para comprobar existencia.
        # Usamos OpenProcess + GetExitCodeProcess: si el exit code es 259
        # (STILL_ACTIVE) el proceso sigue corriendo.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _leer_candado(nombre):
    try:
        with open(_ruta_candado(nombre)) as fh:
            return json.load(fh)
    except Exception:
        return None


def _tomar_candado(nombre):
    """True si quedo tomado; False si ya hay un supervisor vivo para ese bot."""
    previo = _leer_candado(nombre)
    if previo and _vivo(previo.get("supervisor_pid")):
        return False
    with open(_ruta_candado(nombre), "w") as fh:
        json.dump({"supervisor_pid": os.getpid(), "bot": nombre,
                   "desde": datetime.now().isoformat(timespec="seconds")}, fh)
    return True


def _soltar_candado(nombre):
    actual = _leer_candado(nombre)
    if actual and actual.get("supervisor_pid") == os.getpid():
        try:
            os.remove(_ruta_candado(nombre))
        except OSError:
            pass


def log(nombre, msg):
    linea = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(linea, flush=True)
    try:
        with open(os.path.join(TEMP, f"supervisor_{nombre}.log"), "a", encoding="utf-8") as fh:
            fh.write(linea + "\n")
    except Exception:
        pass


def supervisar(nombre):
    if nombre not in BOTS:
        raise SystemExit(f"bot desconocido: {nombre}. Opciones: {', '.join(BOTS)}")
    if not _tomar_candado(nombre):
        previo = _leer_candado(nombre)
        raise SystemExit(f"ya hay un supervisor de '{nombre}' corriendo "
                         f"(pid {previo.get('supervisor_pid')}, desde {previo.get('desde')}). "
                         f"No arranco otro para no duplicar operaciones.")

    salida = os.path.join(TEMP, f"live_{nombre}.log")
    log(nombre, f"supervisor arrancado (pid {os.getpid()}) -> {salida}")
    parar = {"ahora": False}

    def _manejar(_s, _f):
        parar["ahora"] = True
    try:
        signal.signal(signal.SIGINT, _manejar)
        signal.signal(signal.SIGTERM, _manejar)
    except Exception:
        pass

    fallos_seguidos = 0
    proc = None
    try:
        while not parar["ahora"]:
            t0 = time.time()
            with open(salida, "a", encoding="utf-8") as fh:
                fh.write(f"\n=== arranque {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
                fh.flush()
                proc = subprocess.Popen(BOTS[nombre], stdout=fh, stderr=subprocess.STDOUT)
                log(nombre, f"bot lanzado (pid {proc.pid})")
                codigo = proc.wait()
            proc = None
            vivio = time.time() - t0

            if parar["ahora"]:
                break
            if codigo == 0:
                log(nombre, "el bot termino limpio (codigo 0); el supervisor tambien termina")
                break

            fallos_seguidos = fallos_seguidos + 1 if vivio < VIDA_CORTA else 0
            espera = min(ESPERA_MAX, ESPERA_BASE * (2 ** min(fallos_seguidos, 3)))
            log(nombre, f"el bot salio con codigo {codigo} tras {vivio/60:.1f} min "
                        f"(fallos rapidos seguidos: {fallos_seguidos}); relanzo en {espera}s")
            fin = time.time() + espera
            while time.time() < fin and not parar["ahora"]:
                time.sleep(1)
    finally:
        if proc and proc.poll() is None:
            log(nombre, "deteniendo el bot...")
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _soltar_candado(nombre)
        log(nombre, "supervisor detenido")


def estado():
    print(f"{'bot':<12} {'supervisor':<12} {'desde':<22} estado")
    for nombre in BOTS:
        c = _leer_candado(nombre)
        if c and _vivo(c.get("supervisor_pid")):
            print(f"{nombre:<12} {c['supervisor_pid']:<12} {c.get('desde',''):<22} vivo")
        else:
            print(f"{nombre:<12} {'—':<12} {'—':<22} sin supervisor")


def parar_bot(nombre):
    c = _leer_candado(nombre)
    if not c or not _vivo(c.get("supervisor_pid")):
        print(f"no hay supervisor vivo para '{nombre}'")
        return
    pid = c["supervisor_pid"]
    print(f"deteniendo supervisor de '{nombre}' (pid {pid}) y su bot...")
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)
    try:
        os.remove(_ruta_candado(nombre))
    except OSError:
        pass
    print("listo")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bot", nargs="?", choices=sorted(BOTS))
    ap.add_argument("--estado", action="store_true")
    ap.add_argument("--parar", choices=sorted(BOTS))
    a = ap.parse_args()
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if a.estado:
        estado()
    elif a.parar:
        parar_bot(a.parar)
    elif a.bot:
        supervisar(a.bot)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
