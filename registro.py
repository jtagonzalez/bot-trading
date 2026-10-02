"""
REGISTRO — captura de datos de la validacion en PRACTICE.

Guarda TODO lo necesario para responder despues, sin tener que re-descargar ni
re-ejecutar nada:

  * cada evaluacion del modelo, se haya operado o no (y por que no)
  * la ventana OHLC cruda de la decision  -> permite inventar indicadores nuevos
    meses despues y probarlos sobre las decisiones que ya se tomaron
  * el precio de entrada REAL frente al que asumio el backtest -> mide deslizamiento
  * el precio de SALIDA y la vela de resultado completa -> mide si el feed en vivo
    se comporta como el historico
  * la latencia entre el cierre de vela y la orden -> mide si llegamos tarde

Diseno: tres tablas y varias vistas. Las vistas son lo que se consulta para
analizar; las tablas guardan el dato crudo sin interpretarlo.

Uso desde el bot:
    from registro import Registro
    reg = Registro("live.db")
    run_id = reg.iniciar_run(config, modelo_info)
    pid = reg.registrar_prediccion(...)          # siempre, se opere o no
    reg.registrar_entrada(pid, ...)              # si se opera
    reg.registrar_salida(pid, ...)               # al vencer

Uso para analizar:
    py registro.py --resumen
    py registro.py --exportar datos.csv
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone

DB_DEFECTO = "live.db"


ESQUEMA = """
-- Una fila por arranque del bot. Permite comparar configuraciones.
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_inicio         TEXT NOT NULL,
    ts_fin            TEXT,
    tipo_cuenta       TEXT,            -- PRACTICE / REAL
    modelo_nombre     TEXT,
    modelo_hash       TEXT,            -- identifica la version exacta del modelo
    modelo_entrenado  TEXT,            -- hasta que fecha vio datos
    umbral_confianza  REAL,            -- el umbral fijado en entrenamiento
    timeframe         INTEGER,
    horas_operativas  TEXT,            -- ej "20-23"
    monto             REAL,
    wr_esperado       REAL,            -- lo que predice el backtest: 59.67
    config_json       TEXT,
    notas             TEXT
);

-- Una fila por CADA evaluacion del modelo, se opere o no.
-- Registrar las no operadas es lo que permite saber despues si el umbral
-- estaba bien puesto.
CREATE TABLE IF NOT EXISTS predicciones (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    ts_vela           INTEGER NOT NULL,   -- epoch de apertura de la vela de decision
    ts_decision       TEXT NOT NULL,      -- cuando se evaluo (ISO, UTC)
    activo            TEXT NOT NULL,
    timeframe         INTEGER NOT NULL,
    hora_utc          INTEGER,
    dia_semana        INTEGER,

    -- que dijo el modelo
    prob              REAL NOT NULL,      -- probabilidad de que suba
    confianza         REAL NOT NULL,      -- |prob - 0.5|
    umbral            REAL NOT NULL,
    direccion         TEXT NOT NULL,      -- call / put
    operada           INTEGER NOT NULL,   -- 1 si se envio orden
    motivo_no_operada TEXT,               -- baja_confianza / fuera_horario /
                                          -- activo_cerrado / payout_bajo / limite_simultaneas

    -- contexto de mercado en el momento de decidir
    payout_ofrecido   REAL,
    cierre_vela       REAL NOT NULL,      -- precio que el backtest asume como entrada

    -- datos crudos para reanalisis futuro
    ventana_ohlc      TEXT NOT NULL,      -- JSON: [[o,h,l,c],...] ultimas N velas
    indicadores       TEXT NOT NULL       -- JSON: {nombre: valor}
);

-- Solo las operadas. Separada para que predicciones quede limpia.
CREATE TABLE IF NOT EXISTS operaciones (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    prediccion_id     INTEGER NOT NULL UNIQUE REFERENCES predicciones(id),
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    order_id          TEXT,

    -- entrada
    ts_entrada        TEXT,
    precio_entrada    REAL,               -- el REAL, no el asumido
    latencia_ms       INTEGER,            -- desde el cierre de vela hasta la orden
    monto             REAL,
    payout_contratado REAL,

    -- salida
    ts_salida         TEXT,
    precio_salida     REAL,               -- lo que pediste: sin esto no hay analisis
    resultado         TEXT,               -- win / loss / tie / error
    ganancia          REAL,
    payout_real       REAL,               -- cobrado / apostado; puede diferir del ofrecido

    -- la vela de resultado completa, para comparar feed vivo vs historico
    vela_resultado    TEXT,               -- JSON {o,h,l,c}
    error_detalle     TEXT
);

