import { useState, useEffect, useCallback } from "react";

const MOCK_DATA = {
  hoy: {
    fecha: new Date().toISOString().slice(0, 10),
    total_operaciones: 23,
    wins: 16,
    losses: 6,
    ties: 1,
    winrate: 69.6,
    ganancia_total: 34.80,
    racha_actual: 3,
    racha_max_win: 5,
    racha_max_loss: 2,
    por_activo: {
      "EURUSD-OTC": { wins: 8, losses: 2, ganancia: 18.40 },
      "GBPUSD-OTC": { wins: 4, losses: 2, ganancia: 8.20 },
      "USDJPY-OTC": { wins: 4, losses: 2, ganancia: 8.20 },
    },
    por_paso_martingala: {
      1: { wins: 12, losses: 4, total: 16 },
      2: { wins: 3, losses: 1, total: 4 },
      3: { wins: 1, losses: 1, total: 2 },
      4: { wins: 0, losses: 0, total: 1 },
    },
    operaciones: [
      { id: 23, timestamp: "2025-06-15T14:32:00", activo: "EURUSD-OTC", direccion: "call", monto: 1, payout: 87, resultado: "win", ganancia: 0.87, paso_martingala: 1, balance_despues: 1034.80 },
      { id: 22, timestamp: "2025-06-15T14:28:00", activo: "EURUSD-OTC", direccion: "call", monto: 1, payout: 87, resultado: "win", ganancia: 0.87, paso_martingala: 1, balance_despues: 1033.93 },
      { id: 21, timestamp: "2025-06-15T14:25:00", activo: "GBPUSD-OTC", direccion: "put", monto: 2.4, payout: 82, resultado: "win", ganancia: 1.97, paso_martingala: 2, balance_despues: 1033.06 },
      { id: 20, timestamp: "2025-06-15T14:22:00", activo: "GBPUSD-OTC", direccion: "put", monto: 1, payout: 82, resultado: "loss", ganancia: -1, paso_martingala: 1, balance_despues: 1031.09 },
      { id: 19, timestamp: "2025-06-15T14:18:00", activo: "USDJPY-OTC", direccion: "call", monto: 1, payout: 85, resultado: "win", ganancia: 0.85, paso_martingala: 1, balance_despues: 1032.09 },
      { id: 18, timestamp: "2025-06-15T14:15:00", activo: "EURUSD-OTC", direccion: "call", monto: 1, payout: 87, resultado: "loss", ganancia: -1, paso_martingala: 1, balance_despues: 1031.24 },
      { id: 17, timestamp: "2025-06-15T14:11:00", activo: "EURUSD-OTC", direccion: "put", monto: 1, payout: 87, resultado: "win", ganancia: 0.87, paso_martingala: 1, balance_despues: 1032.24 },
      { id: 16, timestamp: "2025-06-15T14:08:00", activo: "GBPUSD-OTC", direccion: "call", monto: 5.76, payout: 82, resultado: "win", ganancia: 4.72, paso_martingala: 3, balance_despues: 1031.37 },
      { id: 15, timestamp: "2025-06-15T14:05:00", activo: "GBPUSD-OTC", direccion: "call", monto: 2.4, payout: 82, resultado: "loss", ganancia: -2.4, paso_martingala: 2, balance_despues: 1026.65 },
      { id: 14, timestamp: "2025-06-15T14:02:00", activo: "GBPUSD-OTC", direccion: "call", monto: 1, payout: 82, resultado: "loss", ganancia: -1, paso_martingala: 1, balance_despues: 1029.05 },
    ],
  },
  global: {
    total_operaciones: 156,
    wins: 102,
    losses: 51,
    winrate: 65.4,
    ganancia_total: 187.30,
    mejor_dia: { fecha: "2025-06-12", ganancia: 52.40 },
    peor_dia: { fecha: "2025-06-10", ganancia: -18.60 },
    dias_operados: 7,
    promedio_diario: 26.76,
  },
  ultima_actualizacion: new Date().toISOString(),
  config: {
    monto_base: 1,
    martingala: true,
    multiplicador: 2.4,
    max_pasos: 6,
    payout_minimo: 80,
  },
};

