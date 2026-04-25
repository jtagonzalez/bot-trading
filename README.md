# GOLD 4.0 v3 — Bot de Trading IQ Option

Bot automatizado de opciones binarias con estrategia de análisis técnico, martingala adaptativa, telemetría SQLite y dashboard web en tiempo real.

---

## Requisitos

- Python 3.8+
- Cuenta IQ Option (demo o real)

```bash
pip install numpy pandas mplfinance matplotlib
pip install websocket-client==0.56
pip install -U git+https://github.com/iqoptionapi/iqoptionapi.git
```

---

## Cómo iniciar

```bash
python bot.py
```

El menú ofrece 3 opciones:

| Opción | Acción |
|--------|--------|
| 1 | Iniciar bot + dashboard |
| 2 | Ver estadísticas guardadas |
| 3 | Solo dashboard (sin operar) |

Al elegir opción 1, el bot pregunta:

```
  ¿Cuántas operaciones simultáneas? (1-10):
```

Ingresa un número entre 1 y 10. Se conectan N instancias API independientes y el bot comienza a operar.

---

## Controles durante la ejecución

| Tecla | Acción |
|-------|--------|
| `Ctrl+C` | Detiene el bot completamente y muestra resumen final |
| `Ctrl+Q` | **Pausa** — deja de buscar nuevas señales. Las operaciones en curso terminan su paso actual (martingala no continúa al siguiente paso). Presionar de nuevo para reanudar. |

---

## Configuración (`CONFIG`)

### Credenciales

```python
"email":    "tu_email@gmail.com",
"password": "tu_contraseña",
```

### Cuenta

```python
"tipo_cuenta": "PRACTICE",   # "PRACTICE" = demo | "REAL" = dinero real
```

### Operación

| Parámetro | Valor actual | Descripción |
|-----------|-------------|-------------|
| `monto_base` | `1` | Monto inicial por operación (USD) |
| `expiracion` | `1` | Minutos de duración de cada opción |
| `timeframe` | `60` | Segundos por vela (60 = velas de 1 min) |
| `num_velas` | `200` | Velas descargadas para calcular indicadores |
| `rescan_intervalo` | `15` | Minutos entre re-escaneos de payout |
| `payout_minimo` | `80` | Solo opera activos con payout ≥ este % |

### Martingala

| Parámetro | Valor actual | Descripción |
|-----------|-------------|-------------|
| `martingala_activa` | `True` | Activar / desactivar |
| `multiplicador` | `2.4` | Factor de multiplicación tras cada pérdida |
| `max_pasos` | `4` | Máximo de entradas por ciclo |

**Progresión con monto base $1 y multiplicador x2.4 (max 4 pasos):**

| Paso | Monto | Pérdida acumulada si falla todo |
|------|-------|---------------------------------|
| 1 | $1.00 | -$1.00 |
| 2 | $2.40 | -$3.40 |
| 3 | $5.76 | -$9.16 |
| 4 | $13.82 | **-$22.98** |

> **Por qué 4 pasos:** análisis de 135 ciclos reales mostró que max_pasos=4 produce +$26.83 neto vs -$17.63 con 5 pasos y -$30.37 con 6 pasos. Los pasos 5 y 6 destruyen rentabilidad por exposición desproporcionada al riesgo.

### Gestión de riesgo

| Parámetro | Valor actual | Descripción |
|-----------|-------------|-------------|
| `max_perdidas_diarias` | `9999` | Sin límite activo (cuenta práctica) |
| `max_operaciones_dia` | `9999` | Sin límite activo |
| `ganancia_objetivo_dia` | `0` | Sin límite (0 = desactivado) |

---

## Estrategia — Indicadores y Condiciones

### Indicadores

| Indicador | Parámetro | Valor | Función |
|-----------|-----------|-------|---------|
| SMA rápida | `sma_rapida` | **3 periodos** | Sigue el precio de cerca; referencia de posición de la vela |
| SMA tendencia | `sma_tendencia` | **50 periodos** | Define la dirección macro del mercado |

### Filtros de calidad de vela