CREATE INDEX IF NOT EXISTS ix_pred_run    ON predicciones(run_id);
CREATE INDEX IF NOT EXISTS ix_pred_ts     ON predicciones(ts_vela);
CREATE INDEX IF NOT EXISTS ix_pred_activo ON predicciones(activo);
CREATE INDEX IF NOT EXISTS ix_pred_oper   ON predicciones(operada);
CREATE INDEX IF NOT EXISTS ix_op_pred     ON operaciones(prediccion_id);
CREATE INDEX IF NOT EXISTS ix_op_res      ON operaciones(resultado);
"""

# Vistas: lo que se consulta para analizar. Cambiarlas no toca el dato crudo.
VISTAS = """
DROP VIEW IF EXISTS v_operaciones;
CREATE VIEW v_operaciones AS
SELECT
    o.id                AS op_id,
    p.id                AS pred_id,
    p.run_id,
    p.activo,
    p.ts_vela,
    datetime(p.ts_vela,'unixepoch')        AS vela_utc,
    p.hora_utc,
    p.dia_semana,
    p.direccion,
    p.prob,
    p.confianza,
    p.cierre_vela,
    o.precio_entrada,
    -- deslizamiento en puntos: cuanto peor (o mejor) entramos de lo asumido
    (o.precio_entrada - p.cierre_vela)     AS desliz_bruto,
    o.latencia_ms,
    o.precio_salida,
    o.resultado,
    o.ganancia,
    o.monto,
    p.payout_ofrecido,
    o.payout_real,
    CASE WHEN o.resultado='win' THEN 1
         WHEN o.resultado='loss' THEN 0 END AS acierto,
    -- lo que HABRIA pasado segun el supuesto del backtest (entrar al cierre de vela)
    CASE WHEN o.precio_salida IS NULL THEN NULL
         WHEN p.direccion='call' AND o.precio_salida > p.cierre_vela THEN 1
         WHEN p.direccion='put'  AND o.precio_salida < p.cierre_vela THEN 1
         WHEN o.precio_salida = p.cierre_vela THEN NULL
         ELSE 0 END                         AS acierto_teorico
FROM operaciones o
JOIN predicciones p ON p.id = o.prediccion_id;

DROP VIEW IF EXISTS v_resumen_run;
CREATE VIEW v_resumen_run AS
SELECT
    r.id, r.ts_inicio, r.modelo_nombre, r.wr_esperado, r.monto,
    COUNT(v.op_id)                                        AS operaciones,
    SUM(v.acierto)                                        AS aciertos,
    ROUND(100.0*SUM(v.acierto)/NULLIF(SUM(CASE WHEN v.acierto IS NOT NULL THEN 1 END),0),2) AS wr_real,
    ROUND(SUM(v.ganancia),2)                              AS ganancia,
    ROUND(AVG(v.latencia_ms),0)                           AS latencia_media_ms,
    ROUND(AVG(v.payout_real)*100,2)                       AS payout_medio
FROM runs r LEFT JOIN v_operaciones v ON v.run_id = r.id
GROUP BY r.id;

-- Cada evaluacion con su desenlace REAL, se operara o no. Es la vista que
-- convierte las señales descartadas en datos etiquetados: sin ella solo
-- sabemos que paso con el 12% que llego a operarse.
DROP VIEW IF EXISTS v_predicciones;
CREATE VIEW v_predicciones AS
SELECT
    p.id, p.run_id, p.activo, p.ts_vela,
    datetime(p.ts_vela,'unixepoch')        AS vela_utc,
    p.hora_utc, p.dia_semana, p.direccion, p.prob, p.confianza, p.umbral,
    p.operada, p.motivo_no_operada, p.payout_ofrecido,
    p.cierre_vela, p.cierre_resultado,
    p.resultado_virtual,
    CASE WHEN p.resultado_virtual='win'  THEN 1
         WHEN p.resultado_virtual='loss' THEN 0 END AS acierto_virtual,
    -- cuanto se movio el precio, con signo a favor de la direccion apostada
    CASE WHEN p.cierre_resultado IS NULL THEN NULL
         WHEN p.direccion='call' THEN p.cierre_resultado - p.cierre_vela
         ELSE p.cierre_vela - p.cierre_resultado END AS movimiento_favor,
    o.resultado                            AS resultado_real,
    o.ganancia
