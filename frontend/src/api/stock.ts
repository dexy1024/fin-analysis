export interface Macd {
  dif: number
  dea: number
  macd: number
}

export interface Boll {
  upper: number
  middle: number
  lower: number
}

export interface Kdj {
  k: number
  d: number
  j: number
}

export interface StockIndicatorsResponse {
  code: string
  date: string
  close: number
  volume: number
  macd: Macd
  boll: Boll
  kdj: Kdj
}

export interface StockHistoryPoint {
  date: string
  close: number
  volume: number
  macd: Macd
  boll: Boll
  kdj: Kdj
}

export interface StockHistoryIndicatorsResponse {
  code: string
  data: StockHistoryPoint[]
}

export interface IndexKlinePoint {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  /** 60 分钟 K 线：后端附 MACD，用于背驰力度（绿柱面积）比较 */
  macd?: Macd
  /** 主图 BOLL(20,2)，样本不足处为 null */
  boll?: {
    upper: number | null
    middle: number | null
    lower: number | null
  }
}

/** 与后端 pens 项一致 */
export interface IndexPen {
  direction: 'up' | 'down'
  start_date: string
  start_price: number
  end_date: string
  end_price: number
}

export interface IndexKlineResponse {
  symbol: string
  start_date: string
  end_date: string
  period: 'daily' | '60' | '15'
  /** 指数/ETF 为 none；普通 A 股与港股日 K/60m 为前复权 qfq */
  adjust: 'none' | 'qfq'
  data: IndexKlinePoint[]
  fractals: Array<{
    type: 'top' | 'bottom'
    date: string
    price: number
    bar_index: number
  }>
  pens: IndexPen[]
  /** 日线：合并连续同向笔后的有效笔，与线段计算一致 */
  pens_effective?: IndexPen[]
  segments: Array<{
    direction?: 'up' | 'down'
    /** 沿线段内笔端点转折的折线，避免只连首尾 */
    points?: [string, number][]
    effective_pen_start_idx?: number
    effective_pen_end_idx?: number
    start_date: string
    start_price: number
    end_date: string
    end_price: number
    pen_count: number
  }>
  /** 日线：连续三段线段形成的中枢区间 [ZD,ZG]，半透明框绘制用 */
  centrals?: Array<{
    zd: number
    zg: number
    start_date: string
    end_date: string
    form_end_date: string
    segment_indices: number[]
    extend_reason: string
    /** 离开笔 MACD 柱面积小于进入笔时，后端可能标为潜在背驰 */
    potential_divergence?: boolean
    macd_area_enter?: number
    macd_area_leave?: number | null
  }>
}

// 后端服务运行在 8000 端口
const API_BASE_URL = 'http://127.0.0.1:8000'

async function fetchWithRetry(input: string, init?: RequestInit, retries = 2): Promise<Response> {
  let lastErr: unknown
  for (let i = 0; i <= retries; i++) {
    try {
      return await fetch(input, init)
    } catch (err) {
      lastErr = err
      if (i < retries) {
        await new Promise((resolve) => setTimeout(resolve, 350 * (i + 1)))
      }
    }
  }
  throw lastErr
}

export async function fetchStockIndicators(code: string): Promise<StockIndicatorsResponse> {
  const trimmed = code.trim()
  if (!trimmed) {
    throw new Error('请输入股票代码')
  }

  const params = new URLSearchParams({ code: trimmed })
  const resp = await fetchWithRetry(`${API_BASE_URL}/api/stock/indicators?${params.toString()}`)

  if (!resp.ok) {
    let msg = '请求失败'
    try {
      const data = (await resp.json()) as { detail?: string }
      if (data.detail) {
        msg = data.detail
      }
    } catch {
      // ignore
    }
    throw new Error(msg)
  }

  return (await resp.json()) as StockIndicatorsResponse
}

export async function fetchStockHistoryIndicators(
  code: string,
  startDate = '2026-01-01',
): Promise<StockHistoryIndicatorsResponse> {
  const trimmed = code.trim()
  if (!trimmed) {
    throw new Error('请输入股票代码')
  }

  const params = new URLSearchParams({ code: trimmed, start_date: startDate })
  const resp = await fetchWithRetry(`${API_BASE_URL}/api/stock/history-indicators?${params.toString()}`)

  if (!resp.ok) {
    let msg = '请求失败'
    try {
      const data = (await resp.json()) as { detail?: string }
      if (data.detail) {
        msg = data.detail
      }
    } catch {
      // ignore
    }
    throw new Error(msg)
  }

  return (await resp.json()) as StockHistoryIndicatorsResponse
}

