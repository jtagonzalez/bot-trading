# Estrategia Gold 4.0 + Bollinger — registro completo

Última actualización: 2026-09-30. Datos: `velas.db`, velas de 1 minuto,
forex 10 pares (22/07 al 30/09/2026, 51 días hábiles — **es el máximo que
guarda IQ Option para 1 min, no se puede pedir más**), cripto BTC/ETH
(01/08 al 30/09/2026, 61 días, 24x7).

Código reutilizable: [`estrategia_gold4_bb.py`](estrategia_gold4_bb.py).
Este documento explica las reglas y los resultados; ese archivo es el código.

## 0. Conclusión (léelo primero)

**Ninguna variante probada supera el 53,76% de acierto necesario (payout
turbo 86%) con confianza estadística.** El acierto real medido va de 46,8%
a 53,5% según la variante — todas indistinguibles de tirar una moneda, o
por debajo. La martingala continua (ver sección 4) no arregla esto: cambia
pérdidas frecuentes y chicas por pérdidas raras y grandes, con el mismo
valor esperado negativo de fondo. Un resultado que en algún momento pareció
"0% de riesgo" (extremo + Gold, nivel bajo) se debía a tener solo 27
señales; con 8 días más de datos ya apareció la primera pérdida.

## 1. Indicador base: Gold 4.0

Traducción del script Lua que pasó el usuario. Dos sistemas independientes:

- **Sistema A** (flechas Up/Down): `mad` = distancia del cierre a su media de
  12; exige `mad` cruzando cero a favor, cruce de medias de 3 y 50 (cierre y
  apertura) y precio respecto a una EMA de 100.
- **Sistema B** (aviso COMPRAR/VENDER, el que se usó en todo lo que sigue):
  cruce entre una oscilación del precio típico (`hl2`) menos su media de 34,
  y una media ponderada (WMA 5) de esa misma oscilación.

Código: `gold4()` en `estrategia_gold4_bb.py` (antes `senales()` en
`simular_gold4.py`).

## 2. Indicadores de apoyo

- **Bandas de Bollinger (14,2):** `mid` = SMA14 del cierre (la "línea media"
  o "naranja" de las capturas), `sup`/`inf` = `mid ± 2·desviación estándar`
  (ddof=0).
- **Línea cian:** SMA50 del cierre — la tendencia lenta de fondo.
- **ATR(14):** para medir la inclinación de la cian en unidades comparables
  entre pares.
- **Pendiente de la cian:** `(cian − cian hace 20 velas) / ATR(14)`. Positiva
  = sube, negativa = baja. Se exige un mínimo ("nivel") para descartar
  mercado lateral.

Código: `indicadores()`.

## 3. Las 3 formas de entrada probadas

Todas comparten el mismo filtro de tendencia de fondo (sección 3.4) y se
diferencian en **dónde** se espera que rebote o siga el precio.

### 3.1 Extremo de Bollinger (el setup original de las capturas)

VENTA: vela roja, la mecha toca la banda **superior**, tendencia bajista.
COMPRA: espejo, banda inferior. Es un setup de **reversión**: se apuesta a
que el precio rebota en el extremo.

Función: `senal_extremo(v, nivel, con_gold)`.

| Nivel inclinación | Señales (51d, 10 pares) | Acierto | Con martingala $/día |
|---|---|---|---|
| 0.0 (mínima) | 43 | 53,49% | +$0,62 |
| 0.5 (moderada) | 38 | 52,63% | +$0,69 |
| 1.0 (fuerte) | 30 | 53,33% | +$0,56 |

Muestra demasiado chica en todos los niveles (IC95 de hasta ±15 puntos) —
no se puede confirmar ni descartar ventaja. Es la única variante que no
está claramente por debajo del breakeven, precisamente porque casi no hay
datos.

### 3.2 Rebote en la línea media (14)

Igual que 3.1 pero el precio solo necesita retroceder hasta la **media**
(no el extremo) y cerrar de nuevo del lado de la tendencia. Da muchas más
señales (90-300/día en 10 pares) porque tocar la media es un evento mucho
más frecuente.

Función: `senal_media(v, nivel, con_gold)`.

| Nivel · Gold | Señales/día | Acierto | Con martingala $/día |
|---|---|---|---|
| 1.0 · con Gold | 92,0 | 48,84% | −$44,10 |
| 1.0 · sin Gold | 204,3 | 49,22% | −$62,71 |

