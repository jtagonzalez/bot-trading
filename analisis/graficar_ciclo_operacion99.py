import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Reconstruccion visual del ciclo de martingala real de la operacion 99
(GBPJPY-op, PUT, 2026-10-02 02:09-02:16 UTC) usando velas de 1 minuto reales
descargadas de IQ Option (_velas_gbpjpy_op99.json). Muestra los 5 pasos
REALES que ocurrieron (4 perdidas + 1 empate, el ciclo se cerro ahi por el
bug ya corregido) y el paso 6 HIPOTETICO que nunca se ejecuto -- reconstruido
con el precio real de esa vela, marcado claramente como no ejecutado.

No modifica ningun dato de live.db -- es solo una imagen de verificacion.
"""
import json
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["text.parse_math"] = False
import matplotlib.pyplot as plt
import numpy as np

FONDO, TEXTO, REJILLA = "#131722", "#d1d4dc", "#2a2e39"
VERDE, ROJA = "#26a69a", "#ef5350"

velas = json.load(open("_velas_gbpjpy_op99.json"))
velas = sorted(velas, key=lambda v: v["from"])
tiempos = [datetime.fromtimestamp(v["from"], tz=timezone.utc) for v in velas]
o = [v["open"] for v in velas]
h = [v["max"] for v in velas]
l = [v["min"] for v in velas]
c = [v["close"] for v in velas]
n = len(velas)

# idx1=02:09 señal, idx2..idx7 pasos 1..5 reales (entra en idx, resuelve en idx+1),
# idx7..idx8 paso 6 HIPOTETICO (nunca ejecutado)
PASOS = [
    (2, 1, 1.00, "loss", "-$1.00"),
    (3, 2, 2.18, "loss", "-$2.18"),
    (4, 3, 4.74, "loss", "-$4.74"),
    (5, 4, 10.32, "loss", "-$10.32"),
    (6, 5, 22.46, "tie", "$0.00"),
]
PASO6_HIPOTETICO = (7, 6, 22.46, "win?", "+$19.09 (estimado)")

fig, ax = plt.subplots(figsize=(17, 7.5), facecolor=FONDO)
fig.subplots_adjust(right=0.71, top=0.86)
ax.set_facecolor(FONDO)
ax.tick_params(colors=TEXTO, labelsize=8)
for s in ax.spines.values():
    s.set_color(REJILLA)

x = np.arange(n)
for xi, (oo, hh, ll, cc) in enumerate(zip(o, h, l, c)):
    col = VERDE if cc >= oo else ROJA
    ax.vlines(xi, ll, hh, color=col, lw=1)
    ax.bar(xi, max(abs(cc - oo), (max(h) - min(l)) * 0.003), bottom=min(oo, cc),
           color=col, width=0.6, zorder=3)

rango = max(h) - min(l)
ymin, ymax = min(l) - rango * 0.12, max(h) + rango * 0.30
ax.set_ylim(ymin, ymax)
ax.set_xlim(-0.6, n - 0.4)

# señal
ax.annotate("SEÑAL\nVENDER (PUT)", xy=(1, c[1]), xytext=(1, ymax - rango * 0.06),
            ha="center", color="#ffeb3b", fontsize=9, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color="#ffeb3b"))

col_tie, col_hip = "#9e9e9e", "#4caf50"
for idx_entrada, num, monto, resultado, texto_g in PASOS:
    col = ROJA if resultado == "loss" else col_tie
    ax.axvline(idx_entrada, color=col, lw=0.7, ls=":", alpha=0.6)
    y_label = ymax - rango * (0.14 + 0.07 * (num - 1))
    etiqueta = f"P{num}  ${monto:.2f}  {'PIERDE' if resultado=='loss' else 'EMPATA'}  {texto_g}"
    ax.text(idx_entrada + 0.08, y_label, etiqueta, color=col, fontsize=8.3,
            fontweight="bold", va="center",
            bbox=dict(boxstyle="round,pad=0.15", fc=FONDO, ec=col, alpha=0.9))
    ax.scatter([idx_entrada], [c[idx_entrada]], color=col, s=35, zorder=5)

# paso 6 hipotetico: sombreado con rayas + etiqueta bien explicita de "no ejecutado"
idx6, num6, monto6, _, texto6 = PASO6_HIPOTETICO
ax.axvspan(idx6 - 0.5, idx6 + 0.5, color=col_hip, alpha=0.12, hatch="//",
           edgecolor=col_hip, lw=0.8, zorder=1)
ax.axvline(idx6, color=col_hip, lw=1.2, ls="--", alpha=0.8)
y6 = ymax - rango * (0.14 + 0.07 * 5)
ax.text(idx6 + 0.08, y6, f"P6 HIPOTÉTICO  ${monto6:.2f}  {texto6}\n(NO se ejecutó — bug ya corregido)",
        color=col_hip, fontsize=8.3, fontweight="bold", va="center",
        bbox=dict(boxstyle="round,pad=0.2", fc=FONDO, ec=col_hip, alpha=0.95))
ax.scatter([idx6], [c[idx6]], facecolors="none", edgecolors=col_hip, s=90,
           linewidths=2, zorder=5, marker="o")

ticks = list(range(n))
ax.set_xticks(ticks, [t.strftime("%H:%M") for t in tiempos])
ax.grid(color=REJILLA, lw=0.6)
ax.yaxis.tick_right()

resumen = (
    "RESULTADO REAL\n"
    "(se cerró en P5\n"
    "por el bug):\n"
    "  P1  -$1.00\n"
    "  P2  -$2.18\n"
    "  P3  -$4.74\n"
    "  P4  -$10.32\n"
    "  P5  +$0.00\n"
    "  -----------\n"
    "  NETO -$18,24\n\n"
    "SI EL FIX YA\n"
    "ESTABA ACTIVO\n"
    "(estimado):\n"
    "  + P6 +$19,09\n"
    "  -----------\n"
    "  NETO ~+$0,85\n\n"
    "Diferencia por\n"
    "el bug en ESTE\n"
    "ciclo: ~$19,09\n\n"
    "Precisión: velas\n"
    "de 1 min, no al\n"
    "segundo exacto.\n"
    "Confianza ALTA,\n"
    "no es 100%\n"
    "certeza absoluta."
)
ax.text(1.05, 0.98, resumen, transform=ax.transAxes, color=TEXTO, fontsize=8.5,
        va="top", ha="left", family="monospace",
        bbox=dict(boxstyle="round", fc="#1a1a19", ec=REJILLA))

titulo = ("GBPJPY-op · reconstrucción del ciclo real de martingala (operación 99) · velas de 1 min reales\n"
          "Señal 2026-10-02 02:09 UTC · dirección VENTA (PUT) · P1-P5 reales, P6 hipotético (no ejecutado)")
ax.set_title(titulo, color=TEXTO, fontsize=10.5, loc="left")

ruta = "capturas_live/ciclo_operacion99_gbpjpy.png"
fig.savefig(ruta, dpi=110, facecolor=FONDO)
plt.close(fig)
print("->", ruta)