// Sparkline mini chart
function Sparkline({ data, color, height = 32, width = 100 }) {
  if (!data || data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const points = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * width;
      const y = height - ((v - min) / range) * (height - 4) - 2;
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function StatCard({ label, value, sub, accent, icon }) {
  return (
    <div
      style={{
        background: "rgba(255,255,255,0.03)",
        border: "1px solid rgba(255,255,255,0.06)",
        borderRadius: 14,
        padding: "20px 22px",
        flex: "1 1 180px",
        minWidth: 160,
      }}
    >
      <div
        style={{
          fontSize: 11,
          letterSpacing: "0.08em",
          color: "rgba(255,255,255,0.4)",
          textTransform: "uppercase",
          marginBottom: 8,
        }}
      >
        {icon} {label}
      </div>
      <div
        style={{
          fontSize: 28,
          fontWeight: 700,
          color: accent || "#e0e0e0",
          fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        }}
      >
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: 12, color: "rgba(255,255,255,0.35)", marginTop: 4 }}>
          {sub}
        </div>
      )}
    </div>
  );
}

function WinRateRing({ rate, size = 120 }) {
  const r = (size - 12) / 2;
  const circ = 2 * Math.PI * r;
  const offset = circ - (rate / 100) * circ;
  const color = rate >= 65 ? "#00e676" : rate >= 50 ? "#ffc400" : "#ff1744";

  return (
    <div style={{ position: "relative", width: size, height: size }}>
      <svg width={size} height={size} style={{ transform: "rotate(-90deg)" }}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={8} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={8}
          strokeDasharray={circ}
          strokeDashoffset={offset}
          strokeLinecap="round"
          style={{ transition: "stroke-dashoffset 1s ease" }}
        />
      </svg>
      <div
        style={{
          position: "absolute",
          inset: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <span style={{ fontSize: 26, fontWeight: 800, color, fontFamily: "'JetBrains Mono', monospace" }}>
          {rate.toFixed(1)}%
        </span>
        <span style={{ fontSize: 9, color: "rgba(255,255,255,0.35)", letterSpacing: "0.1em", textTransform: "uppercase" }}>
          Win Rate
        </span>
      </div>
    </div>
  );
}

function MartingaleBar({ data }) {
  if (!data) return null;
  const steps = Object.entries(data).sort(([a], [b]) => Number(a) - Number(b));
  const maxTotal = Math.max(...steps.map(([, v]) => v.total), 1);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {steps.map(([paso, info]) => {
        const wr = info.total > 0 ? ((info.wins / info.total) * 100).toFixed(0) : 0;
        return (
          <div key={paso} style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span
              style={{
                width: 50,
                fontSize: 11,
                color: "rgba(255,255,255,0.5)",
                fontFamily: "monospace",
                textAlign: "right",
              }}
            >
              Paso {paso}
            </span>
            <div style={{ flex: 1, height: 18, background: "rgba(255,255,255,0.04)", borderRadius: 4, overflow: "hidden", display: "flex" }}>
              <div
                style={{
                  width: `${(info.wins / maxTotal) * 100}%`,
                  background: "rgba(0,230,118,0.7)",
                  height: "100%",
                  transition: "width 0.6s ease",
                }}
              />
              <div
                style={{
                  width: `${(info.losses / maxTotal) * 100}%`,
                  background: "rgba(255,23,68,0.7)",
                  height: "100%",
                  transition: "width 0.6s ease",
                }}
              />
            </div>
            <span style={{ width: 70, fontSize: 11, color: "rgba(255,255,255,0.4)", fontFamily: "monospace" }}>
              {info.wins}W {info.losses}L
            </span>
            <span style={{ width: 35, fontSize: 11, color: Number(wr) >= 60 ? "#00e676" : "#ff9100", fontFamily: "monospace" }}>
              {wr}%
            </span>
          </div>
        );
      })}
    </div>
  );
}

function OperationsTable({ ops }) {
  if (!ops || ops.length === 0) return <div style={{ color: "rgba(255,255,255,0.3)", textAlign: "center", padding: 40 }}>Sin operaciones aún</div>;

  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, fontFamily: "'JetBrains Mono', monospace" }}>
        <thead>
          <tr style={{ borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
            {["#", "Hora", "Activo", "Dir", "Monto", "Payout", "Resultado", "P/L", "Paso", "Balance"].map((h) => (
              <th
                key={h}
                style={{
                  padding: "10px 8px",
                  textAlign: "left",
                  color: "rgba(255,255,255,0.3)",
                  fontWeight: 500,
                  fontSize: 10,
                  letterSpacing: "0.06em",
                  textTransform: "uppercase",
                }}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ops.map((op, i) => {
            const isWin = op.resultado === "win";
            const isLoss = op.resultado === "loss";
            const rowBg = i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.015)";

            return (
              <tr key={op.id} style={{ background: rowBg, borderBottom: "1px solid rgba(255,255,255,0.03)" }}>
                <td style={{ padding: "9px 8px", color: "rgba(255,255,255,0.3)" }}>{op.id}</td>
                <td style={{ padding: "9px 8px", color: "rgba(255,255,255,0.6)" }}>
                  {op.timestamp ? new Date(op.timestamp).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" }) : "--"}
                </td>
                <td style={{ padding: "9px 8px", color: "rgba(255,255,255,0.8)" }}>{op.activo?.replace("-OTC", "") || "--"}</td>
                <td style={{ padding: "9px 8px" }}>
                  <span
                    style={{
                      background: op.direccion === "call" ? "rgba(0,230,118,0.15)" : "rgba(255,23,68,0.15)",
                      color: op.direccion === "call" ? "#00e676" : "#ff1744",
                      padding: "2px 8px",
                      borderRadius: 4,
                      fontSize: 10,
                      fontWeight: 600,
                    }}
                  >
                    {op.direccion === "call" ? "▲ CALL" : "▼ PUT"}
                  </span>
                </td>
                <td style={{ padding: "9px 8px", color: "rgba(255,255,255,0.7)" }}>${op.monto?.toFixed(2)}</td>
                <td style={{ padding: "9px 8px", color: "rgba(255,255,255,0.5)" }}>{op.payout}%</td>
                <td style={{ padding: "9px 8px" }}>
                  <span
                    style={{
                      display: "inline-block",
                      width: 8,
                      height: 8,
                      borderRadius: "50%",
                      background: isWin ? "#00e676" : isLoss ? "#ff1744" : "#ffc400",
                      marginRight: 6,
                      boxShadow: isWin ? "0 0 6px rgba(0,230,118,0.5)" : isLoss ? "0 0 6px rgba(255,23,68,0.5)" : "none",
                    }}
                  />
                  <span style={{ color: isWin ? "#00e676" : isLoss ? "#ff1744" : "#ffc400" }}>
                    {op.resultado?.toUpperCase()}
                  </span>
                </td>
                <td
                  style={{
                    padding: "9px 8px",
                    color: op.ganancia >= 0 ? "#00e676" : "#ff1744",
                    fontWeight: 600,
                  }}
                >
                  {op.ganancia >= 0 ? "+" : ""}${op.ganancia?.toFixed(2)}
                </td>
                <td style={{ padding: "9px 8px", color: op.paso_martingala > 1 ? "#ffc400" : "rgba(255,255,255,0.4)" }}>
                  {op.paso_martingala > 1 ? `M${op.paso_martingala}` : "—"}
                </td>
                <td style={{ padding: "9px 8px", color: "rgba(255,255,255,0.6)" }}>
                  ${op.balance_despues?.toFixed(2)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function GoldDashboard() {
  const [data, setData] = useState(MOCK_DATA);
  const [tab, setTab] = useState("hoy");
  const [live, setLive] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(new Date());

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch("/api/stats");
      if (res.ok) {
        const json = await res.json();
        if (json && !json.error) {
          setData(json);
          setLive(true);
        }
      }
    } catch {
      setLive(false);
    }
    setLastUpdate(new Date());
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 5000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const h = data?.hoy || {};
  const g = data?.global || {};
  const cfg = data?.config || {};

  const balanceHistory = (h.operaciones || []).map((o) => o.balance_despues).reverse();
  const plHistory = [];
  let cumPL = 0;
  for (const op of [...(h.operaciones || [])].reverse()) {
    cumPL += op.ganancia || 0;
    plHistory.push(cumPL);
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#0a0a0f",
        color: "#e0e0e0",
        fontFamily: "'DM Sans', 'Segoe UI', system-ui, sans-serif",
        padding: "24px 20px",
        boxSizing: "border-box",
      }}
    >
      <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=JetBrains+Mono:wght@400;600;700;800&display=swap" rel="stylesheet" />

      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 28 }}>
        <div>
          <h1
            style={{
              margin: 0,
              fontSize: 22,
              fontWeight: 700,
              background: "linear-gradient(135deg, #ffc400 0%, #ff9100 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
              letterSpacing: "-0.02em",
            }}
          >
            GOLD 4.0
          </h1>
          <div style={{ fontSize: 11, color: "rgba(255,255,255,0.3)", marginTop: 2 }}>
            Trading Bot Dashboard
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              background: live ? "rgba(0,230,118,0.08)" : "rgba(255,255,255,0.05)",
              border: `1px solid ${live ? "rgba(0,230,118,0.2)" : "rgba(255,255,255,0.08)"}`,
              borderRadius: 20,
              padding: "5px 12px",
              fontSize: 11,
            }}
          >
            <div
              style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                background: live ? "#00e676" : "#666",
                boxShadow: live ? "0 0 8px rgba(0,230,118,0.6)" : "none",
                animation: live ? "pulse 2s infinite" : "none",
              }}
            />
            <span style={{ color: live ? "#00e676" : "#666" }}>{live ? "LIVE" : "DEMO"}</span>
          </div>
          <div style={{ fontSize: 10, color: "rgba(255,255,255,0.25)" }}>
            {lastUpdate.toLocaleTimeString("es")}
          </div>
        </div>
      </div>

      <style>{`
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
      `}</style>

      {/* Top Stats */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 24 }}>
        <StatCard
          label="Operaciones"
          value={h.total_operaciones || 0}
          sub={`${h.wins || 0}W / ${h.losses || 0}L / ${h.ties || 0}E`}
          icon="📊"
        />
        <StatCard
          label="P/L Hoy"
          value={`$${(h.ganancia_total || 0) >= 0 ? "+" : ""}${(h.ganancia_total || 0).toFixed(2)}`}
          accent={(h.ganancia_total || 0) >= 0 ? "#00e676" : "#ff1744"}
          sub={<Sparkline data={plHistory} color={(h.ganancia_total || 0) >= 0 ? "#00e676" : "#ff1744"} width={80} />}
          icon="💰"
        />
        <StatCard
          label="Balance"
          value={
            h.operaciones?.length > 0
              ? `$${h.operaciones[0].balance_despues?.toFixed(2)}`
              : "—"
          }
          sub={<Sparkline data={balanceHistory} color="#7c4dff" width={80} />}
          accent="#b388ff"
          icon="🏦"
        />
        <StatCard
          label="Racha"
          value={h.racha_actual > 0 ? `+${h.racha_actual}` : h.racha_actual || 0}
          accent={h.racha_actual > 0 ? "#00e676" : h.racha_actual < 0 ? "#ff1744" : "#ffc400"}
          sub={`Best: ${h.racha_max_win || 0} | Worst: ${h.racha_max_loss || 0}`}
          icon="🔥"
        />
      </div>

      {/* Middle Row: WinRate + Martingale + Config */}
      <div style={{ display: "flex", gap: 16, marginBottom: 24, flexWrap: "wrap" }}>
        {/* Win Rate */}
        <div
          style={{
            background: "rgba(255,255,255,0.02)",
            border: "1px solid rgba(255,255,255,0.06)",
            borderRadius: 14,
            padding: 24,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            minWidth: 170,
          }}
        >
          <WinRateRing rate={h.winrate || 0} />
          <div style={{ marginTop: 12, fontSize: 11, color: "rgba(255,255,255,0.35)" }}>
            {h.wins || 0} ganadas de {h.total_operaciones || 0}
          </div>
        </div>

        {/* Martingale Analysis */}
        <div
          style={{
            background: "rgba(255,255,255,0.02)",
            border: "1px solid rgba(255,255,255,0.06)",
            borderRadius: 14,
            padding: "20px 22px",
            flex: "1 1 280px",
          }}
        >
          <div
            style={{
              fontSize: 11,
              letterSpacing: "0.08em",
              color: "rgba(255,255,255,0.4)",
              textTransform: "uppercase",
              marginBottom: 14,
            }}
          >
            🔄 Martingala por paso
          </div>
          <MartingaleBar data={h.por_paso_martingala} />
        </div>

        {/* Config */}
        <div
          style={{
            background: "rgba(255,255,255,0.02)",
            border: "1px solid rgba(255,255,255,0.06)",
            borderRadius: 14,
            padding: "20px 22px",
            minWidth: 160,
          }}
        >
          <div
            style={{
              fontSize: 11,
              letterSpacing: "0.08em",
              color: "rgba(255,255,255,0.4)",
              textTransform: "uppercase",
              marginBottom: 14,
            }}
          >
            ⚙️ Configuración
          </div>
          {[
            ["Monto base", `$${cfg.monto_base || 1}`],
            ["Martingala", cfg.martingala ? "Activa" : "Off"],
            ["Multiplicador", `x${cfg.multiplicador || 2.4}`],
            ["Máx pasos", cfg.max_pasos || 6],
            ["Payout mín", `${cfg.payout_minimo || 80}%`],
          ].map(([k, v]) => (
            <div
              key={k}
              style={{
                display: "flex",
                justifyContent: "space-between",
                padding: "6px 0",
                borderBottom: "1px solid rgba(255,255,255,0.03)",
                fontSize: 12,
              }}
            >
              <span style={{ color: "rgba(255,255,255,0.4)" }}>{k}</span>
              <span style={{ color: "rgba(255,255,255,0.8)", fontFamily: "monospace", fontWeight: 600 }}>{v}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Per-Asset Breakdown */}
      {h.por_activo && Object.keys(h.por_activo).length > 0 && (
        <div
          style={{
            background: "rgba(255,255,255,0.02)",
            border: "1px solid rgba(255,255,255,0.06)",
            borderRadius: 14,
            padding: "20px 22px",
            marginBottom: 24,
          }}
        >
          <div
            style={{
              fontSize: 11,
              letterSpacing: "0.08em",
              color: "rgba(255,255,255,0.4)",
              textTransform: "uppercase",
              marginBottom: 14,
            }}
          >
            📈 Rendimiento por activo
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            {Object.entries(h.por_activo).map(([activo, info]) => {
              const wr = ((info.wins / Math.max(info.wins + info.losses, 1)) * 100).toFixed(0);
              return (
                <div
                  key={activo}
                  style={{
                    flex: "1 1 200px",
                    background: "rgba(255,255,255,0.02)",
                    borderRadius: 10,
                    padding: "14px 16px",
                    border: "1px solid rgba(255,255,255,0.04)",
                  }}
                >
                  <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
                    {activo.replace("-OTC", "")}
                    {activo.includes("OTC") && (
                      <span style={{ fontSize: 9, color: "rgba(255,255,255,0.3)", marginLeft: 4 }}>OTC</span>
                    )}
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                    <span style={{ color: "rgba(255,255,255,0.4)" }}>
                      {info.wins}W / {info.losses}L ({wr}%)
                    </span>
                    <span
                      style={{
                        color: info.ganancia >= 0 ? "#00e676" : "#ff1744",
                        fontFamily: "monospace",
                        fontWeight: 700,
                      }}
                    >
                      {info.ganancia >= 0 ? "+" : ""}${info.ganancia.toFixed(2)}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Tabs */}
      <div style={{ display: "flex", gap: 2, marginBottom: 16 }}>
        {[
          ["hoy", "Operaciones Hoy"],
          ["global", "Stats Globales"],
        ].map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            style={{
              padding: "8px 20px",
              borderRadius: "8px 8px 0 0",
              border: "1px solid rgba(255,255,255,0.06)",
              borderBottom: tab === key ? "2px solid #ffc400" : "1px solid transparent",
              background: tab === key ? "rgba(255,255,255,0.04)" : "transparent",
              color: tab === key ? "#ffc400" : "rgba(255,255,255,0.35)",
              fontSize: 12,
              fontWeight: 600,
              cursor: "pointer",
              letterSpacing: "0.03em",
              transition: "all 0.2s",
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      <div
        style={{
          background: "rgba(255,255,255,0.02)",
          border: "1px solid rgba(255,255,255,0.06)",
          borderRadius: "0 14px 14px 14px",
          padding: "4px 0",
          overflow: "hidden",
        }}
      >
        {tab === "hoy" && <OperationsTable ops={h.operaciones} />}

        {tab === "global" && (
          <div style={{ padding: 24 }}>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <StatCard label="Total ops" value={g.total_operaciones || 0} sub={`${g.dias_operados || 0} días`} icon="📊" />
              <StatCard
                label="Win Rate Global"
                value={`${(g.winrate || 0).toFixed(1)}%`}
                accent={(g.winrate || 0) >= 60 ? "#00e676" : "#ffc400"}
                sub={`${g.wins || 0}W / ${g.losses || 0}L`}
                icon="🎯"
              />
              <StatCard
                label="Ganancia Total"
                value={`$${(g.ganancia_total || 0) >= 0 ? "+" : ""}${(g.ganancia_total || 0).toFixed(2)}`}
                accent={(g.ganancia_total || 0) >= 0 ? "#00e676" : "#ff1744"}
                sub={`Promedio: $${(g.promedio_diario || 0).toFixed(2)}/día`}
                icon="💎"
              />
            </div>
            {g.mejor_dia && (
              <div style={{ marginTop: 20, display: "flex", gap: 16, flexWrap: "wrap" }}>
                <div style={{ flex: 1, padding: "14px 18px", background: "rgba(0,230,118,0.05)", borderRadius: 10, border: "1px solid rgba(0,230,118,0.1)" }}>
                  <div style={{ fontSize: 11, color: "rgba(255,255,255,0.4)", marginBottom: 4 }}>🏆 Mejor día</div>
                  <div style={{ fontFamily: "monospace", color: "#00e676", fontWeight: 700 }}>
                    +${g.mejor_dia.ganancia?.toFixed(2)} <span style={{ color: "rgba(255,255,255,0.3)", fontWeight: 400 }}>({g.mejor_dia.fecha})</span>
                  </div>
                </div>
                <div style={{ flex: 1, padding: "14px 18px", background: "rgba(255,23,68,0.05)", borderRadius: 10, border: "1px solid rgba(255,23,68,0.1)" }}>
                  <div style={{ fontSize: 11, color: "rgba(255,255,255,0.4)", marginBottom: 4 }}>📉 Peor día</div>
                  <div style={{ fontFamily: "monospace", color: "#ff1744", fontWeight: 700 }}>
                    ${g.peor_dia.ganancia?.toFixed(2)} <span style={{ color: "rgba(255,255,255,0.3)", fontWeight: 400 }}>({g.peor_dia.fecha})</span>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Footer */}
      <div
        style={{
          textAlign: "center",
          marginTop: 24,
          fontSize: 10,
          color: "rgba(255,255,255,0.15)",
          letterSpacing: "0.05em",
        }}
      >
        GOLD 4.0 v3 — Actualización cada 5s — Datos {live ? "en vivo" : "de demostración"}
      </div>
    </div>
  );
}
