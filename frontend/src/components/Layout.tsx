import { Link, useLocation, Outlet } from "react-router-dom"
import {
  Activity,
  FlaskConical,
  TrendingUp,
  Wallet,
  Settings,
  BarChart3,
  FileText,
} from "lucide-react"

const nav = [
  { path: "/", label: "Command", icon: Activity },
  { path: "/ab", label: "A/B Lab", icon: FlaskConical },
  { path: "/pnl", label: "P&L", icon: TrendingUp },
  { path: "/positions", label: "Positions", icon: Wallet },
  { path: "/markets", label: "Markets", icon: BarChart3 },
  { path: "/health", label: "Health", icon: FileText },
  { path: "/config", label: "Config", icon: Settings },
]

export default function Layout() {
  const { pathname } = useLocation()
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <nav className="fixed top-0 left-0 right-0 z-50 border-b border-slate-800 bg-slate-900/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center px-4">
          <div className="mr-6 flex items-center gap-2 font-semibold tracking-tight">
            <div className="flex h-7 w-7 items-center justify-center rounded bg-info text-white">
              <Activity size={18} />
            </div>
            <span>Reward Farmer</span>
          </div>
          <div className="flex gap-1 overflow-x-auto">
            {nav.map((item) => {
              const Icon = item.icon
              const active = pathname === item.path || pathname.startsWith(`${item.path}/`)
              return (
                <Link
                  key={item.path}
                  to={item.path}
                  className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                    active
                      ? "bg-slate-800 text-white"
                      : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-200"
                  }`}
                >
                  <Icon size={16} />
                  {item.label}
                </Link>
              )
            })}
          </div>
        </div>
      </nav>
      <main className="mx-auto max-w-7xl px-4 pb-12 pt-20">
        <Outlet />
      </main>
    </div>
  )
}
