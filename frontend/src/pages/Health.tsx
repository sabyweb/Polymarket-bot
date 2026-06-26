import { useState } from "react"
import { api } from "../api"
import { useData } from "../hooks/useData"
import MetricCard from "../components/MetricCard"
import StatusBadge from "../components/StatusBadge"

function isStale(since: string | null): boolean {
  if (!since) return false
  if (since.includes("d ago")) {
    const days = parseFloat(since.replace("d ago", ""))
    return !isNaN(days) && days > 7
  }
  return false
}

export default function Health() {
  const { data: health, error: hErr } = useData(api.health)
  const [service, setService] = useState("polymarket-farmer")
  const { data: logs, error: lErr } = useData(() => api.logs(service, 100), 30000)

  const error = hErr || lErr

  const primaryState = health?.kill_active ? "KILLED" : health?.safety_state || "UNKNOWN"
  const primaryReason = health?.kill_active
    ? `${health.kill_reason}${health.kill_triggered_at ? ` · ${health.kill_triggered_at}` : ""}`
    : health?.safety_reason || "No active alerts"

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">System Health</h1>
      {error && <div className="rounded-lg border border-down/30 bg-down/10 px-4 py-3 text-sm text-down">{error}</div>}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-4">
        <MetricCard
          label="System status"
          value={<StatusBadge state={primaryState} />}
          sub={primaryReason || undefined}
        />
        <MetricCard label="Active orders" value={health?.active_orders || 0} />
        <MetricCard label="Active dumps" value={health?.active_dumps || 0} />
        <MetricCard label="Last fill" value={health?.last_fill || "—"} />
      </div>

      {health && (
        <div className="card">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Status details</h2>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="rounded-lg bg-slate-850 p-3">
              <div className="text-xs uppercase tracking-wider text-slate-500">Live kill state</div>
              <div className="mt-1 text-sm font-medium text-slate-200">
                {health.kill_active ? `Active · ${health.kill_reason}` : "Inactive"}
              </div>
              {health.kill_triggered_at && (
                <div className="mt-1 text-xs text-slate-500">Triggered {health.kill_triggered_at}</div>
              )}
            </div>
            <div className="rounded-lg bg-slate-850 p-3">
              <div className="text-xs uppercase tracking-wider text-slate-500">
                Legacy safety state {isStale(health.safety_since) && "· stale"}
              </div>
              <div className="mt-1 text-sm font-medium text-slate-200">
                {health.safety_state || "—"}
              </div>
              <div className="mt-1 text-xs text-slate-500">
                {health.safety_reason || "No reason"}
                {health.safety_since && ` · ${health.safety_since}`}
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Heartbeats</h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {Object.entries(health?.heartbeats || {}).map(([name, ts]) => (
            <div key={name} className="rounded-lg bg-slate-850 p-3">
              <div className="text-xs uppercase tracking-wider text-slate-500">{name}</div>
              <div className="mt-1 text-sm font-medium text-slate-200">{ts || "—"}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400">Live logs</h2>
          <select
            value={service}
            onChange={(e) => setService(e.target.value)}
            className="rounded-md bg-slate-800 px-2 py-1 text-sm text-slate-200 outline-none ring-0"
          >
            <option value="polymarket-farmer">polymarket-farmer</option>
            <option value="polymarket-oversight">polymarket-oversight</option>
          </select>
        </div>
        <div className="max-h-96 overflow-y-auto rounded-lg bg-slate-950 p-3 font-mono text-xs leading-5">
          {(logs || []).map((l, i) => (
            <div
              key={i}
              className={`mb-1 border-b border-slate-900 pb-1 ${
                l.level === "ERROR"
                  ? "text-down"
                  : l.level === "WARNING" || l.level === "WARN"
                  ? "text-warn"
                  : "text-slate-400"
              }`}
            >
              <span className="text-slate-600">{l.ts}</span> {l.message}
            </div>
          ))}
          {!logs?.length && <div className="text-slate-600">No logs</div>}
        </div>
      </div>
    </div>
  )
}
