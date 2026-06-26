import { AlertTriangle, RefreshCw } from "lucide-react"
import { api } from "../api"
import { useData } from "../hooks/useData"
import MetricCard from "../components/MetricCard"
import StatusBadge from "../components/StatusBadge"
import CohortBadge from "../components/CohortBadge"

function formatUsd(n: number) {
  const abs = Math.abs(n)
  const sign = n < 0 ? "-" : ""
  return `${sign}$${abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function formatPct(n: number | null) {
  if (n === null) return "n/a"
  return `${(n * 100).toFixed(2)}%`
}

export default function CommandCenter() {
  const { data: health, error: hErr, loading: hLoading, reload: hReload } = useData(api.health)
  const { data: port, error: pErr } = useData(api.portfolio)
  const { data: pnl, error: pnlErr } = useData(api.pnl)
  const { data: alloc, error: aErr } = useData(api.allocation)
  const { data: cohorts, error: cErr } = useData(api.cohortLatest)
  const { data: fills, error: fErr } = useData(() => api.fills(8, 24))
  const { data: orders, error: oErr } = useData(api.orders)

  const error = hErr || pErr || pnlErr || aErr || cErr || fErr || oErr

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Command Center</h1>
        <button
          onClick={hReload}
          className="flex items-center gap-1.5 rounded-md bg-slate-800 px-3 py-1.5 text-sm font-medium text-slate-200 hover:bg-slate-700"
        >
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 rounded-lg border border-down/30 bg-down/10 px-4 py-3 text-sm text-down">
          <AlertTriangle size={16} />
          {error}
        </div>
      )}

      {/* Safety / kill banner */}
      {health && (
        <div
          className={`flex items-center justify-between rounded-xl border px-4 py-3 ${
            health.kill_active
              ? "border-down/30 bg-down/10"
              : ["OK", "HEALTHY", "NORMAL", "RUNNING"].includes((health.safety_state || "").toUpperCase())
              ? "border-up/30 bg-up/10"
              : ["DEGRADED", "WARN", "WARNING", "PAUSED"].includes(
                  (health.safety_state || "").toUpperCase()
                )
              ? "border-warn/30 bg-warn/10"
              : "border-down/30 bg-down/10"
          }`}
        >
          <div className="flex items-center gap-3">
            {health.kill_active ? (
              <StatusBadge state="KILLED" />
            ) : (
              <StatusBadge state={health.safety_state} />
            )}
            <span className="text-sm text-slate-200">
              {health.kill_active
                ? `${health.kill_reason}${health.kill_triggered_at ? ` · ${health.kill_triggered_at}` : ""}`
                : `${health.safety_reason || "No active alerts"}${health.safety_since ? ` · ${health.safety_since}` : ""}`}
            </span>
          </div>
          <span className="text-xs text-slate-400">DB {health.db_size_mb} MB · {health.db_updated}</span>
        </div>
      )}

      {/* KPI row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Total value"
          value={formatUsd(port?.total || 0)}
          sub={`Cash ${formatUsd(port?.cash || 0)} + Inventory ${formatUsd(port?.inventory_value || 0)}`}
        />
        <MetricCard
          label="Realized P&L"
          value={formatUsd(pnl?.net || 0)}
          delta={pnl ? `${pnl.num_unwinds} unwinds / ${pnl.num_stops} stops` : undefined}
          deltaPositive={pnl ? pnl.net >= 0 : null}
        />
        <MetricCard
          label="Live notional / wallet"
          value={formatUsd(orders?.reduce((s, o) => s + o.notional, 0) || 0)}
          sub={port && port.total ? `Overcommit ratio ${formatPct((orders?.reduce((s, o) => s + o.notional, 0) || 0) / port.total)}` : undefined}
        />
        <MetricCard
          label="Active orders / markets"
          value={`${orders?.length || 0} / ${new Set(orders?.map((o) => o.condition_id)).size || 0}`}
          sub={health ? `Last fill ${health.last_fill || "never"}` : undefined}
        />
      </div>

      {/* Allocation + Cohorts */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="card lg:col-span-1">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">
            Current allocation
          </h2>
          {alloc ? (
            <div className="space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-slate-400">Deploy</span>
                <span className="font-medium">{alloc.num_deploy}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-slate-400">Avoid</span>
                <span className="font-medium">{alloc.num_avoid}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-slate-400">Capital deployed</span>
                <span className="font-medium">{formatUsd(alloc.total_capital_deployed)}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-slate-400">Plan generated</span>
                <span className="font-medium">{alloc.generated_at ? new Date(alloc.generated_at).toLocaleString() : "—"}</span>
              </div>
            </div>
          ) : (
            <div className="text-sm text-slate-500">{hLoading ? "Loading..." : "No allocation file"}</div>
          )}
        </div>

        <div className="card lg:col-span-2">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">
            A/B cohorts (24h)
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="pb-2">Cohort</th>
                  <th className="pb-2 text-right">Net P&L</th>
                  <th className="pb-2 text-right">Reward</th>
                  <th className="pb-2 text-right">Trading loss</th>
                  <th className="pb-2 text-right">Fills</th>
                  <th className="pb-2 text-right">Return</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {(cohorts || []).map((c) => (
                  <tr key={c.cohort}>
                    <td className="py-2">
                      <CohortBadge cohort={c.cohort} />
                    </td>
                    <td className={`py-2 text-right font-medium ${c.net_pnl >= 0 ? "text-up" : "text-down"}`}>
                      {formatUsd(c.net_pnl)}
                    </td>
                    <td className="py-2 text-right text-slate-300">{formatUsd(c.reward_earned)}</td>
                    <td className="py-2 text-right text-slate-300">{formatUsd(c.unwind_pnl)}</td>
                    <td className="py-2 text-right text-slate-300">{c.fill_count}</td>
                    <td className="py-2 text-right text-slate-300">{c.return_pct.toFixed(3)}%</td>
                  </tr>
                ))}
                {!cohorts?.length && (
                  <tr>
                    <td colSpan={6} className="py-4 text-center text-slate-500">
                      No cohort data yet
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Liveness + Recent fills */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="card lg:col-span-1">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Liveness</h2>
          <div className="space-y-3">
            {[
              ["Oversight", health?.heartbeats?.oversight || health?.last_agent],
              ["Farmer", health?.heartbeats?.farmer || health?.last_cycle],
              ["Last fill", health?.last_fill],
              ["Last order", health?.last_order],
            ].map(([label, value]) => (
              <div key={label} className="flex items-center justify-between text-sm">
                <span className="text-slate-400">{label}</span>
                <span className="font-medium text-slate-200">{value || "—"}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="card lg:col-span-2">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Recent fills</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="pb-2">Time</th>
                  <th className="pb-2">Market</th>
                  <th className="pb-2">Cohort</th>
                  <th className="pb-2 text-right">Value</th>
                  <th className="pb-2 text-right">Slippage</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {(fills || []).map((f) => (
                  <tr key={`${f.condition_id}-${f.ts}-${f.side}`}>
                    <td className="py-2 text-slate-400">{f.time}</td>
                    <td className="py-2">{f.question.slice(0, 45)}{f.question.length > 45 ? "..." : ""}</td>
                    <td className="py-2">
                      <CohortBadge cohort={f.cohort} />
                    </td>
                    <td className="py-2 text-right">{formatUsd(f.usd_value)}</td>
                    <td className={`py-2 text-right ${f.slippage <= 0 ? "text-up" : "text-down"}`}>
                      {f.slippage.toFixed(4)}
                    </td>
                  </tr>
                ))}
                {!fills?.length && (
                  <tr>
                    <td colSpan={5} className="py-4 text-center text-slate-500">
                      No recent fills
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}
