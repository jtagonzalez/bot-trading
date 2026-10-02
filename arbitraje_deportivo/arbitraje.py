"""
Calculadora/detector de arbitraje (surebet) entre casas de apuestas.

Funciona con cuotas ingresadas A MANO por ahora (sin API todavia) -- la idea
es validar la logica antes de pagar por una fuente de datos en tiempo real
(ver README.md para las opciones de API investigadas).

Formula: cada cuota implica una probabilidad = 1/cuota. Si la SUMA de las
probabilidades implicitas de todos los resultados posibles de un evento
(cada uno tomado de la mejor casa que lo ofrece) es MENOR a 100%, hay
arbitraje garantizado. El monto a apostar en cada resultado es proporcional
a su probabilidad implicita, para que la ganancia sea IGUAL sin importar
cual resultado termine pasando.

Uso:
    py arbitraje.py
"""


def probabilidad_implicita(cuota):
    return 1.0 / cuota


def evaluar_arbitraje(cuotas, monto_total=100.0):
    """cuotas: dict {resultado: (casa, cuota)}, un resultado por cada
    desenlace posible del evento. Ejemplos:
        2 resultados (tenis):  {"A": ("Betplay", 2.10), "B": ("Wplay", 2.05)}
        3 resultados (futbol): {"local": (...), "empate": (...), "visitante": (...)}
    """
    probs = {r: probabilidad_implicita(c) for r, (casa, c) in cuotas.items()}
    suma = sum(probs.values())
    hay_arbitraje = suma < 1.0

    apuestas = {r: monto_total * (p / suma) for r, p in probs.items()}
    ganancias = {r: apuestas[r] * cuotas[r][1] - monto_total for r in cuotas}

    return {
        "hay_arbitraje": hay_arbitraje,
        "margen_pct": round((1.0 - suma) * 100, 3),
        "suma_probabilidades_pct": round(suma * 100, 3),
        "apuestas": {r: (cuotas[r][0], round(m, 2)) for r, m in apuestas.items()},
        "ganancia_garantizada": round(min(ganancias.values()), 2),
        "ganancias_por_resultado": {r: round(g, 2) for r, g in ganancias.items()},
    }


def mostrar(resultado, evento=""):
    print(f"\n{'=' * 60}")
    if evento:
        print(f"Evento: {evento}")
    print(f"Suma de probabilidades implicitas: {resultado['suma_probabilidades_pct']}%")
    if resultado["hay_arbitraje"]:
        print(f"*** ARBITRAJE ENCONTRADO *** margen: {resultado['margen_pct']}%")
        print("Repartir asi:")
        for res, (casa, monto) in resultado["apuestas"].items():
            print(f"  {res:15s} -> apostar ${monto:8.2f} en {casa}")
        print(f"Ganancia garantizada (pase lo que pase): ${resultado['ganancia_garantizada']:+.2f}")
    else:
        print("Sin arbitraje -- la suma supera el 100%, no hay combinacion que garantice ganancia")


if __name__ == "__main__":
    ejemplo_2 = {
        "equipo_A": ("Betplay", 2.10),
        "equipo_B": ("Wplay", 2.05),
    }
    mostrar(evaluar_arbitraje(ejemplo_2, monto_total=100), evento="Ejemplo 2 resultados (tenis)")

    ejemplo_3 = {
        "local": ("Betplay", 2.80),
        "empate": ("Rushbet", 3.40),
        "visitante": ("Wplay", 2.75),
    }
    mostrar(evaluar_arbitraje(ejemplo_3, monto_total=100), evento="Ejemplo 3 resultados (futbol)")

    print("\n\nPara probar con cuotas reales: copia 2-3 partidos de distintas casas")
    print("colombianas a mano y arma un dict como los de arriba, o importa")
    print("evaluar_arbitraje() desde otro script.")