| Filtro | Parámetro | Valor | Descripción |
|--------|-----------|-------|-------------|
| Cuerpo mínimo | `porcentaje_cuerpo` | **60%** | El cuerpo debe estar al menos 60% sobre/bajo SMA(3) |
| Mechas máximas | `max_mecha_ratio` | **100%** | La suma de mechas no puede superar 1x el cuerpo de la vela |
| Inclinación SMA50 | `velas_inclinacion` | **3 velas** | La SMA(50) debe estar subiendo/bajando en las últimas 3 velas |
| Separación SMAs | `separacion_minima` | **0.0000** | Sin mínimo de separación entre SMA3 y SMA50 |

### Condiciones de entrada CALL (compra)

Todas deben cumplirse simultáneamente:

1. **Tendencia alcista** — SMA(50) mayor que hace 3 velas
2. **SMA(3) sobre SMA(50)** — línea rápida por encima de la tendencia
3. **SMAs separadas** — distancia entre ambas > separacion_minima
4. **Vela alcista** — cierre mayor que apertura (vela verde)
5. **Cuerpo sobre SMA(3)** — al menos 60% del cuerpo está por encima de SMA(3)
6. **Mechas controladas** — suma mechas < 100% del cuerpo
7. **Pullback** — al menos 1 vela bajista en las últimas 5 velas anteriores
8. **Sin cruce reciente** — SMA(50) no tocó el rango (low-high) de ninguna vela en las últimas 15
9. **Sin vela roja dominante** — ninguna vela bajista de las últimas 5 tiene más del 50% de su cuerpo bajo SMA(3)

### Condiciones de entrada PUT (venta)

Espejo exacto de CALL:

1. SMA(50) bajando en las últimas 3 velas
2. SMA(3) por debajo de SMA(50)
3. SMAs separadas
4. Vela bajista (vela roja)
5. Al menos 60% del cuerpo por debajo de SMA(3)
6. Suma mechas < 100% del cuerpo
7. Al menos 1 vela alcista en las últimas 5 (pullback)
8. Sin cruce reciente de SMA(50) en últimas 15 velas
9. Sin vela verde dominante en últimas 5

---

## Paralelismo — Cómo opera en simultáneo

- Al iniciar se abren **N conexiones API independientes** (Pool), una por operación simultánea
- Cada ciclo de trading corre en su propio hilo con su propia conexión WebSocket
- El semáforo limita cuántos ciclos corren a la vez (el número que ingresas al inicio)
- El resultado de cada opción se recibe por `socket_option_closed` de IQ Option (~60 segundos)

---

## Imágenes de operaciones

Antes de entrar a cada operación se genera automáticamente un gráfico de velas con SMA3 y SMA50.

- **Carpeta:** `capturas_2p/CALL/` y `capturas_2p/PUT/`
- **Nombre inicial:** `CALL_0001_ACTIVO.png` / `PUT_0001_ACTIVO.png`
- **Al cerrar el ciclo:** se renombra agregando `_WIN` o `_LOSS` al final

Estas imágenes sirven para análisis visual de entradas y construcción de datasets de entrenamiento.

---

## Base de datos SQLite (`gold_bot.db`)

El bot guarda cada ciclo completo en las siguientes tablas:

### Tabla `runs`

Una fila por arranque del bot. Permite separar corridas con distintas configuraciones para comparación directa.

| Campo | Descripción |
|-------|-------------|
| `id` | ID del run |
| `timestamp_inicio` | Hora de inicio |
| `monto_base` | Monto base usado |
| `multiplicador` | Multiplicador de martingala |
| `max_pasos` | Pasos máximos configurados |
| `max_mecha_ratio` | Filtro de mechas usado |
| `pct_cuerpo_min` | Umbral mínimo de cuerpo |
| `payout_minimo` | Payout mínimo requerido |
| `activos_excluidos` | JSON con activos bloqueados |
| `notas` | Notas del run |

### Tabla `ciclos`

Una fila por ciclo completo (señal inicial + todas sus martingalas).

