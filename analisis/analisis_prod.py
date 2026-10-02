import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from collections import defaultdict

conn = sqlite3.connect('gold_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""SELECT activo, direccion, timestamp_inicio, pasos_usados,
               monto_maximo, ganancia_neta, resultado_final
               FROM ciclos WHERE run_id=16 AND resultado_final IN ('win','loss')
               ORDER BY id""")
ciclos = [dict(r) for r in cur.fetchall()]
conn.close()

# 1. Stop por objetivo de ganancia
print("=== SIMULACION STOP POR OBJETIVO NETO ===")
for objetivo in [5, 10, 15, 20, 30, 50]:
    neto = 0
    ops = 0
    hora_stop = None
    for c in ciclos:
        neto += c['ganancia_neta']
        ops += 1
        if neto >= objetivo:
            hora_stop = c['timestamp_inicio'][11:16]
            break
    if hora_stop:
        print(f"  Objetivo ${objetivo:>3}: alcanzado en {ops:>4} ops | hora: {hora_stop} | neto: ${neto:.2f}")
    else:
        print(f"  Objetivo ${objetivo:>3}: NO alcanzado en esta sesion")

# 2. Horarios
print()
print("=== HORARIOS (5+ ops) ===")
horas = defaultdict(lambda: {'n':0,'wins':0,'neto':0,'losses':0})
for c in ciclos:
    h = int(c['timestamp_inicio'][11:13])
    horas[h]['n'] += 1
    if c['resultado_final'] == 'win':
        horas[h]['wins'] += 1
        horas[h]['neto'] += c['ganancia_neta']
    else:
        horas[h]['losses'] += 1
        horas[h]['neto'] += c['ganancia_neta']

for h in sorted(horas):
    d = horas[h]
    if d['n'] < 5:
        continue
    wr = d['wins'] / d['n'] * 100
    marca = "<<< NEGATIVO" if d['losses'] > 0 else "OK"
    print(f"  {h:02d}h  {d['n']:>3} ops  WR:{wr:>5.1f}%  Neto:${d['neto']:>8.2f}  {d['losses']} losses  {marca}")

# 3. Activos solidos
print()
print("=== ACTIVOS SOLIDOS (3+ ops, sin losses) ===")
activos = defaultdict(lambda: {'n':0,'wins':0,'neto':0,'losses':0})
for c in ciclos:
    a = c['activo']
    activos[a]['n'] += 1
    if c['resultado_final'] == 'win':
        activos[a]['wins'] += 1
        activos[a]['neto'] += c['ganancia_neta']
    else:
        activos[a]['losses'] += 1
        activos[a]['neto'] += c['ganancia_neta']

top = [(a, d) for a, d in activos.items() if d['n'] >= 3 and d['losses'] == 0]
top.sort(key=lambda x: x[1]['neto'], reverse=True)
for a, d in top:
    print(f"  {a:<25} {d['n']:>3} ops  WR:100%  Neto:${d['neto']:>6.2f}")

print()
print("=== ACTIVOS A EXCLUIR (tuvieron losses) ===")
malos = [(a, d) for a, d in activos.items() if d['losses'] > 0]
malos.sort(key=lambda x: x[1]['neto'])
for a, d in malos:
    wr = d['wins'] / d['n'] * 100
    print(f"  {a:<25} {d['n']:>3} ops  WR:{wr:.0f}%  Neto:${d['neto']:>8.2f}  ({d['losses']} loss)")

# 4. Simulacion con monto base $5 (produccion)
print()
print("=== PROYECCION CON MONTO BASE $5 (x5 escala) ===")
neto_actual = sum(c['ganancia_neta'] for c in ciclos)
print(f"  Neto actual  (base $1): ${neto_actual:.2f}")
print(f"  Proyectado   (base $5): ${neto_actual*5:.2f}")
print(f"  Proyectado  (base $10): ${neto_actual*10:.2f}")
print(f"  Riesgo maximo por ciclo perdido (base $1): $135.76")
print(f"  Riesgo maximo por ciclo perdido (base $5): ${135.76*5:.2f}")
print(f"  Riesgo maximo por ciclo perdido (base $10): ${135.76*10:.2f}")
