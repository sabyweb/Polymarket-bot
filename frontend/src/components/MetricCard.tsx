import type { ReactNode } from "react"

interface Props {
  label: string
  value: ReactNode
  delta?: string
  deltaPositive?: boolean | null
  sub?: ReactNode
}

export default function MetricCard({ label, value, delta, deltaPositive, sub }: Props) {
  const deltaColor =
    deltaPositive === null || deltaPositive === undefined
      ? "text-slate-400"
      : deltaPositive
      ? "text-up"
      : "text-down"
  return (
    <div className="card">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value mt-1">{value}</div>
      {delta && <div className={`mt-1 text-sm font-medium ${deltaColor}`}>{delta}</div>}
      {sub && <div className="mt-2 text-xs text-slate-500">{sub}</div>}
    </div>
  )
}