export async function fetchIndexKline(
  symbol = 'sh000001',
  period: 'daily' | '60' | '15' = 'daily',
  startDate = '2024-12-01',
  endDate?: string,
  refresh = false,
): Promise<IndexKlineResponse> {
  const params = new URLSearchParams({ symbol, period, start_date: startDate })
  if (endDate) {
    params.set('end_date', endDate)
  }
  if (refresh) {
    params.set('refresh', 'true')
  }
  const resp = await fetchWithRetry(`${API_BASE_URL}/api/index/kline?${params.toString()}`, { cache: 'no-store' })

  if (!resp.ok) {
    let msg = '请求失败'
    try {
      const data = (await resp.json()) as { detail?: string }
      if (data.detail) {
        msg = data.detail
      }
    } catch {
      // ignore
    }
    throw new Error(msg)
  }

  return (await resp.json()) as IndexKlineResponse
}

export interface DefenseRadarSummaryItem {
  code: string
  name: string
  alert: string
  /** 一级/终极/红色 三种警报之一 */
  has_alert: boolean
  /** 与雷达 md 一致：60m 有效笔最后一笔方向 */
  pen_60m?: string
  /** 条件1：现价在一级或极限防线 ±1% 带内 */
  radar_zone_ok?: boolean
  /** 条件2：60m 有效笔末笔向下 */
  pen_60m_down?: boolean
  /** 条件3：MACD 绿柱面积较上一跌段缩小；null 表示未启用该过滤 */
  macd_momentum_ok?: boolean | null
  /** 条件4：合并后末三 K 严格底分型且 K3 收 > K2 低 */
  blue_triangle_strict?: boolean
  /** 四条件同时满足（条件3 未启用时视为通过） */
  full_trigger?: boolean
  /** 条件5：60m 现价在 C 中枢内（ZD～ZG） */
  in_c_central?: boolean
  /** 条件6：60m 底背驰点落在当前向上笔内 */
  has_bottom_div_in_switch?: boolean
  /** 条件7：60m BOLL 站回中轨 */
  boll_buy?: boolean
}

export interface DefenseRadarSummaryResponse {
  /** 与 last_summary.json / 雷达任务生成时间一致（可选） */
  generated_at?: string
  symbols: DefenseRadarSummaryItem[]
}

/** 双防线雷达摘要（只读本地），用于顶栏 tab 显隐 */
export async function fetchDefenseRadarSummary(
  refresh = false,
): Promise<DefenseRadarSummaryResponse> {
  const params = new URLSearchParams()
  if (refresh) {
    params.set('refresh', 'true')
  }
  const qs = params.toString()
  const url =
    qs.length > 0
      ? `${API_BASE_URL}/api/diagnosis/defense-radar/summary?${qs}`
      : `${API_BASE_URL}/api/diagnosis/defense-radar/summary`
  const resp = await fetchWithRetry(url, { cache: 'no-store' })
  if (!resp.ok) {
    let msg = '雷达摘要请求失败'
    try {
      const data = (await resp.json()) as { detail?: string }
      if (data.detail) {
        msg = data.detail
      }
    } catch {
      // ignore
    }
    throw new Error(msg)
  }
  return (await resp.json()) as DefenseRadarSummaryResponse
}

/** 双防线雷达：写 logs/defense_radar/defense_radar_*.md；refresh 应默认 false（读本地，在 60m 定时同步之后调用） */
export async function runDefenseRadarDiagnosis(refresh = false): Promise<{ ok: boolean; path: string }> {
  const params = new URLSearchParams()
  if (refresh) {
    params.set('refresh', 'true')
  }
  const qs = params.toString()
  const url =
    qs.length > 0
      ? `${API_BASE_URL}/api/diagnosis/defense-radar?${qs}`
      : `${API_BASE_URL}/api/diagnosis/defense-radar`
  const resp = await fetchWithRetry(url, { method: 'POST' })
  if (!resp.ok) {
    let msg = '雷达请求失败'
    try {
      const data = (await resp.json()) as { detail?: string }
      if (data.detail) {
        msg = data.detail
      }
    } catch {
      // ignore
    }
    throw new Error(msg)
  }
  return (await resp.json()) as { ok: boolean; path: string }
}

// ==================== 持仓管理 ====================

export interface Position {
  code: string
  name: string
  signal_type: string
  buy_date: string
  buy_price: number
  amount: number
  tactical_stop: number
  strategic_stop: number
}

export interface PositionsResponse {
  count: number
  positions: Position[]
}