FROM predicciones p
LEFT JOIN operaciones o ON o.prediccion_id = p.id;

DROP VIEW IF EXISTS v_umbral;
CREATE VIEW v_umbral AS
SELECT
    run_id,
    CASE WHEN operada=1 THEN 'operada' ELSE 'descartada' END AS grupo,
    COUNT(*)                                                 n,
    ROUND(AVG(confianza),5)                                  confianza_media,
    SUM(CASE WHEN resultado_virtual='win' THEN 1 ELSE 0 END) aciertos,
    SUM(CASE WHEN resultado_virtual IN ('win','loss') THEN 1 ELSE 0 END) resueltas,
    ROUND(100.0*SUM(CASE WHEN resultado_virtual='win' THEN 1 ELSE 0 END)
          / NULLIF(SUM(CASE WHEN resultado_virtual IN ('win','loss')
                            THEN 1 ELSE 0 END),0), 2)        wr_virtual
FROM predicciones GROUP BY run_id, grupo;

DROP VIEW IF EXISTS v_descartadas;
CREATE VIEW v_descartadas AS
SELECT run_id, motivo_no_operada, COUNT(*) n,
       ROUND(AVG(confianza),4) confianza_media
FROM predicciones WHERE operada = 0
GROUP BY run_id, motivo_no_operada;
"""


class Registro:
    """Capa de escritura. Thread-safe: el bot opera varios activos en paralelo."""

    def __init__(self, db=DB_DEFECTO):
        self.db = db
        self._lock = threading.Lock()
        self._run_id = None
        with self._conn() as c:
            c.executescript(ESQUEMA)
            self._migrar(c)
            c.executescript(VISTAS)

    @staticmethod
    def _migrar(c):
        """Columnas anadidas despues de la primera version del esquema.

        El desenlace virtual: que HABRIA pasado con cada prediccion, tambien
        con las que no se operaron. Sin esto solo tenemos etiqueta del ~12% de
        las señales y no hay forma de saber si el umbral esta bien puesto.
        """
        tiene = {f["name"] for f in c.execute("PRAGMA table_info(predicciones)")}
        nuevas = {
            "cierre_resultado": "REAL",     # cierre de la vela que decide
            "resultado_virtual": "TEXT",    # win / loss / tie
            "ts_resuelta": "TEXT",
        }
        for col, tipo in nuevas.items():
            if col not in tiene:
                c.execute(f"ALTER TABLE predicciones ADD COLUMN {col} {tipo}")

    def _conn(self):
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")   # permite leer mientras el bot escribe
        return c

    @staticmethod
    def _ahora():
        return datetime.now(timezone.utc).isoformat()

    # ── run ──────────────────────────────────────────────

    def iniciar_run(self, *, tipo_cuenta, modelo_nombre, modelo_hash, modelo_entrenado,
                    umbral, timeframe, horas, monto, wr_esperado, config=None, notas="",
                    reanudar=False):
        """reanudar=True reutiliza el ultimo run identico en vez de abrir uno nuevo.

        Importa cuando el proceso se reinicia solo tras colgarse: sin esto, una
        noche con tres reinicios produce tres runs de 9 evaluaciones cada uno
        en vez de uno de 27, y cada tabla por run se queda sin muestra.
        """
        if reanudar:
            with self._lock, self._conn() as c:
                f = c.execute(
                    """SELECT id FROM runs
                       WHERE modelo_hash=? AND notas=? AND tipo_cuenta=?
                         AND timeframe=? AND horas_operativas=?
                       ORDER BY id DESC LIMIT 1""",
                    (modelo_hash, notas, tipo_cuenta, timeframe, horas)).fetchone()
                if f:
                    self._run_id = f["id"]
                    c.execute("UPDATE runs SET ts_fin=NULL WHERE id=?", (self._run_id,))
                    return self._run_id
        with self._lock, self._conn() as c:
            cur = c.execute(
                """INSERT INTO runs (ts_inicio, tipo_cuenta, modelo_nombre, modelo_hash,
                   modelo_entrenado, umbral_confianza, timeframe, horas_operativas,
                   monto, wr_esperado, config_json, notas)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self._ahora(), tipo_cuenta, modelo_nombre, modelo_hash, modelo_entrenado,
                 umbral, timeframe, horas, monto, wr_esperado,
                 json.dumps(config or {}, default=str), notas))
            self._run_id = cur.lastrowid
        return self._run_id

    def cerrar_run(self):
        if self._run_id is None:
            return
        with self._lock, self._conn() as c:
            c.execute("UPDATE runs SET ts_fin=? WHERE id=?", (self._ahora(), self._run_id))

    # ── prediccion ───────────────────────────────────────

    def registrar_prediccion(self, *, ts_vela, activo, timeframe, prob, umbral,
                             direccion, operada, cierre_vela, ventana_ohlc,
                             indicadores, payout=None, motivo_no_operada=None,
                             hora_utc=None, dia_semana=None):
        """Se llama SIEMPRE, tanto si se opera como si no.

        Registrar las descartadas es lo que permitira decidir despues si el
        umbral de confianza estaba bien puesto.
        """
        dt = datetime.fromtimestamp(ts_vela, timezone.utc)
        with self._lock, self._conn() as c:
            cur = c.execute(
                """INSERT INTO predicciones
                   (run_id, ts_vela, ts_decision, activo, timeframe, hora_utc, dia_semana,
                    prob, confianza, umbral, direccion, operada, motivo_no_operada,
                    payout_ofrecido, cierre_vela, ventana_ohlc, indicadores)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self._run_id, int(ts_vela), self._ahora(), activo, timeframe,
                 hora_utc if hora_utc is not None else dt.hour,
                 dia_semana if dia_semana is not None else dt.weekday(),
                 float(prob), abs(float(prob) - 0.5), float(umbral), direccion,
                 1 if operada else 0, motivo_no_operada, payout, float(cierre_vela),
                 json.dumps(ventana_ohlc), json.dumps(indicadores, default=float)))
            return cur.lastrowid

    # ── operacion ────────────────────────────────────────

    def registrar_entrada(self, prediccion_id, *, order_id, precio_entrada,
                          monto, payout_contratado, latencia_ms=None):
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO operaciones
                   (prediccion_id, run_id, order_id, ts_entrada, precio_entrada,
                    latencia_ms, monto, payout_contratado)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (prediccion_id, self._run_id, str(order_id), self._ahora(),
                 precio_entrada, latencia_ms, monto, payout_contratado))

    def resolver_virtuales(self, activo, timeframe, velas):
        """Rellena el desenlace de las predicciones cuya vela de resultado ya cerro.

        `velas` son las que el bot acaba de pedir para decidir, asi que esto no
        cuesta ni una llamada extra a la API.

        La vela que decide es la SIGUIENTE a la de la señal: la decision se toma
        al cierre de `ts_vela` (o sea, en ts_vela + tf) y el desenlace se conoce
        en ts_vela + 2*tf.

        Los EMPATES se guardan como 'tie', nunca como 'loss'. Contar un empate
        como perdida de la CALL fabrica edges falsos de hasta 11 puntos: la baja
        volatilidad produce muchos mas empates (12,6% en el quintil mas calmo
        frente a 2,4% en el mas volatil), asi que toda regla de baja volatilidad
        pareceria un PUT ganador.
        """
        cierres = {int(v["from"]): float(v["close"]) for v in (velas or [])}
        if not cierres:
            return 0
        ahora = int(datetime.now(timezone.utc).timestamp())
        with self._lock, self._conn() as c:
            filas = c.execute(
                """SELECT id, ts_vela, direccion, cierre_vela FROM predicciones
                   WHERE activo=? AND timeframe=? AND resultado_virtual IS NULL
                     AND ts_vela + 2*? <= ?""",
                (activo, timeframe, timeframe, ahora)).fetchall()
            pend = []
            for f in filas:
                cierre = cierres.get(int(f["ts_vela"]) + timeframe)
                if cierre is None:
                    continue            # esa vela ya no viene en el lote
                if cierre == f["cierre_vela"]:
                    r = "tie"
                else:
                    subio = cierre > f["cierre_vela"]
                    r = "win" if subio == (f["direccion"] == "call") else "loss"
                pend.append((cierre, r, self._ahora(), f["id"]))
            if pend:
                c.executemany(
                    """UPDATE predicciones SET cierre_resultado=?, resultado_virtual=?,
                       ts_resuelta=? WHERE id=?""", pend)
            return len(pend)

    def registrar_rechazo(self, prediccion_id, *, motivo, order_id=None):
        """Una orden que el broker no acepto.

        Hace falta una fila propia: `registrar_salida` hace UPDATE sobre
        `operaciones`, y si la orden nunca llego a abrirse no hay nada que
        actualizar. El 22/09 se rechazaron 12 ordenes seguidas y ninguna dejo
        rastro en la base — solo aparecian en el log, sin el motivo.
        """
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO operaciones
                   (prediccion_id, run_id, order_id, ts_entrada, monto,
                    resultado, ganancia, error_detalle)
                   VALUES (?,?,?,?,0,'rechazada',0,?)""",
                (prediccion_id, self._run_id, str(order_id) if order_id else None,
                 self._ahora(), motivo))

    def cerrar_huerfanas(self, timeframe, margen=3):
        """Marca como perdidas las operaciones que vencieron sin resultado.

        Pasa cuando el bot se detiene con una orden en vuelo: nadie queda
        esperando el desenlace y la fila se queda con resultado NULL para
        siempre. En el panel se ven como una operacion abierta con 0 segundos
        restantes, que no es ni verdad ni util.
        """
        limite = timeframe * margen
        with self._lock, self._conn() as c:
            filas = c.execute(
                """SELECT o.prediccion_id FROM operaciones o
                   JOIN predicciones p ON p.id = o.prediccion_id
                   WHERE o.resultado IS NULL
                     AND p.ts_vela + ? < strftime('%s','now')""",
                (limite,)).fetchall()
            if not filas:
                return 0
            ids = [f["prediccion_id"] for f in filas]
            c.executemany(
                """UPDATE operaciones SET resultado='error', ganancia=0,
                       ts_salida=?, error_detalle='vencio sin resultado: el bot se
detuvo antes del vencimiento' WHERE prediccion_id=?""",
                [(self._ahora(), i) for i in ids])
            return len(ids)

    def registrar_salida(self, prediccion_id, *, precio_salida, resultado,
                         ganancia, vela_resultado=None, error=None):
        """precio_salida es el dato que faltaba en el bot anterior.

        Sin el no se puede distinguir 'el modelo se equivoco' de 'el modelo
        acerto pero entramos a mal precio'.
        """
        monto = None
        with self._lock, self._conn() as c:
            fila = c.execute("SELECT monto FROM operaciones WHERE prediccion_id=?",
                             (prediccion_id,)).fetchone()
            if fila:
                monto = fila["monto"]
            payout_real = None
            if monto and ganancia is not None and resultado == "win" and monto > 0:
                payout_real = ganancia / monto
            c.execute(
                """UPDATE operaciones
                   SET ts_salida=?, precio_salida=?, resultado=?, ganancia=?,
                       payout_real=?, vela_resultado=?, error_detalle=?
                   WHERE prediccion_id=?""",
                (self._ahora(), precio_salida, resultado, ganancia, payout_real,
                 json.dumps(vela_resultado) if vela_resultado else None,
                 error, prediccion_id))


