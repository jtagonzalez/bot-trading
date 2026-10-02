import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
OPTIMIZADOR — busca los valores de indicadores que maximizan el acierto.

Itera sobre los PARAMETROS de los indicadores (periodo del RSI, del estocastico,
de las Bollinger...) y sobre los UMBRALES de entrada. Miles de combinaciones.

Y hace lo que casi ningun optimizador hace: mide cada combinacion DOS veces.

    IN-SAMPLE   -> sobre los datos donde se busco (el pasado que ya vio)
    OUT-SAMPLE  -> sobre datos posteriores que nunca vio

La diferencia entre esas dos columnas es lo unico que importa. Una configuracion
con 80% in-sample y 51% out-sample no es una estrategia: es una coincidencia que
el optimizador encontro por fuerza bruta. Con suficientes intentos SIEMPRE
aparece alguna.

Uso:
    py optimizar.py --tf 30m --intentos 3000
    py optimizar.py --tf 30m --horas 20-23 --intentos 5000
    py optimizar.py --tf 15m --objetivo 80    # cuantas configs llegan a 80%
"""
import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import simulador as S

PAYOUT = 0.84
BREAKEVEN = 100 / (1 + PAYOUT)


def indicadores_param(df, p_rsi, p_stoch, p_bb, n_bb, p_adx):
    """Calcula los indicadores con los PERIODOS que se le pidan."""
    c, h, l = df.close, df.high, df.low
    out = {}

    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / p_rsi, adjust=False).mean()
    pr = (-d.clip(upper=0)).ewm(alpha=1 / p_rsi, adjust=False).mean()
    out["rsi"] = 100 - 100 / (1 + g / pr.replace(0, np.nan))

    ll, hh = l.rolling(p_stoch).min(), h.rolling(p_stoch).max()
    out["stoch"] = 100 * (c - ll) / (hh - ll).replace(0, np.nan)

    m, s = c.rolling(p_bb).mean(), c.rolling(p_bb).std()
    out["bb"] = (c - (m - n_bb * s)) / (2 * n_bb * s).replace(0, np.nan)

    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / p_adx, adjust=False).mean()
    up, dn = h.diff(), -l.diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr.replace(0, np.nan)
    pdi = 100 * plus.ewm(alpha=1 / p_adx, adjust=False).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / p_adx, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    out["adx"] = dx.ewm(alpha=1 / p_adx, adjust=False).mean()
    return out


def preparar(db, tf_seg, horas):
    """Carga las velas una sola vez; los indicadores se recalculan por config."""
    datos = []
    for a in S.activos_disponibles(db, tf_seg):
        df = S.cargar_tf(db, a, tf_seg)
        if len(df) < 2000:
            continue
        ok = df.contigua.shift(-1).fillna(False).values & (df.dow.values < 5)
        if horas:
            ok = ok & (df.hora.values >= horas[0]) & (df.hora.values <= horas[1])
        nxt = df.close.shift(-1).values
        ok = ok & np.isfinite(nxt) & (nxt != df.close.values)
        datos.append((a, df, ok, (nxt > df.close.values).astype(int)))
    return datos


def evaluar_config(datos, cfg, corte_frac=0.7):
    """Devuelve (wr_in, n_in, wr_out, n_out) para una configuracion."""
    ins_k = ins_n = out_k = out_n = 0
    for a, df, ok, sube in datos:
        ind = indicadores_param(df, cfg["p_rsi"], cfg["p_stoch"],
                                cfg["p_bb"], cfg["n_bb"], cfg["p_adx"])
        rsi, st, bb, adx = ind["rsi"].values, ind["stoch"].values, ind["bb"].values, ind["adx"].values
        val = ok & np.isfinite(rsi) & np.isfinite(st) & np.isfinite(bb) & np.isfinite(adx)

        call = val & (rsi < cfg["rsi_lo"]) & (st < cfg["st_lo"]) & (bb < cfg["bb_lo"])
        put = val & (rsi > 100 - cfg["rsi_lo"]) & (st > 100 - cfg["st_lo"]) & (bb > 1 - cfg["bb_lo"])
        if cfg["adx_max"] < 100:
            call &= (adx < cfg["adx_max"])
            put &= (adx < cfg["adx_max"])
        if cfg["adx_min"] > 0:
            call &= (adx > cfg["adx_min"])
            put &= (adx > cfg["adx_min"])

        corte = int(len(df) * corte_frac)
        idx = np.arange(len(df))
        for m, direc in ((call, 1), (put, 0)):
            if not m.any():
                continue
            acierto = (sube == direc)
            sel_in = m & (idx < corte)
            sel_out = m & (idx >= corte)
            ins_n += int(sel_in.sum()); ins_k += int(acierto[sel_in].sum())
            out_n += int(sel_out.sum()); out_k += int(acierto[sel_out].sum())
    wi = 100 * ins_k / ins_n if ins_n else 0.0
    wo = 100 * out_k / out_n if out_n else 0.0
    return wi, ins_n, wo, out_n


def muestrear(rng):
    return {
        "p_rsi": int(rng.integers(4, 31)),
        "p_stoch": int(rng.integers(5, 31)),
        "p_bb": int(rng.integers(10, 41)),
        "n_bb": float(rng.choice([1.5, 2.0, 2.5, 3.0])),
        "p_adx": int(rng.integers(7, 29)),
        "rsi_lo": float(rng.integers(5, 46)),
        "st_lo": float(rng.integers(2, 41)),
        "bb_lo": float(rng.choice([0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 1.01])),
        "adx_max": float(rng.choice([15, 20, 25, 30, 40, 100])),
        "adx_min": float(rng.choice([0, 0, 0, 20, 25, 30])),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="velas.db")
    ap.add_argument("--tf", default="30m", choices=list(S.TFS))
    ap.add_argument("--horas", default=None, help="franja UTC, ej 20-23")
    ap.add_argument("--intentos", type=int, default=2000)
    ap.add_argument("--min-ops", type=int, default=150)
    ap.add_argument("--objetivo", type=float, default=80.0)
    ap.add_argument("--semilla", type=int, default=0)
    args = ap.parse_args()

    horas = tuple(int(x) for x in args.horas.split("-")) if args.horas else None
    print(f"Cargando {args.tf}" + (f", horas {args.horas} UTC" if horas else "") + " ...")
    datos = preparar(args.db, S.TFS[args.tf], horas)
    if not datos:
        raise SystemExit("sin datos")
    print(f"  {len(datos)} activos  |  breakeven {BREAKEVEN:.2f}%  |  objetivo {args.objetivo:.0f}%")
    print(f"  probando {args.intentos:,} configuraciones de indicadores...\n")

    rng = np.random.default_rng(args.semilla)
    res = []
    for i in range(args.intentos):
        cfg = muestrear(rng)
        wi, ni, wo, no = evaluar_config(datos, cfg)
        if ni < args.min_ops or no < args.min_ops // 3:
            continue
        res.append({**cfg, "wr_in": wi, "n_in": ni, "wr_out": wo, "n_out": no})
        if (i + 1) % max(1, args.intentos // 10) == 0:
            print(f"    {i + 1:,}/{args.intentos:,} ... validas: {len(res):,}")
    if not res:
        raise SystemExit("ninguna configuracion genero operaciones suficientes")
    R = pd.DataFrame(res)

    print(f"\n{'=' * 92}")
    print(f"RESULTADO DE {len(R):,} CONFIGURACIONES VALIDAS")
    print(f"{'=' * 92}")

    print(f"\n  Las 10 mejores SEGUN EL PASADO (in-sample), que es como se elegiria")
    print(f"  una estrategia si no se validara:\n")
    top = R.nlargest(10, "wr_in")
    print(f"  {'rsi':>4}{'st':>4}{'bb':>4}{'nbb':>5}{'rsi<':>6}{'st<':>5}{'bb<':>6}"
          f"{'adx':>10}{'n_in':>7}{'WR_in':>8}{'n_out':>7}{'WR_out':>8}{'caida':>8}")
    print("  " + "-" * 88)
    for _, r in top.iterrows():
        adx = f"{r.adx_min:.0f}-{r.adx_max:.0f}"
        print(f"  {int(r.p_rsi):>4}{int(r.p_stoch):>4}{int(r.p_bb):>4}{r.n_bb:>5.1f}"
              f"{r.rsi_lo:>6.0f}{r.st_lo:>5.0f}{r.bb_lo:>6.2f}{adx:>10}"
              f"{int(r.n_in):>7,}{r.wr_in:>7.2f}%{int(r.n_out):>7,}{r.wr_out:>7.2f}%"
              f"{r.wr_out - r.wr_in:>+7.2f}")

    print(f"\n  {'-' * 88}")
    objetivo = args.objetivo
    llegan = R[R.wr_in >= objetivo]
    print(f"\n  CONFIGURACIONES QUE ALCANZAN EL {objetivo:.0f}% EN EL PASADO: {len(llegan):,}")
    if len(llegan):
        print(f"    de esas, cuantas mantienen {objetivo:.0f}% en datos nuevos: "
              f"{int((llegan.wr_out >= objetivo).sum()):,}")
        print(f"    WR medio de esas mismas configs en datos nuevos: {llegan.wr_out.mean():.2f}%")
        print(f"    caida media al salir del pasado: {(llegan.wr_out - llegan.wr_in).mean():+.2f} puntos")
    else:
        print(f"    ninguna. El maximo in-sample fue {R.wr_in.max():.2f}%")

    sup = R[R.wr_out >= objetivo]
    print(f"\n  CONFIGURACIONES QUE ALCANZAN EL {objetivo:.0f}% EN DATOS NUEVOS: {len(sup):,}")
    if len(sup):
        print(f"    (revisar su n_out: con muestras pequenas esto es ruido)")
        for _, r in sup.nlargest(5, "n_out").iterrows():
            print(f"      n_out={int(r.n_out):,}  WR_out={r.wr_out:.2f}%  WR_in={r.wr_in:.2f}%")

    print(f"\n  {'-' * 88}")
    print(f"\n  RELACION ENTRE PASADO Y FUTURO")
    c = np.corrcoef(R.wr_in, R.wr_out)[0, 1]
    print(f"    correlacion WR_in vs WR_out: {c:+.3f}")
    print(f"    (si fuera ~0, optimizar sobre el pasado no dice NADA del futuro)")
    print(f"\n    mejor WR_in:  {R.wr_in.max():6.2f}%   ->  esa misma config en datos nuevos: "
          f"{R.loc[R.wr_in.idxmax(), 'wr_out']:6.2f}%")
    print(f"    mejor WR_out: {R.wr_out.max():6.2f}%   (n={int(R.loc[R.wr_out.idxmax(), 'n_out']):,})")

    # lo honesto: elegir por in-sample y ver que sale
    elegidas = R.nlargest(max(1, len(R) // 100), "wr_in")
    print(f"\n    Si eligieras el mejor 1% segun el pasado ({len(elegidas)} configs),")
    print(f"    obtendrias de media {elegidas.wr_out.mean():.2f}% en datos nuevos.")
    print(f"    Breakeven: {BREAKEVEN:.2f}%")

    R.to_csv("optimizacion_resultados.csv", index=False)
    print(f"\n  Resultados completos en optimizacion_resultados.csv")


if __name__ == "__main__":
    main()
