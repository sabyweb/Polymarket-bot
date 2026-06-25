export interface Health {
  status: string
  safety_state: string | null
  safety_reason: string | null
  safety_since: string | null
  last_fill: string | null
  last_order: string | null
  last_cycle: string | null
  last_agent: string | null
  active_orders: number
  active_dumps: number
  db_size_mb: number
  db_updated: string | null
  heartbeats: Record<string, string | null>
}

export interface Portfolio {
  cash: number
  inventory_value: number
  unrealized: number
  total: number
  num_positions: number
  drawdown_pct: number | null
}

export interface PnlSummary {
  realized_pnl: number
  stop_loss_total: number
  net: number
  num_fills: number
  num_unwinds: number
  num_stops: number
}

export interface DailyPnl {
  day: string
  gains: number
  losses: number
  net_pnl: number
  unwind_count: number
}

export interface FillRow {
  ts: number
  time: string
  condition_id: string
  question: string
  side: string
  shares: number
  price: number
  usd_value: number
  slippage: number
  fill_type: string
  cohort: number
}

export interface UnwindRow {
  ts: number
  time: string
  condition_id: string
  question: string
  side: string
  shares: number
  sell_price: number
  usd_value: number
  pnl: number
  hold_hours: number
  unwind_type: string
}

export interface ActiveOrder {
  condition_id: string
  side: string
  order_type: string
  price: number
  shares: number
  notional: number
  placed_at: string
}

export interface PositionRow {
  market: string
  side: string
  shares: number
  avg: number
  now: number
  value: number
  pnl: number
  pnl_pct: number
  expires: string | null
}

export interface CohortLatest {
  cohort: number
  reward_earned: number
  unwind_pnl: number
  net_pnl: number
  fill_count: number
  deployed_markets: number
  target_capital: number
  return_pct: number
}

export interface CohortSnapshot extends CohortLatest {
  ts: number
  time: string
}

export interface Allocation {
  num_deploy: number
  num_avoid: number
  total_capital_deployed: number
  generated_at: string
  deploys: Record<string, unknown>[]
}

export interface ConfigEntry {
  key: string
  default_value: unknown
  override_value: unknown
  effective_value: unknown
  overridden: boolean
}

export interface LogLine {
  service: string
  ts: string
  level: string
  message: string
}
