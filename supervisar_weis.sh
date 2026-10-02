#!/bin/sh
# SUPERVISOR para bot_weis_bollinger.py (Weis Wave + Bollinger, martingala 6 pasos)
cd "$(dirname "$0")" || exit 1
LOG="${LOG:-$TEMP/live_weis.log}"
ESPERA=20

echo "supervisor weis arrancado $(date '+%Y-%m-%d %H:%M:%S') -> $LOG"

while true; do
    echo "" >> "$LOG"
    echo "=== arranque $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"

    py bot_weis_bollinger.py \
        --max-pasos 6 \
        --monto 1 \
        --max-perdida-dia 100 \
        --db live.db \
        --reanudar \
        --notas "Weis Wave Volume(acum) ATR_mult=2 racha_min=3 + Bollinger(14,2). CON martingala 6 pasos en el MISMO activo, mult=(1+payout)/payout. Backtest: n=7022, WR=47.6%, EV negativo sin y con martingala (peor con). Desplegado a pedido explicito del usuario pese al resultado negativo, en PRACTICE. Opera los 10 pares forex -op." \
        >> "$LOG" 2>&1

    codigo=$?
    echo "--- bot weis salio codigo $codigo $(date '+%H:%M:%S') ---" >> "$LOG"

    if [ "$codigo" -eq 0 ]; then
        echo "parada limpia; supervisor weis termina"
        exit 0
    fi

    echo "reiniciando en ${ESPERA}s" >> "$LOG"
    sleep "$ESPERA"
done
