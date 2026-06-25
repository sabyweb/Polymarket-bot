import { api } from "../api"
import { useData } from "../hooks/useData"
import MetricCard from "../components/MetricCard"

function formatUsd(n: number) {
  const abs = Math.abs(n)
  const sign = n < 0 ? "-" : ""
  return `${sign}$${abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function Positions() {
  const { data: positions, error } = useData(api.positions)

  const totalValue = positions?.reduce((s, p) => s + p.value, 0) || 0
  const totalPnl = positions?.reduce((s, p) => s + p.pnl, 0) || 0

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Positions</h1>
      {error && <div className="rounded-lg border border-down/30 bg-down/10 px-4 py-3 text-sm text-down">{error}</div>}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <MetricCard label="Positions" value={positions?.length || 0} />
        <MetricCard label="Total value" value={formatUsd(totalValue)} />
        <MetricCard label="Unrealized P&L" value={formatUsd(totalPnl)} deltaPositive={totalPnl >= 0} />
      </div>

      <div className="card">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="pb-2">Market</th>
                <th className="pb-2">Side</th>
                <th className="pb-2 text-right">Shares</th>
                <th className="pb-2 text-right">Avg</th>
                <th className="pb-2 text-right">Now</th>
                <th className="pb-2 text-right">Value</th>
                <th className="pb-2 text-right">P&L</th>
                <th className="pb-2 text-right">P&L %</th>
                <th className="pb-2">Expires</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {(positions || []).map((p, i) => (
                <tr key={i}>
                  <td className="py-2">{p.market}</td>
                  <td className="py-2">{p.side}</td>
                  <td className="py-2 text-right">{p.shares.toFixed(2)}</td>
                  <td className="py-2 text-right">{p.avg.toFixed(4)}</td>
                  <td className="py-2 text-right">{p.now.toFixed(4)}</td>
                  <td className="py-2 text-right">{formatUsd(p.value)}</td>
                  <td className={`py-2 text-right font-medium ${p.pnl >= 0 ? "text-up" : "text-down"}`}>
                    {formatUsd(p.pnl)}
                  </td>
                  <td className={`py-2 text-right ${p.pnl_pct >= 0 ? "text-up" : "text-down"}`}>
                    {p.pnl_pct.toFixed(1)}%
                  </td>
                  <td className="py-2 text-slate-400">{p.expires || "—"}</td>
                </tr>
              ))}
              {!positions?.length && (
                <tr>
                  <td colSpan={9} className="py-4 text-center text-slate-500">
                    No exchange positions (FUNDER may be unset)
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
