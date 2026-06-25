interface Props {
  cohort: number
}

const labels = ["C0 Baseline", "C1 Trader", "C2 Volume"]
const colors = [
  "bg-slate-700 text-slate-200 border-slate-600",
  "bg-info/15 text-info border-info/30",
  "bg-purple-500/15 text-purple-400 border-purple-500/30",
]

export default function CohortBadge({ cohort }: Props) {
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium border ${colors[cohort % colors.length]}`}
    >
      {labels[cohort % labels.length]}
    </span>
  )
}
