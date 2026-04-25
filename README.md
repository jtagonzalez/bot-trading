# GOLD 4.0 v3 — Bot de Trading IQ Option

Bot automatizado de opciones binarias con estrategia de análisis técnico, martingala adaptativa, telemetría SQLite y dashboard web en tiempo real.

---

## Requisitos

- Python 3.8+
- Cuenta IQ Option (demo o real)

```bash
pip install numpy pandas
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

Ingresa un número entre 1 y 10. Luego se conectan N instancias API independientes y el bot comienza a operar.

---

## Controles durante la ejecución

| Tecla | Acción |
|-------|--------|
| `Ctrl+C` | Detiene el bot completamente y muestra resumen final |
| `Ctrl+P` | **Pausa** — deja de buscar nuevas señales. Las operaciones en curso terminan su paso actual (martingala no continúa al siguiente paso). Presionar de nuevo para reanudar. |

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
| `num_velas` | `200` | Velas que se descargan para calcular indicadores |
| `rescan_intervalo` | `15` | Minutos entre re-escaneos de payout |
| `payout_minimo` | `80` | Solo opera activos con payout ≥ este % |

### Martingala

| Parámetro | Valor actual | Descripción |
|-----------|-------------|-------------|
| `martingala_activa` | `True` | Activar / desactivar |
| `multiplicador` | `2.4` | Factor de multiplicación tras cada pérdida |
| `max_pasos` | `6` | Máximo de entradas por ciclo |

**Progresión con monto base $1 y multiplicador x2.4:**

| Paso | Monto | Pérdida acumulada si falla todo |
|------|-------|---------------------------------|
| 1 | $1.00 | -$1.00 |
| 2 | $2.40 | -$3.40 |
| 3 | $5.76 | -$9.16 |
| 4 | $13.82 | -$22.98 |
| 5 | $33.18 | -$56.16 |
| 6 | $79.63 | **-$135.79** |

### Gestión de riesgo

| Parámetro | Valor actual | Descripción |
|-----------|-------------|-------------|
| `max_perdidas_diarias` | `9999` | Sin límite activo (cuenta práctica) |
| `max_operaciones_dia` | `9999` | Sin límite activo |
| `ganancia_objetivo_dia` | `0` | Sin límite (0 = desactivado) |

---

## Estrategia — Indicadores y Condiciones

### Indicadores utilizados

| Indicador | Parámetro | Valor | Función |
|-----------|-----------|-------|---------|
| SMA rápida | `sma_rapida` | **3 periodos** | Sigue el precio de cerca; referencia de posición de la vela |
| SMA tendencia | `sma_tendencia` | **50 periodos** | Define la dirección macro del mercado |

### Filtros de calidad de vela

| Filtro | Parámetro | Valor | Descripción |
|--------|-----------|-------|-------------|
| Cuerpo mínimo | `porcentaje_cuerpo` | **60%** | El cuerpo de la vela debe estar al menos 60% por encima/debajo de la SMA(3) |
| Mechas máximas | `max_mecha_ratio` | **200%** | La suma de mechas no puede superar 2x el cuerpo de la vela |
| Inclinación SMA50 | `velas_inclinacion` | **3 velas** | La SMA(50) debe estar subiendo/bajando en las últimas 3 velas |
| Separación SMAs | `separacion_minima` | **0.0000** | Sin mínimo de separación entre SMA3 y SMA50 |

### Condiciones de entrada CALL (compra)

Todas deben cumplirse simultáneamente:

1. **Tendencia alcista** — SMA(50) mayor que hace 3 velas
2. **SMA(3) sobre SMA(50)** — línea rápida por encima de la tendencia
3. **SMAs separadas** — distancia entre ambas > separacion_minima
4. **Vela alcista** — cierre mayor que apertura (vela verde)
5. **Cuerpo sobre SMA(3)** — al menos 60% del cuerpo está por encima de SMA(3)
6. **Mechas controladas** — suma mechas < 200% del cuerpo
7. **Pullback** — al menos 1 vela bajista en las últimas 5 velas anteriores
8. **Sin cruce reciente** — SMA(50) no tocó el rango (low-high) de ninguna vela en las últimas 15
9. **Sin vela roja dominante** — ninguna vela bajista de las últimas 5 tiene más del 50% de su cuerpo bajo SMA(3)

### Condiciones de entrada PUT (venta)

Espejo de CALL:

1. SMA(50) bajando en las últimas 3 velas
2. SMA(3) por debajo de SMA(50)
3. SMAs separadas
4. Vela bajista (vela roja)
5. Al menos 60% del cuerpo por debajo de SMA(3)
6. Suma mechas < 200% del cuerpo
7. Al menos 1 vela alcista en las últimas 5 (pullback)
8. Sin cruce reciente de SMA(50) en últimas 15 velas
9. Sin vela verde dominante en últimas 5

---

## Paralelismo — Cómo opera en simultáneo

- Al iniciar se abren **N conexiones API independientes** (Pool), una por operación simultánea
- Cada ciclo de trading corre en su propio hilo con su propia conexión WebSocket
- El semáforo limita cuántos ciclos corren a la vez (el número que ingresas al inicio)
- El resultado de cada opción se recibe por `socket_option_closed` de IQ Option (60 segundos después de colocar la orden)

---

## Base de datos SQLite (`gold_bot.db`)

El bot guarda cada ciclo completo en 3 tablas:

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

### Tabla `operaciones`

Una fila por ciclo (misma info que `ciclos` pero con datos técnicos de la vela señal).

| Campo adicional | Descripción |
|----------------|-------------|
| `sma3` | Valor de SMA(3) en la vela señal |
| `sma50` | Valor de SMA(50) en la vela señal |
| `pct_cuerpo` | % del cuerpo sobre/bajo SMA(3) |
| `ratio_mechas` | Suma mechas / cuerpo |
| `pasos_json` | JSON con detalle de cada paso (monto, resultado, ganancia) |

### Tabla `escaneos`

Registro de cada escaneo de payout realizado.

---

## Tabla en pantalla (por vela)

Después de cada vela escaneada se imprime una tabla con todos los ciclos del día:

```
  ────────────────────────────────────────────────────────────────────────────────
     #      Hora  Activo                  Dir  Pasos  Monto$    Neto$   Res
  ────────────────────────────────────────────────────────────────────────────────
   121  18:39:01  GBPNZD-OTC             PUT       1    3.24    +0.83  ✅
   122  18:41:05  EURNZD-OTC            CALL       4   13.82    -9.16  💀
  ────────────────────────────────────────────────────────────────────────────────
