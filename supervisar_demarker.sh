#!/bin/sh
# SUPERVISOR para bot_demarker.py (DeMarker 17, crypto BTCUSD/ETHUSD)
cd "$(dirname "$0")" || exit 1
LOG="${LOG:-$TEMP/live_demarker.log}"
ESPERA=20

echo "supervisor demarker arrancado $(date '+%Y-%m-%d %H:%M:%S') -> $LOG"

while true; do
    echo "" >> "$LOG"
    echo "=== arranque $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"

    py bot_demarker.py \
        --periodo 17 \
        --sobrecompra 80 \
        --sobreventa 20 \
        --min-velas 17 \
        --solo-operar BTCUSD-op,ETHUSD-op \
        --monto 1 \
        --max-simultaneas 6 \
        --max-perdida-dia 20 \
        --max-antiguedad-seg 25 \
        --payout-minimo 75 \
        --payout-asumido 86 \
        --watchdog-seg 600 \
        --wr-esperado 62.07 \
        --reanudar \
        --db live.db \
        --notas "DeMarker(17) 80/20 min_velas>17, 1 min, SIN martingala. Opera solo BTCUSD-op/ETHUSD-op." \
        >> "$LOG" 2>&1

    codigo=$?
    echo "--- bot demarker salio codigo $codigo $(date '+%H:%M:%S') ---" >> "$LOG"

    if [ "$codigo" -eq 0 ]; then
        echo "parada limpia; supervisor demarker termina"
        exit 0
    fi

    echo "reiniciando en ${ESPERA}s" >> "$LOG"
    sleep "$ESPERA"
done
