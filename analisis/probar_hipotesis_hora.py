import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
?ES LA APERTURA DE LONDRES, O ES CASUALIDAD?

El escaneo saco la hora 07 UTC con 55,39% de acierto. Sobrevivio la correccion
por comparaciones multiples y la particion en dos mitades, pero eso solo dice
"no lo he podido matar", no dice POR QUE.

La hipotesis: es la apertura de Londres. Y esa hipotesis hace una prediccion
que se puede comprobar — porque Londres NO abre siempre a la misma hora UTC:

    verano (BST, finales de marzo a finales de octubre)  ->  07:00 UTC
    invierno (GMT, finales de octubre a finales de marzo) ->  08:00 UTC

Si el edge es la apertura de Londres, debe MOVERSE con el horario de verano:
estar en la hora 07 en verano y saltar a la hora 08 en invierno.

Si en cambio se queda clavado en la hora 07 todo el ano, la explicacion de
Londres es falsa y probablemente estemos mirando ruido que sobrevivio por
suerte.

Esta es la clase de prueba que no se puede aprobar por casualidad: no mide si
el numero es alto, mide si se comporta como deberia comportarse la causa que
le atribuimos.

Uso:
    py probar_hipotesis_hora.py
    py probar_hipotesis_hora.py --npz oos_15m.npz --payout 90
"""
import argparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

import simulador as S

LONDRES = ZoneInfo("Europe/London")


def en_verano(ts):
    """True si en esa fecha Londres esta en BST (UTC+1)."""
    d = datetime.fromtimestamp(int(ts), timezone.utc).astimezone(LONDRES)
    return bool(d.dst())


def celda(acierto, m, be, etiqueta):
    n = int(m.sum())
    if n < 80:
        return f"  {etiqueta:<34} n={n:<5} muestra insuficiente"
    ac = acierto[m]
    k = int(ac.sum())
    lo, hi = S.wilson(k, n)
    marca = "  <-- sobre breakeven" if lo > be else ""
    return (f"  {etiqueta:<34} n={n:<5} {100 * k / n:6.2f}%  "
            f"[{lo:5.2f},{hi:5.2f}]{marca}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="oos_15m.npz")
    ap.add_argument("--payout", type=float, default=90.0)
    ap.add_argument("--pct", type=float, default=10.0)
    a = ap.parse_args()

    be = 100 / (1 + a.payout / 100)
    d = np.load(a.npz, allow_pickle=True)
    p, y, hora = d["p"], d["y"], d["hora"]
    ts = d["dt"].astype("int64")

    conf = np.abs(p - 0.5)
    sel = conf >= np.quantile(conf, 1 - a.pct / 100)
    acierto = np.where(p > 0.5, y, 1 - y)

    verano = np.array([en_verano(t) for t in ts])
    print(f"Payout {a.payout}%  ->  breakeven {be:.2f}%")
    print(f"Operando el {a.pct:.0f}% mas confiable: {int(sel.sum()):,} predicciones")
    print(f"En horario de verano: {int((sel & verano).sum()):,}   "
          f"en invierno: {int((sel & ~verano).sum()):,}")

    print(f"\n{'=' * 74}")
    print("LA PRUEBA: ?se mueve el edge con el horario de verano?")
    print(f"{'=' * 74}")
    print("  Si es la apertura de Londres, la hora 07 debe rendir en VERANO y")
    print("  la hora 08 en INVIERNO. Si las dos rinden igual en las dos")
    print("  estaciones, no es Londres.\n")

    for h in (6, 7, 8, 9):
        print(f"  --- hora {h:02d} UTC ---")
        print(celda(acierto, sel & (hora == h) & verano,  be, f"verano  (Londres abre 07 UTC)"))
        print(celda(acierto, sel & (hora == h) & ~verano, be, f"invierno (Londres abre 08 UTC)"))
        print()

    # El contraste que decide es DENTRO de cada estacion.
    #
    # Comparar "apertura" contra "desfase" juntando las dos estaciones es un
    # error: el verano aporta muchas mas muestras, asi que ese contraste mide
    # sobre todo cuanto rinde el verano, no si el edge se movio. La pregunta
    # es si DENTRO del invierno el edge salta de la hora 07 a la hora 08.
    print(f"{'=' * 74}")
    print("VEREDICTO — el contraste dentro de cada estacion")
    print(f"{'=' * 74}")

    def wr(m):
        n = int(m.sum())
        return (100 * acierto[m].mean(), n) if n >= 80 else (None, n)

    v7, n_v7 = wr(sel & (hora == 7) & verano)
    v8, n_v8 = wr(sel & (hora == 8) & verano)
    i7, n_i7 = wr(sel & (hora == 7) & ~verano)
    i8, n_i8 = wr(sel & (hora == 8) & ~verano)

    print(f"  VERANO   (Londres abre a las 07 UTC)")
    print(f"    hora 07: {v7:.2f}% (n={n_v7:,})   hora 08: {v8:.2f}% (n={n_v8:,})"
          f"   -> gana la {'07' if v7 > v8 else '08'}")
    print(f"  INVIERNO (Londres abre a las 08 UTC)")
    print(f"    hora 07: {i7:.2f}% (n={n_i7:,})   hora 08: {i8:.2f}% (n={n_i8:,})"
          f"   -> gana la {'07' if i7 > i8 else '08'}")

    sigue_a_londres = (v7 > v8) and (i8 > i7)
    print()
    if sigue_a_londres:
        print("  -> La hipotesis AGUANTA: el edge se mueve con el horario de verano,")
        print("     que es lo que haria la apertura de Londres y no haria el azar.")
    else:
        print("  -> La hipotesis de Londres NO se sostiene.")
        print(f"     En invierno la hora 07 ({i7:.2f}%) sigue ganando a la hora 08")
        print(f"     ({i8:.2f}%), cuando Londres ya no abre a las 07. El edge esta")
        print("     clavado a una hora UTC fija, no a la sesion de mercado.")
        print()
        print("     Eso NO lo invalida, pero lo deja sin explicacion. Y un patron")
        print("     sin mecanismo, encontrado buscando entre 17 horas, merece mucha")
        print("     menos confianza que uno que se comporta como dice su causa.")
        print("     Recomendacion: no operarlo hasta entender a que se ancla.")


if __name__ == "__main__":
    main()
