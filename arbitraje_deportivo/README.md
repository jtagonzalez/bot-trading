# Arbitraje deportivo (surebets) — Colombia

Proyecto nuevo, separado del bot de trading. Objetivo: detectar combinaciones
de cuotas entre casas de apuestas donde la suma de probabilidades implícitas
es menor a 100%, lo que garantiza ganancia sin importar el resultado (ver
`arbitraje.py` para la fórmula y el cálculo de montos).

## Investigación de fuentes de datos (2026-10-01)

Ninguna casa de apuestas colombiana (Betplay, Rushbet, Wplay, Yajuego, Codere
Colombia, etc.) publica una API propia para consultar cuotas — son casas de
cuota fija orientadas al usuario final, no exchanges como Betfair (que sí
tiene API pero no opera en Colombia).

Opciones de terceros que agregan cuotas de varias casas:

- **odds-api.net** — CONFIRMA cobertura de 15 casas colombianas (Betplay,
  Wplay, Rushbet, Betano, Codere, Bwin, Rivalo, Zamba, Betsson, Luckia, etc).
  Sin plan gratuito. Plan "Starter" USD 30/mes, 50K requests/mes, hasta 2
  casas para el feed de arbitraje — suficiente para probar la idea real
  (2 casas ya alcanza para encontrar arbitrajes de 2 resultados).
- **odds-api.io** — plan gratuito real (2 casas, 100 requests/hora), pero
  NO confirma cobertura de casas colombianas específicamente (hay que
  escribirles a hello@odds-api.io para confirmar).

Son dos empresas DISTINTAS con nombres parecidos (.io vs .net) — no confundir.

## Plan de trabajo

1. Validar la lógica de detección y cálculo de montos con cuotas tomadas a
   mano de 2-3 casas colombianas (gratis, sin depender de ninguna API todavía).
2. Si la lógica funciona bien, decidir si pagar odds-api.net (USD 30/mes)
   para automatizar la obtención de cuotas en tiempo real.
3. Automatizar el escaneo continuo + alerta (no colocar apuestas automático
   al principio — las casas banean/limitan cuentas que detectan como
   "arbers", hay que operarlo con cautela manual al inicio).

## Riesgo principal (igual que con IQ Option y los OTC)

Las casas de apuestas detectan y limitan/banean cuentas que hacen arbitraje
de forma sistemática. No es un problema técnico, es el mismo patrón que ya
vivimos en el proyecto de trading: la plataforma reacciona cuando detecta
que alguien le saca ventaja de forma consistente.
