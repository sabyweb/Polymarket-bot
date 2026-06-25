interface Props {
  state?: string | null
}

export default function StatusBadge({ state }: Props) {
  const s = (state || "UNKNOWN").toUpperCase()
  let cls = "bg-slate-700 text-slate-200"
  if (["OK", "HEALTHY", "NORMAL", "RUNNING"].includes(s)) {
    cls = "bg-up/15 text-up border border-up/30"
  } else if (["DEGRADED", "WARN", "WARNING", "PAUSED", "DATA_UNAVAILABLE"].includes(s)) {
    cls = "bg-warn/15 text-warn border border-warn/30"
  } else {
    cls = "bg-down/15 text-down border border-down/30"
  }
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${cls}`}>
      {s}
    </span>
  )
}
