# Detener los bots

## Opción 1 — Todo de un golpe

```sh
sh detener_todo.sh
```

Lee los PIDs guardados por `levantar_todo.sh` y mata cada supervisor con su árbol de procesos.

---

## Opción 2 — Parada limpia por bot

Enviar una señal limpia al supervisor de cada bot. El bot termina ordenadamente (guarda el estado, cierra la conexión) y el supervisor no lo reinicia.

```sh
py supervisar.py --parar weis
py supervisar.py --parar demarker
```

Para EURUSD y cripto (que usan los `.sh` directos), Ctrl+C en la terminal donde corren, o:

```sh
taskkill /F /T /PID <pid_del_supervisor>
```

El PID está en `$TEMP/bots_pids.txt` o en el archivo de candado `$TEMP/supervisor_weis.json`.

---

## Verificar que pararon

```sh
py supervisar.py --estado
# debe mostrar "sin supervisor" para weis y demarker

netstat -ano | findstr :8090
# sin resultado = panel detenido
```

---

## Parar solo el panel

```sh
# Windows PowerShell
Stop-Process -Id (Get-NetTCPConnection -LocalPort 8090).OwningProcess -Force

# Git Bash
kill $(lsof -t -i:8090)
```

---

## Emergencia — matar todo lo Python

Solo si algo quedó colgado y no responde a los métodos anteriores:

```sh
# PowerShell — mata TODOS los procesos py.exe / python.exe
Get-Process -Name "py","python" | Stop-Process -Force
```

> Esto también detiene cualquier otro script Python que estuviera corriendo.

---

## Notas

- Matar el bot directamente (sin matar el supervisor primero) hace que el supervisor lo reinicie en 20 s. **Siempre matar el supervisor primero**, o usar `--parar`.
- Si el bot termina con código 0 (parada limpia), el supervisor también termina y no reinicia.
- Los datos en `live.db` no se pierden: la base de datos sobrevive cualquier tipo de cierre, incluso un kill forzoso (WAL mode).