# ── consulta rapida desde linea de comandos ──────────────

def resumen(db=DB_DEFECTO):
    import pandas as pd
    c = sqlite3.connect(db)
    print("=== RUNS ===")
    r = pd.read_sql("SELECT * FROM v_resumen_run", c)
    print(r.to_string(index=False) if len(r) else "  (sin runs)")
    print("\n=== SENALES DESCARTADAS ===")
    d = pd.read_sql("SELECT * FROM v_descartadas", c)
    print(d.to_string(index=False) if len(d) else "  (ninguna)")
    n = pd.read_sql("SELECT COUNT(*) n FROM predicciones", c).n[0]
    o = pd.read_sql("SELECT COUNT(*) n FROM operaciones WHERE resultado IS NOT NULL", c).n[0]
    print(f"\npredicciones registradas: {n:,}   operaciones cerradas: {o:,}")
    c.close()


def exportar(salida, db=DB_DEFECTO):
    import pandas as pd
    c = sqlite3.connect(db)
    df = pd.read_sql("SELECT * FROM v_operaciones", c)
    c.close()
    df.to_csv(salida, index=False)
    print(f"{len(df):,} operaciones exportadas a {salida}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFECTO)
    ap.add_argument("--resumen", action="store_true")
    ap.add_argument("--exportar", default=None)
    ap.add_argument("--crear", action="store_true", help="solo crear el esquema")
    a = ap.parse_args()
    if a.crear:
        Registro(a.db)
        print(f"esquema creado en {a.db}")
    elif a.exportar:
        exportar(a.exportar, a.db)
    else:
        resumen(a.db)
