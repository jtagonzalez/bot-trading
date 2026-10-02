#!/bin/sh
# DETIENE TODO — para los 3 supervisores (y los bots que levantaron) y el panel.
#
# Los supervisores reinician el bot si muere con codigo != 0, asi que no basta
# con matar al bot: hay que matar primero al supervisor (sh) y luego a su bot
# (py), o el supervisor lo vuelve a levantar en 20s. Por eso se mata el arbol
# de procesos completo de cada PID guardado por levantar_todo.sh.
#
# Uso:
#   sh detener_todo.sh

cd "$(dirname "$0")" || exit 1
LOG_DIR="${LOG_DIR:-$TEMP}"
PIDS="$LOG_DIR/bots_pids.txt"

if [ ! -f "$PIDS" ]; then
    echo "no encuentro $PIDS — ¿se arranco con levantar_todo.sh?"
    echo "si los arrancaste a mano, para cada proceso con: taskkill //F //T //PID <pid>"
    exit 1
fi

while IFS=: read -r nombre pid; do
    [ -z "$pid" ] && continue
    echo "deteniendo $nombre (pid $pid, con sus hijos)..."
    taskkill //F //T //PID "$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null
done < "$PIDS"

rm -f "$PIDS"
echo "listo."
