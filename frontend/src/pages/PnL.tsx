import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from "recharts"
import { api } from "../api"
import { useData } from "../hooks/useData"
import MetricCard from "../components/MetricCard"

function formatUsd(n: number) {
  const abs = Math.abs(n)
  const sign = n < 0 ? "-" : ""
  return `${sign}$${abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function PnL() {
  const { data: pnl, error: pnlErr } = useData(api.pnl)
  const { data: daily, error: dErr } = useData(() => api.dailyPnl(14))
  const { data: unwinds, error: uErr } = useData(() => api.unwinds(20))

  const error = pnlErr || dErr || uErr

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">P&L</h1>
      {error && <div className="rounded-lg border border-down/30 bg-down/10 px-4 py-3 text-sm text-down">{error}</div>}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-4">
        <MetricCard label="Realized P&L" value={formatUsd(pnl?.realized_pnl || 0)} />
        <MetricCard label="Stop-loss damage" value={formatUsd(pnl?.stop_loss_total || 0)} />
        <MetricCard label="Net" value={formatUsd(pnl?.net || 0)} deltaPositive={pnl ? pnl.net >= 0 : null} />
        <MetricCard label="Fills / Unwinds / Stops" value={`${pnl?.num_fills || 0} / ${pnl?.num_unwinds || 0} / ${pnl?.num_stops || 0}`} />
      </div>

      <div className="card">
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-slate-400">Daily unwind P&L</h2>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={daily || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis dataKey="day" stroke="#64748b" tick={{ fontSize: 11 }} />
              <YAxis stroke="#64748b" tick={{ fontSize: 11 }} tickFormatter={(v) => `$${v.toFixed(0)}`} />
              <Tooltip contentStyle={{ backgroundColor: "#0f172a", border: "1px solid #1e293b" }} />
              <Legend />
              <Bar dataKey="gains" name="Gains" fill="#22c55e" radius={[4, 4, 0, 0]} />
              <Bar dataKey="losses" name="Losses" fill="#ef4444" radius={[4, 4, 0, 0]} />
              <Bar dataKey="net_pnl" name="Net" fill="#3b82f6" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="card">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Recent unwinds</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="pb-2">Time</th>
                <th className="pb-2">Market</th>
                <th className="pb-2 text-right">P&L</th>
                <th className="pb-2 text-right">Hold hrs</th>
                <th className="pb-2 text-right">Value</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {(unwinds || []).map((u) => (
                <tr key={`${u.condition_id}-${u.ts}`}>
                  <td className="py-2 text-slate-400">{u.time}</td>
                  <td className="py-2">{u.question.slice(0, 45)}{u.question.length > 45 ? "..." : ""}</td>
                  <td className={`py-2 text-right font-medium ${u.pnl >= 0 ? "text-up" : "text-down"}`}>
                    {formatUsd(u.pnl)}
                  </td>
                  <td className="py-2 text-right">{u.hold_hours}h</td>
                  <td className="py-2 text-right">{formatUsd(u.usd_value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