Acierto estable ~49% en todos los niveles — muestra grande (miles de
señales), resultado confiable: **no hay ventaja**.

### 3.3 3 velas seguidas del mismo color (continuación, no reversión)

Idea contraria a las dos anteriores: en vez de esperar un rebote, se apuesta
a que una tendencia YA en marcha (3 velas seguidas del mismo color, las 3
del lado correcto de la media) sigue un paso más. No usa aviso del Gold 4.0.

Función: `senal_3velas(v, con_cian)`.

| Variante | Señales/día (10 pares) | Acierto | Con martingala $/día |
|---|---|---|---|
| Literal (sin filtro cian) | 1.383,6 | 49,30% | −$683,70 |
| + cian a favor | 815,4 | 49,37% | −$431,79 |

70.564 señales en 51 días — la muestra más grande de las tres, y la más
concluyente: **49,3% de acierto, sin ninguna ambigüedad de muestra chica.**

### 3.4 Zigzag confirmado en mercado lateral (idea opuesta a las 3 anteriores)

En vez de operar A FAVOR de una tendencia, esta idea busca mercado LATERAL:
detecta toques alternados de extremo a extremo de Bollinger (sup, inf, sup,
inf...) y, con un mínimo de rebotes confirmados, opera esperando que el
precio siga rebotando (tocó abajo → compra; tocó arriba → venta).

Función: `senal_zigzag(v, min_rebotes)`.

| Mín. rebotes | Señales/día (forex, 10 pares) | Acierto | Con martingala $/día |
|---|---|---|---|
| 1 | 474,1 | 50,84% | −$142,67 |
| 2 | 74,3 | 50,38% | −$22,31 |
| 3 | 11,8 | 52,08% | +$5,34 (muestra chica, no confiable — 616 ciclos, IC95 [48,09-56,05]) |

**Defecto de diseño encontrado (confirmado con una imagen real, AUDUSD
14/09 06:46):** "rebotar entre bandas" no distingue un mercado lateral real
de un CANAL con tendencia — las bandas pueden bajar/subir juntas mientras
el precio zigzaguea dentro, y el detector lo confunde con lateralidad. Ese
ejemplo perdió las 6 operaciones (−$87,16) precisamente porque la cian
bajaba con claridad (pendiente −2,44 ATR/20v) mientras el zigzag local
seguía "confirmándose".

**Corregido:** se agregó `max_inclinacion` a `senal_zigzag()` — exige
`|pend| <= umbral` (cian prácticamente plana). Verificado que excluye el
caso AUDUSD 14/09 con cualquier umbral probado. Resultado con la corrección
(forex, mínimo 2 rebotes, 51 días):

| Cian plana | Señales/día | Acierto | IC95 | Con martingala $/día |
|---|---|---|---|---|
| Sin filtro | 74,3 | 50,38% | [48,79-51,97] | −$22,31 |
| ≤0,5 ATR/20v | 17,3 | 50,74% | [47,44-54,02] | −$10,32 |
| ≤0,3 ATR/20v | 10,8 | 50,99% | [46,84-55,14] | −$2,56 |
| ≤0,2 ATR/20v | 6,9 | 53,85% | [48,62-58,99] | −$4,35 |
| ≤0,1 ATR/20v | 3,5 | 52,54% | [45,21-59,77] | +$1,39 |

El punto central mejora al exigir más planitud, pero el límite inferior del
IC95 nunca supera el breakeven (53,76%) y la muestra baja de 74 a 3,5
señales/día — sigue sin ser una ventaja confirmable. Igual patrón de
siempre: más filtro = mejor número aparente, pero menos datos para creerlo.

### 3.5 Filtro de tendencia común (evolución dentro de esta conversación)

Se fue corrigiendo en varias iteraciones, cada una motivada por un caso real
donde la anterior fallaba:

1. **T0** (primera versión): solo el cierre de la vela al lado correcto de
   la cian → dejaba pasar mercado lateral (cian plana).
2. **T1**: la vela ENTERA (máximo/mínimo) al lado correcto de la cian.
3. **T2**: T1 + la cian distinta a la de 10 velas atrás → encontramos un
   caso real (GBPUSD 28/07) donde se vendía con la cian ya subiendo.
