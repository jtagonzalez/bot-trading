#!/bin/sh
# LEVANTA TODO — un solo comando para arrancar los 3 bots + el panel unificado.
#
#   EURUSD (15m, modelo ML)      -> supervisar.sh
#   cripto 15m (modelo ML)       -> supervisar_cripto.sh
#   DeMarker 1m (regla, cripto)  -> supervisar_demarker.sh
#   panel.py                     -> http://localhost:8090, muestra los 3 juntos
#
# Cada supervisor corre en SEGUNDO PLANO (nohup) y se reinicia solo si su bot
# se cuelga. Este script no se queda esperando: lanza y vuelve a la terminal.
# Los logs de cada uno quedan en $TEMP (o en $LOG_DIR si se define).
#
# Uso:
#   sh levantar_todo.sh              # arranca los 3 bots + panel
#   sh levantar_todo.sh --sin-panel  # solo los bots, sin abrir el navegador
#   sh detener_todo.sh               # para todo (ver ese script)

cd "$(dirname "$0")" || exit 1
LOG_DIR="${LOG_DIR:-$TEMP}"
PIDS="$LOG_DIR/bots_pids.txt"

echo "Arrancando los 3 bots en segundo plano..."
: > "$PIDS"

nohup sh supervisar.sh          > "$LOG_DIR/supervisor_eurusd.out"   2>&1 &
echo "eurusd:$!"   >> "$PIDS"
echo "  [OK] EURUSD 15m      (supervisor pid $!)"

nohup sh supervisar_cripto.sh   > "$LOG_DIR/supervisor_cripto.out"   2>&1 &
echo "cripto15:$!" >> "$PIDS"
echo "  [OK] cripto 15m      (supervisor pid $!)"

nohup sh supervisar_demarker.sh > "$LOG_DIR/supervisor_demarker.out" 2>&1 &
echo "demarker:$!" >> "$PIDS"
echo "  [OK] DeMarker 1m     (supervisor pid $!)"

nohup sh supervisar_weis.sh     > "$LOG_DIR/supervisor_weis.out"     2>&1 &
echo "weis:$!"     >> "$PIDS"
echo "  [OK] Weis+Bollinger  (supervisor pid $!)"

if [ "$1" != "--sin-panel" ]; then
    nohup py panel.py --no-abrir > "$LOG_DIR/panel.out" 2>&1 &
    echo "panel:$!" >> "$PIDS"
    echo "  [OK] panel           (pid $!) -> http://localhost:8090"
fi

echo ""
echo "Todo levantado. PIDs guardados en $PIDS"
echo "Logs: $LOG_DIR/live_supervisado.log  live_cripto.log  live_demarker.log  live_weis.log"
echo "Para pararlo todo:  sh detener_todo.sh"
