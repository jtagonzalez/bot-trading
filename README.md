# 🏆 GOLD 4.0 v3 — Bot de Trading para IQ Option

Bot automatizado de opciones binarias con estrategia basada en análisis técnico, sistema de martingala, telemetría completa y dashboard visual en tiempo real.

---

## 📋 Requisitos Previos

- **Python 3.8+** → [Descargar Python](https://www.python.org/downloads/)
- **pip** (viene incluido con Python)
- **Git** (para instalar la API de IQ Option)
- **Cuenta en IQ Option** (demo o real)

### Verificar instalación

```bash
python --version    # Debe ser 3.8 o superior
pip --version
git --version
```

---

## 🚀 Instalación Paso a Paso

### 1. Crear una carpeta para el proyecto

```bash
mkdir gold-bot
cd gold-bot
```

### 2. (Recomendado) Crear un entorno virtual

```bash
python -m venv venv

# Activar en Windows:
venv\Scripts\activate

# Activar en Mac/Linux:
source venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

Si hay problemas con la API de IQ Option, instálala manualmente:

```bash
pip install numpy pandas
pip install -U git+https://github.com/iqoptionapi/iqoptionapi.git
```

### 4. Colocar los archivos

Asegúrate de tener estos archivos en la carpeta del proyecto:

```
gold-bot/
├── gold_bot_v3.py          # Bot principal
├── gold_dashboard.jsx      # Dashboard visual (para usar en Claude.ai)
├── test_conexion.py        # Script para probar la conexión
├── requirements.txt        # Dependencias
└── README.md               # Este archivo
```

### 5. Fijar versión de websocket-client (IMPORTANTE)

La API de IQ Option requiere una versión específica de `websocket-client`. Si no haces esto, la conexión puede fallar silenciosamente:

```bash
pip uninstall websocket-client
pip install websocket-client==0.56
```

### 6. Verificar que todo está instalado

```bash
python -c "from iqoptionapi.stable_api import IQ_Option; print('✅ API instalada correctamente')"
```

Si ves `✅ API instalada correctamente`, estás listo. Si da error, revisa la sección de Solución de Problemas al final.

---

## 🔌 Conexión a la API de IQ Option

La API se conecta directamente a los servidores de IQ Option usando WebSocket. No necesitas crear tokens ni API keys — usas tu email y contraseña de tu cuenta normal de IQ Option.

### Paso 1: Crear cuenta en IQ Option

Si no tienes cuenta:

1. Ve a [iqoption.com](https://iqoption.com)
2. Regístrate con email y contraseña
3. Puedes usar la cuenta demo (viene con $10,000 virtuales) sin necesidad de depositar dinero real

### Paso 2: Configurar credenciales en el bot

Abre `gold_bot_v3.py` y edita estas líneas:

```python
"email": "tu_email_real@gmail.com",     # El email con el que te registraste
"password": "tu_contraseña_real",        # Tu contraseña de IQ Option
```

### Paso 3: Verificación en dos pasos (2FA)

Si tienes 2FA activado en tu cuenta de IQ Option (verificación por SMS), el bot lo maneja automáticamente:

1. Al conectar, la API detecta que tienes 2FA
2. IQ Option envía un SMS con un código a tu teléfono
3. El bot te pide que ingreses el código en la terminal
4. Se completa la conexión

> 💡 **Consejo:** Si vas a dejar el bot corriendo sin supervisión, considera desactivar 2FA temporalmente desde la configuración de seguridad de IQ Option, ya que el bot se detendrá esperando el código SMS si la conexión se reinicia.

### Paso 4: Probar la conexión

Antes de usar el bot completo, prueba que la conexión funciona con este script rápido:

```python
from iqoptionapi.stable_api import IQ_Option
import json

print("Conectando...")
api = IQ_Option("tu_email@gmail.com", "tu_contraseña")
status, reason = api.connect()

# Manejar 2FA si está activado
if reason == "2FA":
    print("2FA detectado — revisa tu SMS")
    codigo = input("Ingresa el código SMS: ")
    status, reason = api.connect_2fa(codigo)

if status:
    print(f"✅ Conectado exitosamente")
    api.change_balance("PRACTICE")  # Cambiar a cuenta demo
    print(f"💰 Balance demo: ${api.get_balance():.2f}")
else:
    print(f"❌ Error: {reason}")
```

Guárdalo como `test_conexion.py` y ejecútalo:

```bash
python test_conexion.py
```

### Posibles resultados de conexión

| Status | Reason | Significado | Solución |
|--------|--------|-------------|----------|
| `True` | `None` | Conexión exitosa | Todo bien |
| `False` | `"2FA"` | Verificación en dos pasos | Ingresa código SMS |
| `False` | `invalid_credentials` | Email o contraseña incorrectos | Verifica tus datos |
| `False` | Timeout/error de red | Problema de internet | Revisa tu conexión |

### Notas importantes sobre la conexión

- **No abras IQ Option en el navegador al mismo tiempo** — puede causar conflictos de sesión y desconectar el bot
- **La API se reconecta automáticamente** si se pierde la conexión momentáneamente
- **Horarios de activos:** Los activos regulares (EURUSD, GBPUSD, etc.) solo están disponibles en horario de mercado. Los activos OTC (EURUSD-OTC, etc.) están disponibles 24/7 incluyendo fines de semana
- **La API es no oficial** — es mantenida por la comunidad. Funciona bien pero IQ Option podría bloquearla en cualquier momento

---

## ⚙️ Configuración del Bot

Abre `gold_bot_v3.py` y edita la sección `CONFIG`:

### Tipo de cuenta

```python
"tipo_cuenta": "PRACTICE",   # Usa "PRACTICE" para demo, "REAL" para real
```

> ⚠️ **SIEMPRE empieza con cuenta PRACTICE (demo).** No uses cuenta real hasta que hayas probado extensamente.

### Parámetros de operación

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `monto_base` | `1` | Monto inicial por operación (USD) |
| `expiracion` | `1` | Duración de la operación en minutos |
| `timeframe` | `60` | Temporalidad de las velas en segundos |
| `payout_minimo` | `80` | Solo opera activos con payout > este % |

### Martingala

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `martingala_activa` | `True` | Activar/desactivar martingala |
| `multiplicador` | `2.4` | Factor de multiplicación tras pérdida |
| `max_pasos` | `6` | Máximo de entradas seguidas (1 original + 5 recuperación) |

**Ejemplo de progresión con monto base $1:**

| Paso | Monto | Acumulado invertido |
|------|-------|---------------------|
| 1 | $1.00 | $1.00 |
| 2 | $2.40 | $3.40 |
| 3 | $5.76 | $9.16 |
| 4 | $13.82 | $22.98 |
| 5 | $33.18 | $56.16 |
| 6 | $79.63 | $135.79 |

> ⚠️ **La martingala es agresiva.** Con 6 pasos y multiplicador x2.4, una racha de 6 pérdidas consecutivas puede consumir $135+ del balance. Ajusta el `monto_base` según tu capital.

### Gestión de riesgo

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `max_perdidas_diarias` | `10` | Detiene el bot tras N pérdidas en el día |
| `max_operaciones_dia` | `50` | Máximo de operaciones por día |
| `ganancia_objetivo_dia` | `50` | Meta diaria en USD (0 = sin límite) |

---

## ▶️ Cómo Usar

### Opción 1: Bot completo + Dashboard

```bash
python gold_bot_v3.py
```

Selecciona la opción `1`. El bot:

1. Se conecta a IQ Option
2. Inicia el servidor del dashboard en `http://localhost:8080`
3. Escanea todos los activos buscando payout > 80%
4. Analiza velas en busca de señales
5. Ejecuta operaciones automáticamente
6. Aplica martingala si hay pérdida

### Opción 2: Ver estadísticas guardadas

```bash
python gold_bot_v3.py
```

Selecciona la opción `2` para ver el resumen de operaciones anteriores.

### Opción 3: Solo Dashboard

```bash
python gold_bot_v3.py
```

Selecciona la opción `3` para iniciar solo el servidor web del dashboard sin el bot.

### Detener el bot

Presiona `Ctrl + C` en la terminal. El bot mostrará un resumen final de la sesión.

---

## 📊 Dashboard Visual

El dashboard se puede usar de dos formas:

### Opción A: Dashboard web (con el bot corriendo)

Cuando el bot está activo, abre en tu navegador:

```
http://localhost:8080/dashboard.html
```

Se actualiza automáticamente cada 5 segundos.

### Opción B: Dashboard en Claude.ai

El archivo `gold_dashboard.jsx` es un componente React que puedes visualizar directamente en Claude.ai. Muestra datos de demostración cuando no hay conexión al bot.

### Qué muestra el dashboard

- **Operaciones en tiempo real** — cada trade con su resultado
- **Win Rate** — anillo animado con porcentaje de aciertos
- **Balance y P/L** — con mini gráficos de tendencia
- **Análisis de martingala** — aciertos por paso
- **Rendimiento por activo** — qué pares generan más ganancia
- **Racha actual** — rachas de victorias y derrotas
- **Estadísticas globales** — mejor día, peor día, promedio

---

## 📈 Estrategia (Cómo Funciona)

### Indicadores

- **SMA(3)** — línea verde, sigue el precio de cerca
- **SMA(50)** — línea cian, marca la tendencia general

### Condiciones de COMPRA (CALL)

1. SMA(50) con inclinación alcista (subiendo en las últimas 3 velas)
2. SMA(3) por encima de SMA(50) y separada
3. Vela alcista (verde, cierre > apertura)
4. 95% o más del cuerpo de la vela está por encima de la SMA(3)
5. La suma de mechas (superior + inferior) es menor al 50% del cuerpo

### Condiciones de VENTA (PUT)

Lo opuesto a las condiciones de compra:

1. SMA(50) bajando
2. SMA(3) por debajo de SMA(50) y separada
3. Vela bajista (roja, cierre < apertura)
4. 95% o más del cuerpo debajo de la SMA(3)
5. Mechas sumadas < 50% del cuerpo

### Ejecución

- La señal se detecta al cerrar una vela
- La operación se ejecuta al **abrir la siguiente vela**
- Si se pierde, la martingala entra inmediatamente en la siguiente vela

---

## 📁 Archivos Generados

El bot crea estos archivos automáticamente:

| Archivo | Descripción |
|---------|-------------|
| `telemetria.json` | Historial completo de cada operación |
| `estadisticas.json` | Resumen estadístico (leído por el dashboard) |

### Formato de telemetría

Cada operación guarda:

```json
{
  "id": 1,
  "timestamp": "2025-06-15T14:32:00",
  "activo": "EURUSD-OTC",
  "direccion": "call",
  "monto": 1.00,
  "payout": 87,
  "resultado": "win",
  "ganancia": 0.87,
  "paso_martingala": 1,
  "balance_despues": 1034.80,
  "sma3": 1.10023,
  "sma50": 1.09985,
  "pct_cuerpo": 0.97,
  "ratio_mechas": 0.32,
  "close": 1.10045
}
```

---

## 🛡️ Seguridad y Advertencias

- **Nunca compartas tu archivo `gold_bot_v3.py`** con las credenciales configuradas
- **La API de IQ Option es no oficial** — puede dejar de funcionar si IQ Option cambia su plataforma
- **El trading conlleva riesgo de pérdida** — no inviertas dinero que no puedas perder
- **La martingala es peligrosa** — una mala racha puede consumir el balance rápidamente
- **Resultados pasados no garantizan resultados futuros**
- **Prueba siempre en cuenta demo** antes de usar dinero real

---

## 🔧 Solución de Problemas

### "Error de conexión"

- Verifica que tu email y contraseña son correctos
- Asegúrate de que no tienes la plataforma IQ Option abierta en el navegador (pueden conflictuar)
- Revisa tu conexión a internet

### "No hay activos con payout suficiente"

- Puede ocurrir fuera del horario de mercado
- Los activos OTC suelen estar disponibles 24/7
- Intenta bajar el `payout_minimo` a 75 temporalmente

### "pip install falla con la API"

```bash
# Intenta con pip3 en lugar de pip
pip3 install -U git+https://github.com/iqoptionapi/iqoptionapi.git

# Si falla Git, descarga manualmente:
# 1. Ve a https://github.com/iqoptionapi/iqoptionapi
# 2. Descarga como ZIP
# 3. Descomprime y ejecuta:
cd iqoptionapi-master
pip install .
```

### "ModuleNotFoundError: No module named 'iqoptionapi'"

```bash
# Asegúrate de que el entorno virtual está activado
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# Reinstala
pip install -U git+https://github.com/iqoptionapi/iqoptionapi.git
```

### El dashboard no carga

- Verifica que el puerto 8080 no esté en uso
- Cambia `dashboard_port` en CONFIG si es necesario
- Abre directamente `http://localhost:8080/dashboard.html`

---

## 📝 Personalización

### Cambiar temporalidad

Para velas de 5 minutos:

```python
"timeframe": 300,     # 5 min en segundos
"expiracion": 5,      # 5 min de expiración
```

### Desactivar martingala

```python
"martingala_activa": False,
```

### Operar solo ciertos activos

Modifica el método `obtener_activos_rentables()` para filtrar por nombre:

```python
# Ejemplo: solo pares con USD
if "USD" in activo and payout >= CONFIG["payout_minimo"]:
    activos_buenos.append(...)
```

### Ajustar sensibilidad de la estrategia

- Bajar `porcentaje_cuerpo` de 0.95 a 0.90 → más señales, menos filtradas
- Subir `max_mecha_ratio` de 0.50 a 0.70 → acepta velas con mechas más grandes
- Cambiar `velas_inclinacion` de 3 a 5 → exige tendencia más confirmada

---

## 📜 Licencia

Este software es para uso personal y educativo. No es consejo financiero. Úsalo bajo tu propio riesgo.
