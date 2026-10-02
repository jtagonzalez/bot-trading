import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
Lista, EN ORDEN DE EJECUCIÓN, cada señal de la estrategia Weis Wave Volume
(acumulativo) + Bollinger(14,2) (backtest_weis_bollinger.py) en los 10 pares
forex "clásicos" (-op), usando los ~2 meses de 1 minuto disponibles en
velas.db, con martingala A 1 GALE (1 paso de martingala = 2 intentos por
señal como máximo: el original + un reintento si pierde).

Columnas: fecha/hora UTC de inicio del ciclo, activo, sentido, pasos usados,
monto arriesgado, resultado, ganancia neta, saldo acumulado (si se operaran
TODAS las señales de los 10 pares con $1 base).

Exporta operaciones_weis_bollinger.xlsx

    py listar_operaciones_weis_bollinger.py
"""
import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill

from backtest_weis_bollinger import (ATR_MULT_DEF, FOREX, PAYOUT, RACHA_MIN_DEF,
                                     cargar, construir_arrays_martingala,
                                     detectar_senales, resultados_de)
from portafolio_multi_edge import ciclos_generico

MAX_PASOS = 2   # "1 gale" = 1 paso de martingala ademas del original


def main():
    filas = []
    for a in FOREX:
        df = cargar(a)
        if len(df) < 500:
            continue
        eventos = detectar_senales(df, ATR_MULT_DEF, RACHA_MIN_DEF, "estricta")
        r = resultados_de(df, eventos)
        if not len(r):
            continue
        ev = [(int(row["idx"]), int(row["direccion"])) for _, row in r.iterrows()]
        ts, close, entra, direccion, operable = construir_arrays_martingala(df, ev)
        ciclos = ciclos_generico(ts, close, entra, direccion, operable, PAYOUT, 60, MAX_PASOS)
        for ts0, neto, pasos, gano, arriesgado in ciclos:
            dt0 = pd.Timestamp(int(ts0), unit="s", tz="UTC")
            filas.append(dict(
                fecha_hora_utc=dt0, fecha=dt0.date(), hora_utc=dt0.strftime("%H:%M"),
                activo=a, sentido="COMPRA" if direccion[np.searchsorted(ts, ts0)] > 0 else "VENTA",
                pasos_usados=pasos, monto_arriesgado=round(arriesgado, 2),
                resultado="GANADA" if gano else "PERDIDA", ganancia=round(neto, 4)))

    df = pd.DataFrame(filas).sort_values(["fecha_hora_utc", "activo"], kind="mergesort").reset_index(drop=True)

    # no puede haber 2 operaciones en el mismo minuto: si varias monedas
    # disparan a la vez, nos quedamos con 1 sola (la primera por orden
    # alfabetico de activo, para que sea determinista) y se descarta el resto
    antes = len(df)
    df = df.drop_duplicates(subset="fecha_hora_utc", keep="first").reset_index(drop=True)
    descartadas = antes - len(df)

    cols = ["fecha", "hora_utc", "fecha_hora_utc", "activo", "sentido",
            "pasos_usados", "monto_arriesgado", "resultado", "ganancia"]
    df = df[cols]
    df["fecha_hora_utc"] = df["fecha_hora_utc"].dt.tz_localize(None)
    df.index += 1
    df.index.name = "n_operacion"
    df["saldo_acumulado"] = df.ganancia.cumsum()

    nombre = "operaciones_weis_bollinger.xlsx"
    hoja = f"Weis+Bollinger 1 gale"
    df.to_excel(nombre, sheet_name=hoja)

    verde = PatternFill("solid", fgColor="C6EFCE")
    rojo = PatternFill("solid", fgColor="FFC7CE")
    texto_verde, texto_rojo = Font(color="006100"), Font(color="9C0006")
    wb = load_workbook(nombre)
    ws = wb[hoja]
    col_resultado = list(df.columns).index("resultado") + 2
    for fila in range(2, ws.max_row + 1):
        celda = ws.cell(row=fila, column=col_resultado)
        if celda.value == "GANADA":
            celda.fill, celda.font = verde, texto_verde
        elif celda.value == "PERDIDA":
            celda.fill, celda.font = rojo, texto_rojo
    for col in ws.columns:
        letra = col[0].column_letter
        ancho = max(len(str(c.value)) for c in col if c.value is not None)
        ws.column_dimensions[letra].width = max(ancho + 2, 10)
    wb.save(nombre)

    print(f"{descartadas} señal(es) descartada(s) por coincidir con otra moneda en el mismo minuto")
    print(f"{len(df)} ciclos (1 gale) en orden de ejecución -> {nombre}\n")
    print(df.resultado.value_counts())
    print(f"\nacierto de ciclos: {100 * (df.resultado=='GANADA').mean():.2f}%")
    print(f"EV por $ arriesgado: {df.ganancia.sum() / df.monto_arriesgado.sum():+.4f}")
    print(f"saldo final operando los 10 pares juntos: ${df.ganancia.sum():+.2f}")
    print(f"\npor activo:")
    print(df.groupby("activo").agg(n=("resultado", "size"),
                                   acierto=("resultado", lambda s: 100 * (s == "GANADA").mean()),
                                   neto=("ganancia", "sum")).round(2))
    print(f"\nprimeras 5:")
    print(df[["fecha", "hora_utc", "activo", "sentido", "resultado", "ganancia"]].head(5).to_string())
    print(f"\nultimas 5:")
    print(df[["fecha", "hora_utc", "activo", "sentido", "resultado", "ganancia"]].tail(5).to_string())


if __name__ == "__main__":
    main()
