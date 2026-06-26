import type {
  Health,
  Portfolio,
  PnlSummary,
  DailyPnl,
  FillRow,
  UnwindRow,
  ActiveOrder,
  PositionRow,
  CohortLatest,
  CohortSnapshot,
  Allocation,
  ConfigEntry,
  LogLine,
  RewardSummary,
  RewardDaily,
} from "./types"

const API = ""

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export const api = {
  health: () => get<Health>("/api/health"),
  portfolio: () => get<Portfolio>("/api/portfolio"),
  pnl: () => get<PnlSummary>("/api/pnl"),
  dailyPnl: (days = 14) => get<DailyPnl[]>(`/api/pnl/daily?days=${days}`),
  dailyFills: (days = 14) => get<DailyPnl[]>(`/api/fills/daily?days=${days}`),
  fills: (limit = 50, hours = 24) => get<FillRow[]>(`/api/fills?limit=${limit}&hours=${hours}`),
  unwinds: (limit = 50) => get<UnwindRow[]>(`/api/unwinds?limit=${limit}`),
  orders: () => get<ActiveOrder[]>("/api/orders"),
  positions: () => get<PositionRow[]>("/api/positions"),
  cohortLatest: () => get<CohortLatest[]>("/api/ab-cohorts/latest"),
  cohortHistory: (days = 2) => get<CohortSnapshot[]>(`/api/ab-cohorts/history?days=${days}`),
  allocation: () => get<Allocation>("/api/allocations"),
  config: () => get<ConfigEntry[]>("/api/config"),
  logs: (service: string, lines = 100) => get<LogLine[]>(`/api/logs?service=${service}&lines=${lines}`),
  rewards24h: () => get<RewardSummary>("/api/rewards/24h"),
  rewardsDaily: (days = 7) => get<RewardDaily[]>(`/api/rewards/daily?days=${days}`),
}
