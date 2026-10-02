# Levantar los bots

## Opción 1 — Todo de un golpe

```sh
sh levantar_todo.sh
```

Lanza los 4 supervisores y el panel en segundo plano. Los PIDs quedan en `$TEMP/bots_pids.txt`.

```sh
sh levantar_todo.sh --sin-panel   # solo los bots, sin panel
```

---

## Opción 2 — Bot por bot

Abrir una terminal **Git Bash** en la carpeta del proyecto para cada uno:

```sh
# EURUSD 15m (ML)
sh supervisar.sh

# Cripto 15m (ML) — BTC y ETH, 24×7
sh supervisar_cripto.sh

# DeMarker 1m — cripto
sh supervisar_demarker.sh

# Weis Wave + Bollinger — forex -op
sh supervisar_weis.sh

# Panel unificado
py panel.py --db live.db
# Abre http://localhost:8090
```

Cada supervisor se queda corriendo en esa terminal. Si el bot se cuelga o cae con código ≠ 0, el supervisor lo reinicia tras 20 s.

---

## Verificar que están corriendo

```sh
# Estado de los supervisores Python (weis y demarker)
py supervisar.py --estado

# Verificar que el panel responde
curl http://localhost:8090/api/estado
```

El panel muestra un punto **verde** (●) junto al nombre del bot si recibió actividad en los últimos 40 min.

---

## Dónde quedan los logs

| Bot | Log |
|-----|-----|
| EURUSD | `$TEMP/live_supervisado.log` |
| Cripto | `$TEMP/live_cripto.log` |
| DeMarker | `$TEMP/live_demarker.log` |
| Weis | `$TEMP/live_weis.log` |
| Panel | `$TEMP/panel.log` |

`$TEMP` en Windows = `C:\Users\<usuario>\AppData\Local\Temp`

---

## Primer arranque (entorno limpio)

```sh
# Instalar dependencias
pip install -r requirements.txt

# Verificar conexión con IQ Option
py analisis/test_conexion.py

# Arrancar
sh levantar_todo.sh
```

---

## Notas

- Los bots usan cuenta **PRACTICE** por defecto. Para cambiar a real hay que modificar `--tipo-cuenta` en el `.sh` correspondiente.
- `--reanudar` en cada supervisor hace que los reinicios no fragmenten los datos: el `run` en `live.db` sigue siendo el mismo mientras el modelo y las notas no cambien.
- La API de IQ Option incluye un watchdog: si el bucle principal deja de ejecutarse más de `--watchdog-seg` segundos, el bot se mata a sí mismo (código 3) para que el supervisor lo reinicie limpio.