| Campo | Descripción |
|-------|-------------|
| `id` | ID secuencial del ciclo |
| `activo` | Par operado (ej. GBPNZD-OTC) |
| `direccion` | call / put |
| `timestamp_inicio` | Hora de apertura del ciclo |
| `timestamp_fin` | Hora de cierre |
| `pasos_usados` | Cuántas martingalas se ejecutaron (1 = ganó directo) |
| `resultado_final` | win / loss / cancelado |
| `ganancia_neta` | P/L neto del ciclo en USD |
| `monto_inicial` | Monto del paso 1 |
| `monto_maximo` | Monto del último paso ejecutado |
| `payout_inicial` | Payout del activo al iniciar |
| `run_id` | Referencia al run |

### Tabla `operaciones`

Una fila por ciclo con datos técnicos de la vela señal.

| Campo adicional | Descripción |
|----------------|-------------|
| `sma3` | Valor de SMA(3) en la vela señal |
| `sma50` | Valor de SMA(50) en la vela señal |
| `pct_cuerpo` | % del cuerpo sobre/bajo SMA(3) |
| `ratio_mechas` | Suma mechas / cuerpo |
| `pasos_json` | JSON con detalle de cada paso (monto, resultado, ganancia) |
| `run_id` | Referencia al run |

### Tabla `indicadores`

Una fila por ciclo con métricas de la señal para análisis de estrategia.

| Campo | Descripción |
|-------|-------------|
| `ciclo_id` | Referencia al ciclo |
| `run_id` | Referencia al run |
| `sma3` / `sma50` | Valores de las medias en la señal |
| `distancia_smas` | Separación absoluta entre SMA3 y SMA50 |
| `pct_cuerpo` | % del cuerpo sobre/bajo SMA3 |
| `ratio_mechas` | Ratio mechas/cuerpo |
| `payout` | Payout del activo en esa entrada |
| `gano_p1` | 1 si ganó en paso 1 sin martingala |
| `gano_p2` | 1 si ganó en paso 2 (primera martingala) |

### Tabla `escaneos`

Registro de cada escaneo de payout realizado.

---

## Análisis de rentabilidad por pasos (135 ciclos históricos)

| Max pasos | Win Rate | Neto $ | Pérd. máx/ciclo |
|-----------|----------|--------|-----------------|
| 1 | 55.6% | +$4.01 | -$1.00 |
| 2 | 83.0% | +$23.29 | -$3.40 |
| 3 | 91.1% | +$12.74 | -$9.16 |
| **4** | **96.3%** | **+$26.83** | **-$22.98** |
| 5 | 97.8% | -$17.63 | -$56.16 |
| 6 | 98.5% | -$30.37 | -$135.79 |

> Configuración actual: **max_pasos = 4** (mejor balance riesgo/rentabilidad)

---

## Tabla en pantalla (por vela)

```
  ────────────────────────────────────────────────────────────────────────────────
     #      Hora  Activo                  Dir  Pasos  Monto$    Neto$   Res
  ────────────────────────────────────────────────────────────────────────────────
   121  18:39:01  GBPNZD-OTC             PUT       1    1.00    +0.83  ✅
   122  18:41:05  EURNZD-OTC            CALL       4   13.82    -9.16  💀
  ────────────────────────────────────────────────────────────────────────────────
```

---

## Dashboard web

Con el bot corriendo, abrir en el navegador:

```
http://localhost:8080/dashboard.html
```

Se actualiza automáticamente cada 5 segundos. Muestra balance, win rate, P/L, rachas, rendimiento por activo y análisis por paso de martingala.

---

## Archivos del proyecto

| Archivo | Descripción |
|---------|-------------|
| `bot.py` | Bot principal |
| `gold_bot.db` | Base de datos SQLite con todos los ciclos |
| `telemetria.json` | Historial de operaciones (formato JSON) |
| `estadisticas.json` | Resumen estadístico para el dashboard |
| `bot_log.txt` | Log completo de la sesión en texto |
| `capturas_2p/CALL/` | Imágenes de entradas CALL (renombradas con _WIN/_LOSS) |
| `capturas_2p/PUT/` | Imágenes de entradas PUT (renombradas con _WIN/_LOSS) |
| `README.md` | Este archivo |

---

## Advertencias

- Usar siempre cuenta PRACTICE antes de operar con dinero real
- La martingala de 4 pasos puede perder $22.98 por ciclo con monto base $1
- La API de IQ Option es no oficial y puede dejar de funcionar sin aviso
- No es consejo financiero
