#!/bin/sh
# SUPERVISOR — mantiene el bot vivo.
#
# iqoptionapi tiene esperas sin salida (get_all_init reintenta con `while True`,
# connect espera el balance con `while balance_id is None: pass`). Cuando caen
# ahi, el proceso queda vivo y congelado: 0% de CPU, sin excepcion, sin una
# linea en el log. Pasa sobre todo de madrugada, cuando nadie mira.
#
# El bot lleva un vigilante que lo mata con codigo 3 si el bucle principal deja
# de dar vueltas. Este script es la otra mitad: lo vuelve a levantar.
#
# --reanudar hace que los reinicios NO fragmenten los datos: el run es el mismo
# mientras el modelo y las notas no cambien.
#
# Uso:
#   sh supervisar.sh
#   LOG=/ruta/al.log sh supervisar.sh

cd "$(dirname "$0")" || exit 1
LOG="${LOG:-$TEMP/live_cripto.log}"
ESPERA=20          # segundos entre reinicios
intento=0

echo "supervisor arrancado $(date '+%Y-%m-%d %H:%M:%S') -> $LOG"

while true; do
    intento=$((intento + 1))
    echo "" >> "$LOG"
    echo "=== arranque #$intento  $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"

    py bot_validacion.py \
        --modelo modelo_cripto15.joblib \
        --activos BTCUSD-op,ETHUSD-op \
        --solo-operar BTCUSD-op,ETHUSD-op --fines-de-semana \
        --horas todas \
        --monto 1 \
        --max-simultaneas 6 \
        --max-perdida-dia 30 \
        --max-antiguedad-seg 90 \
        --payout-minimo 85 \
        --watchdog-seg 900 \
        --wr-esperado 57.0 \
        --reanudar \
        --db live.db \
        --notas "CRIPTO supervisado: BTCUSD-op y ETHUSD-op 24x7, binary 15m pago 87%. Modelo 15m BTC+ETH, 20% mas confiable. Validado: 56-58% fuera de muestra, aguanta 1-2 min de latencia. Motivado por Kitron y Wengrowicz arXiv 2608.21888." \
        >> "$LOG" 2>&1

    codigo=$?
    echo "--- el bot salio con codigo $codigo  $(date '+%H:%M:%S') ---" >> "$LOG"

    # 0 = parada limpia pedida por el usuario: no reiniciar
    if [ "$codigo" -eq 0 ]; then
        echo "parada limpia; supervisor termina"
        exit 0
    fi

    echo "reiniciando en ${ESPERA}s (codigo $codigo)" >> "$LOG"
    sleep "$ESPERA"
done
