import { api } from "../api"
import { useData } from "../hooks/useData"

export default function Config() {
  const { data: config, error } = useData(api.config)

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Config</h1>
      {error && <div className="rounded-lg border border-down/30 bg-down/10 px-4 py-3 text-sm text-down">{error}</div>}

      <div className="card">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="pb-2">Parameter</th>
                <th className="pb-2">Effective value</th>
                <th className="pb-2">Override</th>
                <th className="pb-2">Default</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {(config || []).map((c) => (
                <tr key={c.key} className={c.overridden ? "bg-info/5" : undefined}>
                  <td className="py-2 font-mono text-xs text-slate-300">{c.key}</td>
                  <td className="py-2 font-medium text-white">{String(c.effective_value)}</td>
                  <td className="py-2 text-slate-400">{c.overridden ? String(c.override_value) : "—"}</td>
                  <td className="py-2 text-slate-500">{String(c.default_value)}</td>
                </tr>
              ))}
              {!config?.length && (
                <tr>
                  <td colSpan={4} className="py-4 text-center text-slate-500">No config data</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