```

---

## Activos excluidos

Los siguientes activos están bloqueados permanentemente por haber perdido 6 pasos consecutivos en 2 o más ocasiones:

| Activo | Motivo |
|--------|--------|
| SNDK-OTC | 2 pérdidas completas de 6 pasos |
| SANDUSD-OTC | 2 pérdidas completas de 6 pasos |
| IMXUSD-OTC | 2 pérdidas completas de 6 pasos |
| EURUSD-OTC | 2 pérdidas completas de 6 pasos |
| CHFNOK-OTC | 2 pérdidas completas de 6 pasos |

Para agregar o quitar activos de la lista, editar `ACTIVOS_EXCLUIDOS` al inicio de `bot.py`.

---

## Archivos del proyecto

| Archivo | Descripción |
|---------|-------------|
| `bot.py` | Bot principal |
| `gold_bot.db` | Base de datos SQLite con todos los ciclos |
| `telemetria.json` | Historial de operaciones (formato JSON) |
| `estadisticas.json` | Resumen estadístico para el dashboard |
| `bot_log.txt` | Log completo de la sesión en texto |
| `README.md` | Este archivo |

---

## Dashboard web

Con el bot corriendo, abrir en el navegador:

```
http://localhost:8080/dashboard.html
```

Se actualiza automáticamente cada 5 segundos. Muestra balance, win rate, P/L, rachas, rendimiento por activo y análisis por paso de martingala.

---

## Advertencias

- Usar siempre cuenta PRACTICE antes de operar con dinero real
- La martingala de 6 pasos puede perder $135.79 por ciclo con monto base $1
- La API de IQ Option es no oficial y puede dejar de funcionar sin aviso
- No es consejo financiero
