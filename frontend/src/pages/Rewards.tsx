import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts"
import { api } from "../api"
import { useData } from "../hooks/useData"
import MetricCard from "../components/MetricCard"

function fmtUsd(n: number): string {
  return `$${n.toFixed(2)}`
}

export default function Rewards() {
  const { data: rewards, error } = useData(api.rewards24h, 60000)
  const { data: daily } = useData(() => api.rewardsDaily(7), 60000)

  const hours = rewards?.hours || []
  const latest = rewards?.latest || 0
  const total = rewards?.total || 0
  const marketsTracked = daily && daily.length > 0 ? daily[0].markets : 0

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Rewards</h1>
      {error && (
        <div className="rounded-lg border border-down/30 bg-down/10 px-4 py-3 text-sm text-down">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <MetricCard label="24h rewards" value={fmtUsd(latest)} />
        <MetricCard label="All-time rewards" value={fmtUsd(total)} />
        <MetricCard label="Markets tracked" value={marketsTracked} />
      </div>

      <div className="card">
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-slate-400">
          Hourly earnings (last 24h)
        </h2>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={hours} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis
                dataKey="hour"
                stroke="#64748b"
                tick={{ fill: "#94a3b8", fontSize: 11 }}
                interval="preserveStartEnd"
                angle={-30}
                textAnchor="end"
                height={60}
              />
              <YAxis
                stroke="#64748b"
                tick={{ fill: "#94a3b8", fontSize: 11 }}
                tickFormatter={(v) => `$${v}`}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#0f172a",
                  border: "1px solid #334155",
                  borderRadius: "0.375rem",
                }}
                labelStyle={{ color: "#e2e8f0" }}
                itemStyle={{ color: "#38bdf8" }}
                formatter={(value: number) => [fmtUsd(value), "Earnings"]}
              />
              <Line
                type="monotone"
                dataKey="earnings_usd"
                stroke="#38bdf8"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="card">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">
          Daily totals
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-slate-500">
                <th className="py-2 pr-4 font-medium">Date</th>
                <th className="py-2 pr-4 font-medium">Earnings</th>
                <th className="py-2 font-medium">Markets</th>
              </tr>
            </thead>
            <tbody>
              {(daily || []).map((d) => (
                <tr key={d.date} className="border-b border-slate-900">
                  <td className="py-2 pr-4 text-slate-300">{d.date}</td>
                  <td className="py-2 pr-4 text-slate-200">{fmtUsd(d.earnings_usd)}</td>
                  <td className="py-2 text-slate-400">{d.markets}</td>
                </tr>
              ))}
              {!daily?.length && (
                <tr>
                  <td colSpan={3} className="py-4 text-slate-600">
                    No reward data
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