4. **v2 (versión final, la que usan 3.1 y 3.2):** vela entera + **inclinación
   mínima de la cian en 20 velas** (en múltiplos de ATR14) + dirección
   correcta + **vela anterior del mismo color** (dos verdes/rojas seguidas
   antes de la señal).

Código: `_base_tendencia()`.

## 4. Gestión de dinero: martingala continua

Regla exacta pedida por el usuario: tras una vela perdedora, la siguiente
operación de 1 minuto se abre **en la vela inmediatamente siguiente**, mismo
sentido, sin esperar señal nueva. Monto ×2,16 = `(1+payout)/payout` (con
payout 86%, recupera lo perdido + $1 de ganancia). Máximo 6 intentos. Empate
= se devuelve el monto, se repite en la vela siguiente (consume un intento).

Código: `ciclos_de()` (antes en `simular_bb_martingala.py`).

**Por qué "parece" funcionar y no funciona:** con acierto ~49-53%, la
mayoría de los ciclos se recuperan y dejan +$0,86 — un día típico de 10
ciclos gana +$8,60 y se siente bien. Pero 1 o 2 ciclos perdidos completos
cuestan $87-190 cada uno, y sobran para dejar el mes en negativo. El valor
esperado por ciclo sigue siendo negativo en (casi) todas las variantes,
solo que la mayoría de los días no se nota.

## 5. Probabilidad de un día perdedor (10 operaciones/día)

Calculado con binomial exacta (sin martingala) y bootstrap de 100.000 días
sobre los ciclos históricos reales (con martingala). Ver
`prob_10_dia_v2b.py` (script ad-hoc, no versionado como los demás).

| Setup | Prob. día perdedor SIN martingala | Prob. día perdedor CON martingala | El 1% peor día (con mart.) |
|---|---|---|---|
| Extremo + Gold | 57,3% | 0,0% ⚠️ (27-30 muestras, no confiable) | — |
| Extremo sin Gold | 69,9% | 10,9% | −$32,10 |
| Media, nivel 1.0 | 58,9-75,0% (según par) | 12,0-21,6% | −$79 a −$167 |
| 3 velas | 64,0% | 17,7% | −$120,12 |

## 6. Archivos

| Archivo | Qué hace |
|---|---|
| `estrategia_gold4_bb.py` | **Módulo consolidado** — usar este para nuevas pruebas |
| `simular_gold4.py` | Traducción original del Gold 4.0, primeras pruebas sueltas |
| `simular_gold4_ciclos.py` | Ciclos de recuperación (10 intentos, 10/día) sin filtro de Bollinger |
| `simular_gold4_filtros.py` | Pruebas de RSI/Estocástico/ADX sobre el Gold 4.0 solo |
| `simular_gold4_bb.py` | Primera versión del setup Bollinger (sin filtro de tendencia estricto) |
| `simular_bb_martingala.py` | Martingala continua sobre el setup original |
| `ver_toque_bb.py` | Detalle del solape mecha/banda, genera `toque_bb.csv` y `capturas_bb/` |
| `simular_bb_tendencia.py` | Evolución T0→T4 del filtro de tendencia |
| `simular_bb_color_previo.py` | Agrega el filtro "vela previa mismo color" sobre T0-T4 |
| `simular_bb_v2.py` | **Setup extremo final** (T2 + inclinación 20 velas + color previo) |
| `simular_bb_media.py` | **Setup media final** (igual que v2, toca la media en vez del extremo) |
| `simular_3velas.py` | **Setup 3 velas seguidas** |
| `simular_zigzag.py` | **Setup zigzag en mercado lateral** |
| `busqueda_exhaustiva.py` | Barrido setup×par×hora con corrección BH + control aleatorio |
| `estrategias_externas.py` | 9 estrategias buscadas en internet (RSI, MACD, EMA, Estocástico, DeMarker, S/R) |
| `busqueda_externas.py` | Las prueba con el mismo embudo estadístico (BH + control aleatorio del mismo tamaño) |
| `estrategias_propias.py` | 9 ideas propias (envolvente, pin bar, doji, inside bar, NR7, agotamiento, squeeze, volumen x2) |
| `busqueda_propias.py` | Las prueba igual, más la validación cruzada de activo que reveló el sobreajuste de BTCUSD |
| `animar_perdida.py` | Genera imágenes/GIF de un ciclo real vela a vela (`--regla original\|T2\|v2\|media`) |

## 7. Memoria relacionada