/** 获取当前持仓列表 */
export async function fetchPositions(): Promise<PositionsResponse> {
  const resp = await fetchWithRetry(`${API_BASE_URL}/api/positions`, { cache: 'no-store' })
  if (!resp.ok) {
    let msg = '持仓请求失败'
    try {
      const data = (await resp.json()) as { detail?: string }
      if (data.detail) {
        msg = data.detail
      }
    } catch {
      // ignore
    }
    throw new Error(msg)
  }
  return (await resp.json()) as PositionsResponse
}

/** 用户持仓/自选列表 */
export interface WatchlistItem {
  code: string
  name: string
  cost?: number
  shares?: number
  note?: string
}

export interface WatchlistResponse {
  holdings: WatchlistItem[]
}

/** 读取用户持仓/自选列表 */
export async function fetchWatchlist(): Promise<WatchlistResponse> {
  const resp = await fetchWithRetry(`${API_BASE_URL}/api/watchlist`, { cache: 'no-store' })
  if (!resp.ok) {
    return { holdings: [] }
  }
  return (await resp.json()) as WatchlistResponse
}

/** 用户观察/自选列表（与持仓区分，仅用于前端显示） */
export interface ObservationResponse {
  observations: WatchlistItem[]
}

/** 读取用户观察/自选列表 */
export async function fetchObservation(): Promise<ObservationResponse> {
  const resp = await fetchWithRetry(`${API_BASE_URL}/api/observation`, { cache: 'no-store' })
  if (!resp.ok) {
    return { observations: [] }
  }
  return (await resp.json()) as ObservationResponse
}

/** 破位状态详情 */
export interface BrokenSymbolDetail {
  code: string
  name: string
  is_broken: boolean
  a_zd: number | null
  c_zd: number | null
  last_price: number | null
}

export interface BrokenSymbolsResponse {
  generated_at: string | null
  broken_codes: string[]
  details: BrokenSymbolDetail[]
}

/** 获取 watchlist + observation 的破位状态汇总（由定时调度预计算） */
export async function fetchBrokenSymbols(): Promise<BrokenSymbolsResponse> {
  const resp = await fetchWithRetry(`${API_BASE_URL}/api/broken-symbols`, { cache: 'no-store' })
  if (!resp.ok) {
    let msg = '破位状态请求失败'
    try {
      const data = (await resp.json()) as { detail?: string }
      if (data.detail) {
        msg = data.detail
      }
    } catch {
      // ignore
    }
    throw new Error(msg)
  }
  return (await resp.json()) as BrokenSymbolsResponse
}

/** 买卖信号状态详情 */
export interface BuySellSignalDetail {
  code: string
  name: string
  first_buy: boolean
  second_buy: boolean
  third_buy: boolean
  first_sell: boolean
  second_sell: boolean
  third_sell: boolean
}

export interface BuySellSignalsResponse {
  generated_at: string | null
  buy_codes: string[]
  sell_codes: string[]
  details: BuySellSignalDetail[]
}

/** 获取 watchlist + observation 的买卖信号汇总（由定时调度预计算） */
export async function fetchBuySellSignals(): Promise<BuySellSignalsResponse> {
  const resp = await fetchWithRetry(`${API_BASE_URL}/api/buy-sell-signals`, { cache: 'no-store' })
  if (!resp.ok) {
    let msg = '买卖信号请求失败'
    try {
      const data = (await resp.json()) as { detail?: string }
      if (data.detail) {
        msg = data.detail
      }
    } catch {
      // ignore
    }
    throw new Error(msg)
  }
  return (await resp.json()) as BuySellSignalsResponse
}

/** 标的配置项 */
export interface SymbolConfigItem {
  code: string
  name: string
}

/** 标的配置响应 */
export interface SymbolsConfigResponse {
  core: SymbolConfigItem[]
  custom: SymbolConfigItem[]
  total_count: number
}

/** 获取系统标的配置（核心列表 + 用户自定义列表） */
export async function fetchSymbolsConfig(): Promise<SymbolsConfigResponse> {
  const resp = await fetchWithRetry(`${API_BASE_URL}/api/config/symbols`, { cache: 'no-store' })
  if (!resp.ok) {
    let msg = '标的配置请求失败'
    try {
      const data = (await resp.json()) as { detail?: string }
      if (data.detail) {
        msg = data.detail
      }
    } catch {
      // ignore
    }
    throw new Error(msg)
  }
  return (await resp.json()) as SymbolsConfigResponse
}

/** SSE 端点：实时推送雷达更新与止损告警 */
export function createSseConnection(
  onMessage: (data: Record<string, unknown>) => void,
  onError?: (err: Event) => void,
): EventSource {
  const es = new EventSource(`${API_BASE_URL}/api/sse/radar-updates`)
  es.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      onMessage(data)
    } catch {
      // ignore parse errors
    }
  }
  if (onError) {
    es.onerror = onError
  }
  return es
}

