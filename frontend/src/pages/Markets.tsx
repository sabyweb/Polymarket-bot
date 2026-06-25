import { api } from "../api"
import { useData } from "../hooks/useData"
import MetricCard from "../components/MetricCard"

function formatUsd(n: number) {
  const abs = Math.abs(n)
  const sign = n < 0 ? "-" : ""
  return `${sign}$${abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function Markets() {
  const { data: allocation, error } = useData(api.allocation)

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Market Intelligence</h1>
      {error && <div className="rounded-lg border border-down/30 bg-down/10 px-4 py-3 text-sm text-down">{error}</div>}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <MetricCard label="Deploy" value={allocation?.num_deploy || 0} />
        <MetricCard label="Avoid" value={allocation?.num_avoid || 0} />
        <MetricCard label="Capital deployed" value={formatUsd(allocation?.total_capital_deployed || 0)} />
      </div>

      <div className="card">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Deployed markets</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="pb-2">Market</th>
                <th className="pb-2 text-right">Est capital</th>
                <th className="pb-2 text-right">Daily rate</th>
                <th className="pb-2">Source</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {(allocation?.deploys || []).map((m: any) => (
                <tr key={m.condition_id}>
                  <td className="py-2">
                    {(m.question || m.condition_id).slice(0, 55)}
                    {(m.question || m.condition_id).length > 55 ? "..." : ""}
                  </td>
                  <td className="py-2 text-right">{formatUsd(m.est_capital_cost || 0)}</td>
                  <td className="py-2 text-right">{m.daily_rate || 0}</td>
                  <td className="py-2">{m.q_share_source || "—"}</td>
                </tr>
              ))}
              {!allocation?.deploys?.length && (
                <tr>
                  <td colSpan={4} className="py-4 text-center text-slate-500">No allocation loaded</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
