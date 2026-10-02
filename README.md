# Bot de Trading IQ Option — Sistema en producción

> **Cuenta:** PRACTICE · **Estado:** operativo con 4 bots en paralelo

---

## Bots activos

| Bot | Archivo | Activos | Timeframe | Edge validado |
|-----|---------|---------|-----------|---------------|
| EURUSD ML | `supervisar.sh` → `bot_validacion.py` | EURUSD-op | 15 m | WR 55.6 %, p=0.0006 |
| Cripto ML | `supervisar_cripto.sh` → `bot_validacion.py` | BTCUSD-op, ETHUSD-op | 15 m | WR 56–58 %, 24×7 |
| DeMarker | `supervisar_demarker.sh` → `bot_demarker.py` | BTCUSD-op, ETHUSD-op | 1 m | WR 62 % (pendiente validación larga) |
| Weis+BB | `supervisar_weis.sh` → `bot_weis_bollinger.py` | 10 pares forex -op | 1 m | WR 47.6 % — **EV negativo, en PRACTICE por pedido explícito** |

Panel unificado: `http://localhost:8090`

---

## Inicio rápido

```sh
sh levantar_todo.sh          # arranca los 4 bots + panel
sh detener_todo.sh           # para todo
py panel.py --db live.db     # solo el panel (sin bots)
```

Ver [LEVANTAR.md](LEVANTAR.md) y [DETENER.md](DETENER.md) para instrucciones detalladas.

---

## Estructura del proyecto

```
.                           ← raíz: bots activos, módulos del sistema
├── bot_validacion.py       ← bot principal ML (EURUSD y cripto)
├── bot_demarker.py         ← bot DeMarker 1m
├── bot_weis_bollinger.py   ← bot Weis Wave + Bollinger
├── panel.py                ← panel web unificado (port 8090)
├── supervisar*.sh          ← supervisores de cada bot
├── registro.py             ← módulo: guarda ops en live.db
├── entrenar_modelo.py      ← módulo: entrena / re-entrena el modelo ML
├── simulador.py            ← módulo: backtesting genérico
├── backtest_weis_bollinger.py ← señales del bot weis (importado)
├── portafolio_multi_edge.py   ← lógica de ciclos multi-bot
├── estrategias_propias.py  ← reglas de entrada propias
├── estrategia_gold4_bb.py  ← estrategia Gold 4.0 + Bollinger
├── ml_walkforward.py       ← validación walk-forward del modelo
├── grafico_operacion.py    ← genera PNGs de operaciones individuales
├── live.db                 ← base de datos activa (runs, ops, predicciones)
├── modelo_*.joblib         ← modelos ML entrenados
│
├── analisis/               ← scripts de análisis manual
├── backtest/               ← backtest, búsquedas, optimización
├── simulacion/             ← scripts simular_*.py
├── bots_old/               ← versiones archivadas de bots
├── datos/                  ← .pkl, .csv, .xlsx, .json de resultados
├── db/                     ← bases de datos inactivas
├── capturas/               ← imágenes de operaciones (subcarpetas por tipo)
├── docs/                   ← documentación extra, dashboards viejos
└── logs/                   ← logs de sesiones
```

---

## Base de datos (`live.db`)

Tabla `runs` — un registro por arranque de bot.  
Tabla `operaciones` — una fila por operación (entrada, resultado, ganancia).  
Tabla `predicciones` — evaluaciones del modelo (operadas y descartadas).

El panel agrupa automáticamente todos los `runs` del mismo modelo dentro de una ventana de 3 días.

---

## Edges validados

| Edge | Método | WR | p-valor | Breakeven | ¿Rentable? |
|------|--------|----|---------|-----------|------------|
| EURUSD 15m hora 7 UTC | ML walk-forward | 55.6 % | 0.0006 | 52.6 % (payout 90 %) | ✓ |
| BTC/ETH 15m 24×7 | ML walk-forward | 56–58 % | < 0.01 | 52.6 % | ✓ |
| Reversión a la media 1m | RSI + Stoch | 51.9 % | < 0.001 | 54.4 % (payout 84 %) | ✗ payout insuficiente |
| OTC sintéticos | ML + reglas | 49.8 % | — | 54.4 % | ✗ no predecibles |

---

## Reglas de análisis (anti-autoengaño)

1. **Métrica principal: WR vs breakeven**, nunca el P&L a secas.
2. **Breakeven = 1 / (1 + payout)**. Con payout 90 % → breakeven 52.6 %.
3. Todo hallazgo requiere **test de permutación + partición temporal**.
4. La martingala no crea edge — solo amplifica el que exista.
5. Ante cualquier "edge" nuevo: verificar primero que la tasa base del backtester coincide con el mercado crudo (el sesgo de empates fabricó +11 pts falsos).
6. Elegir la mejor config histórica da **peor** resultado que elegir al azar — no optimizar sobre el pasado.

---

## Notas de la API

- `get_candles` se puede colgar sin timeout — el bot incluye watchdog que lo mata con código 3.
- Los activos `-op` (mercado real forex) pagan 90 % y son operables 24×5.
- Los activos `-OTC` son sintéticos: el broker aleatoriza el signo → no predecibles.
- Pares `-op` sin sufijo en `ACTIVES` → usar el escaneo de `activos_operables.json`.
