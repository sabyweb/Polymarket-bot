import { useMemo } from "react"
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  BarChart,
  Bar,
  Legend,
} from "recharts"
import { api } from "../api"
import { useData } from "../hooks/useData"
import MetricCard from "../components/MetricCard"
import CohortBadge from "../components/CohortBadge"

function formatUsd(n: number) {
  const abs = Math.abs(n)
  const sign = n < 0 ? "-" : ""
  return `${sign}$${abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

const cohortNames = ["C0 Baseline", "C1 Trader", "C2 Volume"]

export default function ABLab() {
  const { data: latest, error: lErr } = useData(api.cohortLatest)
  const { data: history, error: hErr } = useData(() => api.cohortHistory(2))
  const { data: fills, error: fErr } = useData(() => api.fills(100, 24))

  const error = lErr || hErr || fErr

  const chartData = useMemo(() => {
    if (!history) return []
    const grouped: Record<string, { time: string; c0?: number; c1?: number; c2?: number }> = {}
    history.forEach((row) => {
      if (!grouped[row.time]) grouped[row.time] = { time: row.time }
      grouped[row.time][`c${row.cohort}` as "c0" | "c1" | "c2"] = row.net_pnl
    })
    return Object.values(grouped).reverse()
  }, [history])

  const rewardLossData = useMemo(() => {
    return (latest || []).map((c) => ({
      name: cohortNames[c.cohort],
      reward: c.reward_earned,
      loss: Math.abs(c.unwind_pnl),
    }))
  }, [latest])

  const fillStats = useMemo(() => {
    if (!fills) return []
    const stats: Record<number, { count: number; value: number; slippage: number; ageSum: number; rows: number }> = {}
    fills.forEach((f) => {
      if (!stats[f.cohort]) stats[f.cohort] = { count: 0, value: 0, slippage: 0, ageSum: 0, rows: 0 }
      // order_age_secs is not in FillRow from API; approximate via ts if needed
      stats[f.cohort].count += 1
      stats[f.cohort].value += f.usd_value
      stats[f.cohort].slippage += f.slippage
      stats[f.cohort].rows += 1
    })
    return Object.entries(stats).map(([cohort, s]) => ({
      cohort: Number(cohort),
      count: s.count,
      value: s.value,
      avgSlippage: s.rows ? s.slippage / s.rows : 0,
    }))
  }, [fills])

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">A/B Experiment Lab</h1>

      {error && (
        <div className="rounded-lg border border-down/30 bg-down/10 px-4 py-3 text-sm text-down">
          {error}
        </div>
      )}

      {/* Latest cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {(latest || []).map((c) => (
          <MetricCard
            key={c.cohort}
            label={cohortNames[c.cohort]}
            value={formatUsd(c.net_pnl)}
            delta={`${c.fill_count} fills · ${formatUsd(c.target_capital)} deployed`}
            deltaPositive={c.net_pnl >= 0}
            sub={`Reward ${formatUsd(c.reward_earned)} · Loss ${formatUsd(c.unwind_pnl)}`}
          />
        ))}
      </div>

      {/* Net PnL over time */}
      <div className="card">
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-slate-400">
          Net P&L over time
        </h2>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis dataKey="time" stroke="#64748b" tick={{ fontSize: 11 }} />
              <YAxis stroke="#64748b" tick={{ fontSize: 11 }} tickFormatter={(v) => `$${v.toFixed(1)}`} />
              <Tooltip
                contentStyle={{ backgroundColor: "#0f172a", border: "1px solid #1e293b" }}
                formatter={(v: number) => formatUsd(v)}
              />
              <Line type="monotone" dataKey="c0" name="C0 Baseline" stroke="#94a3b8" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="c1" name="C1 Trader" stroke="#3b82f6" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="c2" name="C2 Volume" stroke="#a855f7" strokeWidth={2} dot={false} />
              <Legend />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Reward vs Loss */}
      <div className="card">
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-slate-400">
          Reward vs trading loss (latest 24h)
        </h2>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rewardLossData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis dataKey="name" stroke="#64748b" tick={{ fontSize: 11 }} />
              <YAxis stroke="#64748b" tick={{ fontSize: 11 }} tickFormatter={(v) => `$${v.toFixed(1)}`} />
              <Tooltip contentStyle={{ backgroundColor: "#0f172a", border: "1px solid #1e293b" }} />
              <Legend />
              <Bar dataKey="reward" name="Reward" fill="#22c55e" radius={[4, 4, 0, 0]} />
              <Bar dataKey="loss" name="Trading loss" fill="#ef4444" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Ground-truth fill table */}
      <div className="card">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">
          Recent fills by cohort (ground truth)
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="pb-2">Time</th>
                <th className="pb-2">Market</th>
                <th className="pb-2">Cohort</th>
                <th className="pb-2 text-right">Side</th>
                <th className="pb-2 text-right">Value</th>
                <th className="pb-2 text-right">Slippage</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {(fills || []).map((f) => (
                <tr key={`${f.condition_id}-${f.ts}-${f.side}`}>
                  <td className="py-2 text-slate-400">{f.time}</td>
                  <td className="py-2">{f.question.slice(0, 50)}{f.question.length > 50 ? "..." : ""}</td>
                  <td className="py-2">
                    <CohortBadge cohort={f.cohort} />
                  </td>
                  <td className="py-2 text-right uppercase">{f.side}</td>
                  <td className="py-2 text-right">{formatUsd(f.usd_value)}</td>
                  <td className={`py-2 text-right ${f.slippage <= 0 ? "text-up" : "text-down"}`}>
                    {f.slippage.toFixed(4)}
                  </td>
                </tr>
              ))}
              {!fills?.length && (
                <tr>
                  <td colSpan={6} className="py-4 text-center text-slate-500">
                    No recent fills
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Cohort fill stats */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        {fillStats.map((s) => (
          <MetricCard
            key={s.cohort}
            label={`${cohortNames[s.cohort]} fills`}
            value={`${s.count}`}
            delta={`Total value ${formatUsd(s.value)}`}
            sub={`Avg slippage ${s.avgSlippage.toFixed(4)}`}
          />
        ))}
      </div>
    </div>
  )
}