Ver `indicadores-sin-poder-predictivo.md`, `sesgo-empates-backtesting.md`,
`optimizar-es-contraproducente.md` en el índice de memoria del proyecto —
esta misma conclusión (ningún indicador de precio predice la vela
siguiente a 1 min) se repite en cada intento distinto.

## 8. Ideas ya descartadas (para no repetir)

- Filtros de sobrecompra/sobreventa (RSI, Estocástico, ADX) sobre el Gold 4.0.
- Fibonacci, cruce EMA9/21, EMA200.
- 10 pares nuevos, timeframes de 5 min y 30 min.
- Aviso del Gold 4.0 como filtro extra sobre extremo/media: reduce señales,
  no mejora el acierto de forma confiable.

## 9. Búsqueda exhaustiva (2026-09-30) — `busqueda_exhaustiva.py`

Se cruzaron los 8 setups de este documento × 12 activos × cada hora del día
(758 celdas, n≥30) contra el breakeven, con corrección Benjamini-Hochberg
(q=0.10) y exigiendo estabilidad en las 2 mitades del historial. Se corrió
en paralelo un control de 218 celdas con señales 100% aleatorias.

**Resultado: 0 de 758 celdas reales sobreviven la corrección — igual que el
control aleatorio (0 de 218).** El mejor candidato real sin corregir
(BTCUSD, 3 velas, hora 08 UTC, 57,44%, p=0,042) tiene un p-valor PEOR que
el mejor candidato del control aleatorio (NZDUSD, señal inventada, hora 16
UTC, 63,64%, p=0,018) — prueba directa de que, a esta escala de búsqueda,
el ruido puro produce resultados iguales o mejores que cualquier setup real.
No hay ventaja explotable en velas de 1 min de este broker con ninguna
combinación de lo construido hasta ahora.

Antes de programar una variante nueva, correrla dentro de este mismo marco
(agregarla a `setups()` en `busqueda_exhaustiva.py`) en vez de evaluarla
sola — así cualquier resultado bueno queda automáticamente puesto a prueba
contra el control aleatorio y la corrección múltiple.

## 10. Estrategias de internet probadas (2026-09-30) — `estrategias_externas.py` + `busqueda_externas.py`

Se buscaron las estrategias de 1 minuto más citadas en internet y se
tradujeron a reglas verificables: EMA triple (11/21/34), RSI(4) rebote,
RSI(14) clásico 30/70, Estocástico (12,3) 20/80, MACD+Estocástico,
Bollinger Bounce+RSI (un artículo afirma 70% de acierto con 50 operaciones),
ZigZag+DeMarker, Soporte/Resistencia (20 velas), SMA cruce 5/20.

- **Completas, todo el horario operable:** 48,2-51,3% de acierto — ninguna
  cerca del breakeven (53,76%). El "70%" de Bollinger Bounce+RSI resultó
  51,27% con datos reales.
- **Cruzadas por hora del día** (1.956 celdas) contra un control aleatorio
  del MISMO tamaño (1.962 celdas, para que la comparación sea justa): **0
  de 1.956 sobreviven** la corrección BH (q=0,10) — igual que el control (0
  de 1.962). Los mejores candidatos sin corregir (USDJPY Bollinger+RSI 15h
  UTC 68,2%; ETHUSD EMA triple 5h UTC 72,9%) no son estadísticamente
  distinguibles de lo que produce el ruido puro a esta escala de búsqueda:
  con ~2.000 pruebas, la probabilidad de obtener al menos un p≤0,005 por
  puro azar ya es del 99,99%.

**Conclusión: ninguna estrategia popular de internet tiene ventaja real en
este broker a 1 minuto**, ni aplicada tal cual ni buscando la mejor hora.

## 11. Ideas propias probadas (2026-09-30) — `estrategias_propias.py` + `busqueda_propias.py`

9 ideas propias, no sacadas de internet: envolvente, pin bar, doji en
extremo, inside bar, NR7+microtendencia, agotamiento de momentum (reversión,
lo opuesto a "3 velas seguidas"), squeeze de Bollinger, y 2 con volumen real
(sí está disponible en `velas.db`, columna `volume`, no son ceros).

- Sin cortar por hora: 49,6-52,9% de acierto. Las de VOLUMEN dieron lo mejor
  visto en toda la conversación (Volumen seco+ruptura 52,91%, Volumen
  pico+rechazo 52,74%) pero sin significancia (p=0,93 y 0,97, muestra chica).
- Cruzadas por hora (1.646 celdas): **0 sobreviven BH**, pero apareció el
  candidato más separado del ruido de toda la búsqueda: **BTCUSD, Squeeze
  Bollinger, hora 08 UTC: 63,76%, p=0,001** (vs. mejor aleatorio p=0,020),
  estable en las 2 mitades (66,4%/61,2%).

**Puesto a prueba de estrés — falla:**
1. Horas vecinas (07 y 09 UTC): 53,85% y 54,81%, nada especial — un efecto
   real de sesión debería decaer suave, no ser un pico aislado.
2. 25 combinaciones de parámetros (ventana 10-30, percentil 0,10-0,30):
   todas dan 60-67% — parece robusto, pero son pruebas correlacionadas
   (miran las mismas velas de BTCUSD con una definición apenas distinta),
   no 25 confirmaciones independientes.
3. **La prueba decisiva — mismo setup, mismo horario, otro activo:**
   ETHUSD 56,0% (p=0,27), EURUSD **48,3%** (p=0,92), GBPUSD **43,8%**
   (p=0,99) — por debajo de la moneda al aire en forex. Si fuera un efecto
   real de mercado a esa hora, debería aparecer también en otros pares de
   la misma sesión. No aparece: es sobreajuste al camino específico de
   BTCUSD en esta muestra de 61 días, no una ventaja real.

**Conclusión: ninguna de las 9 ideas propias tiene ventaja real tampoco.**
Metodología para futuras ideas: además de BH + control aleatorio + mitades,
**siempre validar cruzando a otro activo** — es la prueba que más rápido
distingue una ventaja real de un sobreajuste a una sola serie de precios.

## 12. Vencimiento largo y combinaciones de a 2 (2026-09-30)

- **Vencimiento 3 y 5 min** (en vez de 1) para las 4 familias más prometedoras: ninguna mejora de forma significativa (todos los p-valores >0,70). Nota: el 52,91% de "Volumen seco+ruptura" (sección 11) era casi todo cripto (53,41%); en forex solo da 47,26%, peor que el azar.
- **153 combinaciones de a 2** (exigir que dos de las ~20 señales construidas coincidan en la misma vela y sentido): la mejor, "MACD+Estocástico + Volumen pico+rechazo", da 58,79% pero con n=199 y p=0,088 — no sobrevive corrección (0 de 129 combinaciones con n≥30 la pasan).

**Balance de toda la búsqueda de reglas en 1 min (2026-09-30):** 4 setups Gold4+Bollinger, 758 celdas propias, 9 estrategias de internet (1.956 celdas), 9 ideas propias (1.646 celdas), 153 combinaciones — **miles de pruebas corregidas, cero sobreviven.** El único candidato real del proyecto entero sigue siendo el de 15 minutos (sección 9 del índice de memoria, `edge-ventana-operable.md`): EURUSD hora 07 UTC, ya corriendo en PRACTICE (run 7). Verificar ESE con más operaciones reales es más prometedor que seguir buscando reglas nuevas a 1 minuto.

## 13. DeMarker: reentrada tras extremo sostenido (2026-09-30)

Idea del usuario sobre el indicador DeMarker de su plataforma (período 13,
sobrecompra 80, sobreventa 20): si el DeMarker sale de la banda por MÁS de
N velas seguidas (extremo sostenido, no solo un toque) y luego reingresa,
operar a favor del reingreso (desde abajo → compra, desde arriba → venta).
Función: `demarker_reentrada(v, n=13, sobreventa=20, sobrecompra=80, min_velas=5)`
en `estrategias_propias.py`.

- Sin cortar por hora, exigir más velas fuera (2 a 15) no cambia nada:
  50,0-50,5% siempre, en 1 min y en 15 min (2,5 años de historia).
- Cruzado por hora (97 celdas con n≥30): 0 sobreviven BH; el mejor real
  (p=0,008) es PEOR que el mejor del control aleatorio (p=0,001) — la señal
  real ni siquiera se distingue del ruido.

**Conclusión:** el tiempo sostenido en el extremo del DeMarker no agrega
información, igual que RSI y Estocástico simples (sección 10). Un oscilador
de sobrecompra/sobreventa mide cuánto se movió el precio, no hacia dónde va
la siguiente vela.

## 14. Por explorar

Espacio para las próximas ideas — sección abierta.
