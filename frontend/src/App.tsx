import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './App.css'
import { classifyDefenseAlert, type DefenseAlertKind } from './DefenseAlertBrief'
import { DailyChanChart } from './DailyChanChart'
import { HourlyChanChart } from './HourlyChanChart'
import {
  fetchBrokenSymbols,
  fetchBuySellSignals,
  fetchDefenseRadarSummary,
  fetchIndexKline,
  fetchObservation,
  fetchWatchlist,
  type IndexKlineResponse,
  type WatchlistItem,
} from './api/stock'
import { useCustomSymbols } from './hooks/useCustomSymbols'
import { CustomSymbolAdder } from './components/CustomSymbolAdder'

/** 与 DailyChanChart 一致：按中枢起始日（再按结束日）排序，首段为 A、末段为 C */
function sortCentralsChronologically(
  raw: NonNullable<IndexKlineResponse['centrals']>,
): NonNullable<IndexKlineResponse['centrals']> {
  return [...raw].sort((a, b) => {
    const byStart = a.start_date.localeCompare(b.start_date)
    if (byStart !== 0) return byStart
    return a.end_date.localeCompare(b.end_date)
  })
}

function startDateDaysAgo(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return d.toISOString().slice(0, 10)
}

type ChartTabKey =
  | 'etf300'
  | 'etf159915'
  | 'etf588000'
  | 'etf588200'
  | 'etf159755'
  | 'etf513130'
  | 'etf159992'
  | 'etf515790'
  | 'etf159899'
  | 'etf513360'
  | 's601225'
  | 's002508'
  | 's000333'
  | 's000429'
  | 's000423'
  | 's000338'
  | 's000895'
  | 's600011'
  | 's601138'
  | 's600660'
  | 's300048'
  | 's002415'
  | 's601919'
  | 's600585'
  | 's600873'
  | 's889999'
  | 's601166'
  | 's600900'
  | 's600887'
  | 's603317'
  | 's601728'
  | 's601857'
  | 's601766'
  | 's600096'
  | 's000001'
  | 's000651'
  | 's002230'
  | 's002714'
  | 'hk01810'
  | 's002602'
  | 's688981'
  | 's688041'
  | 's512690'
  | 'hk00175'
  | 'hk03690'
  | 'hk03896'
  | 'hk06862'
  | 's000538'
  | 's000858'
  | 's600938'
  | 's601288'
  | 's002475'
  // 动态自定义标的 key 格式：custom_${code}
  | `custom_${string}`

const CHART_TABS: {
  key: ChartTabKey
  code: string
  tabLabel: string
  seriesName: string
  seriesName60: string
}[] = [
  {
    key: 'etf300',
    code: '510300',
    tabLabel: '沪深300ETF（510300）',
    seriesName: '沪深300ETF',
    seriesName60: '沪深300ETF·60m',
  },
  {
    key: 'etf159915',
    code: '159915',
    tabLabel: '创业板ETF（159915）',
    seriesName: '创业板ETF',
    seriesName60: '创业板ETF·60m',
  },
  {
    key: 'etf588000',
    code: '588000',
    tabLabel: '科创50ETF（588000）',
    seriesName: '科创50ETF',
    seriesName60: '科创50ETF·60m',
  },
  {
    key: 'etf588200',
    code: '588200',
    tabLabel: '科创芯片ETF（588200）',
    seriesName: '科创芯片ETF',
    seriesName60: '科创芯片ETF·60m',
  },
  {
    key: 'etf159755',
    code: '159755',
    tabLabel: '电池ETF（159755）',
    seriesName: '电池ETF',
    seriesName60: '电池ETF·60m',
  },
  {
    key: 'etf513130',
    code: '513130',
    tabLabel: '恒生科技ETF（513130）',
    seriesName: '恒生科技ETF',
    seriesName60: '恒生科技ETF·60m',
  },
  {
    key: 'etf159992',
    code: '159992',
    tabLabel: '创新药ETF（159992）',
    seriesName: '创新药ETF',
    seriesName60: '创新药ETF·60m',
  },
  {
    key: 'etf515790',
    code: '515790',
    tabLabel: '光伏ETF（515790）',
    seriesName: '光伏ETF',
    seriesName60: '光伏ETF·60m',
  },
  {
    key: 'etf159899',
    code: '159899',
    tabLabel: '软件ETF（159899）',
    seriesName: '软件ETF',
    seriesName60: '软件ETF·60m',
  },
  {
    key: 'etf513360',
    code: '513360',
    tabLabel: '教育ETF（513360）',
    seriesName: '教育ETF',
    seriesName60: '教育ETF·60m',
  },
  {
    key: 's601225',
    code: '601225',
    tabLabel: '陕西煤业（601225）',
    seriesName: '陕西煤业',
    seriesName60: '陕西煤业·60m',
  },
  {
    key: 's002508',
    code: '002508',
    tabLabel: '老板电器（002508）',
    seriesName: '老板电器',
    seriesName60: '老板电器·60m',
  },
  {
    key: 's000333',
    code: '000333',
    tabLabel: '美的集团（000333）',
    seriesName: '美的集团',
    seriesName60: '美的集团·60m',
  },
  {
    key: 's000429',
    code: '000429',
    tabLabel: '粤高速（000429）',
    seriesName: '粤高速',
    seriesName60: '粤高速·60m',
  },
  {
    key: 's000423',
    code: '000423',
    tabLabel: '东阿阿胶（000423）',
    seriesName: '东阿阿胶',
    seriesName60: '东阿阿胶·60m',
  },
  {
    key: 's000338',
    code: '000338',
    tabLabel: '潍柴动力（000338）',
    seriesName: '潍柴动力',
    seriesName60: '潍柴动力·60m',
  },
  {
    key: 's000895',
    code: '000895',
    tabLabel: '双汇发展（000895）',
    seriesName: '双汇发展',
    seriesName60: '双汇发展·60m',
  },
  {
    key: 's600011',
    code: '600011',
    tabLabel: '华能国际（600011）',
    seriesName: '华能国际',
    seriesName60: '华能国际·60m',
  },
  {
    key: 's601138',
    code: '601138',
    tabLabel: '工业富联（601138）',
    seriesName: '工业富联',
    seriesName60: '工业富联·60m',
  },
  {
    key: 's600660',
    code: '600660',
    tabLabel: '福耀玻璃（600660）',
    seriesName: '福耀玻璃',
    seriesName60: '福耀玻璃·60m',
  },
  {
    key: 's300048',
    code: '300048',
    tabLabel: '合康新能（300048）',
    seriesName: '合康新能',
    seriesName60: '合康新能·60m',
  },
  {
    key: 's002415',
    code: '002415',
    tabLabel: '海康威视（002415）',
    seriesName: '海康威视',
    seriesName60: '海康威视·60m',
  },
  {
    key: 's601919',
    code: '601919',
    tabLabel: '中远海控（601919）',
    seriesName: '中远海控',
    seriesName60: '中远海控·60m',
  },
  {
    key: 's600585',
    code: '600585',
    tabLabel: '海螺水泥（600585）',
    seriesName: '海螺水泥',
    seriesName60: '海螺水泥·60m',
  },
  {
    key: 's600873',
    code: '600873',
    tabLabel: '梅花生物（600873）',
    seriesName: '梅花生物',
    seriesName60: '梅花生物·60m',
  },
  {
    key: 's889999',
    code: '889999',
    tabLabel: '梅花2test（889999）',
    seriesName: '梅花2test',
    seriesName60: '梅花2test·60m',
  },
  {
    key: 's601166',
    code: '601166',
    tabLabel: '兴业银行（601166）',
    seriesName: '兴业银行',
    seriesName60: '兴业银行·60m',
  },
  {
    key: 's600900',
    code: '600900',
    tabLabel: '长江电力（600900）',
    seriesName: '长江电力',
    seriesName60: '长江电力·60m',
  },
  {
    key: 's600887',
    code: '600887',
    tabLabel: '伊利股份（600887）',
    seriesName: '伊利股份',
    seriesName60: '伊利股份·60m',
  },
  {
    key: 's603317',
    code: '603317',
    tabLabel: '天味食品（603317）',
    seriesName: '天味食品',
    seriesName60: '天味食品·60m',
  },
  {
    key: 's601728',
    code: '601728',
    tabLabel: '中国电信（601728）',
    seriesName: '中国电信',
    seriesName60: '中国电信·60m',
  },
  {
    key: 's601857',
    code: '601857',
    tabLabel: '中国石油（601857）',
    seriesName: '中国石油',
    seriesName60: '中国石油·60m',
  },
  {
    key: 's601766',
    code: '601766',
    tabLabel: '中国中车（601766）',
    seriesName: '中国中车',
    seriesName60: '中国中车·60m',
  },
  {
    key: 's600096',
    code: '600096',
    tabLabel: '云天化（600096）',
    seriesName: '云天化',
    seriesName60: '云天化·60m',
  },
  {
    key: 's000001',
    code: '000001',
    tabLabel: '平安银行（000001）',
    seriesName: '平安银行',
    seriesName60: '平安银行·60m',
  },
  {
    key: 's000651',
    code: '000651',
    tabLabel: '格力电器（000651）',
    seriesName: '格力电器',
    seriesName60: '格力电器·60m',
  },
  {
    key: 's002230',
    code: '002230',
    tabLabel: '科大讯飞（002230）',
    seriesName: '科大讯飞',
    seriesName60: '科大讯飞·60m',
  },
  {
    key: 's002714',
    code: '002714',
    tabLabel: '牧原股份（002714）',
    seriesName: '牧原股份',
    seriesName60: '牧原股份·60m',
  },
  {
    key: 'hk01810',
    code: 'hk01810',
    tabLabel: '小米集团（hk01810）',
    seriesName: '小米集团',
    seriesName60: '小米集团·60m',
  },
  {
    key: 's002602',
    code: '002602',
    tabLabel: '世纪华通（002602）',
    seriesName: '世纪华通',
    seriesName60: '世纪华通·60m',
  },
  {
    key: 's688981',
    code: '688981',
    tabLabel: '中芯国际（688981）',
    seriesName: '中芯国际',
    seriesName60: '中芯国际·60m',
  },
  {
    key: 's688041',
    code: '688041',
    tabLabel: '海光信息（688041）',
    seriesName: '海光信息',
    seriesName60: '海光信息·60m',
  },
  {
    key: 's512690',
    code: '512690',
    tabLabel: '酒ETF（512690）',
    seriesName: '酒ETF',
    seriesName60: '酒ETF·60m',
  },
  {
    key: 'hk00175',
    code: 'hk00175',
    tabLabel: '吉利汽车（hk00175）',
    seriesName: '吉利汽车',
    seriesName60: '吉利汽车·60m',
  },
  {
    key: 'hk03690',
    code: 'hk03690',
    tabLabel: '美团（hk03690）',
    seriesName: '美团',
    seriesName60: '美团·60m',
  },
  {
    key: 'hk03896',
    code: 'hk03896',
    tabLabel: '金山云（hk03896）',
    seriesName: '金山云',
    seriesName60: '金山云·60m',
  },
  {
    key: 'hk06862',
    code: 'hk06862',
    tabLabel: '海底捞（hk06862）',
    seriesName: '海底捞',
    seriesName60: '海底捞·60m',
  },
  {
    key: 's000538',
    code: '000538',
    tabLabel: '云南白药（000538）',
    seriesName: '云南白药',
    seriesName60: '云南白药·60m',
  },
  {
    key: 's000858',
    code: '000858',
    tabLabel: '五粮液（000858）',
    seriesName: '五粮液',
    seriesName60: '五粮液·60m',
  },
  {
    key: 's600938',
    code: '600938',
    tabLabel: '中国海油（600938）',
    seriesName: '中国海油',
    seriesName60: '中国海油·60m',
  },
  {
    key: 's601288',
    code: '601288',
    tabLabel: '农业银行（601288）',
    seriesName: '农业银行',
    seriesName60: '农业银行·60m',
  },
  {
    key: 's002475',
    code: '002475',
    tabLabel: '立讯精密（002475）',
    seriesName: '立讯精密',
    seriesName60: '立讯精密·60m',
  },
]

/**
 * code 到 CHART_TABS key 的映射，用于将 watchlist/observation 中的 code
 * 正确映射为 CHART_TABS 中已定义的 key（避免重复生成 custom_${code}）
 */
const CODE_TO_CHART_TAB_KEY = new Map<string, ChartTabKey>(
  CHART_TABS.map(t => [t.code, t.key]),
)

/** 基础常驻集合（当前为空，所有标的均按雷达条件触发显示） */
const BASE_ALWAYS_VISIBLE_TAB_KEYS: ReadonlySet<ChartTabKey> = new Set([])

/** 生成包含自定义标的、持仓标的和观察标的的始终显示集合 */
function getAlwaysVisibleTabKeys(
  customSymbolCodes: string[],
  watchlistCodes: string[] = [],
  observationCodes: string[] = [],
): ReadonlySet<ChartTabKey> {
  const customKeys = customSymbolCodes.map(code => CODE_TO_CHART_TAB_KEY.get(code) ?? `custom_${code}` as ChartTabKey)
  const watchlistKeys = watchlistCodes.map(code => CODE_TO_CHART_TAB_KEY.get(code) ?? `custom_${code}` as ChartTabKey)
  const observationKeys = observationCodes.map(code => CODE_TO_CHART_TAB_KEY.get(code) ?? `custom_${code}` as ChartTabKey)
  return new Set([...BASE_ALWAYS_VISIBLE_TAB_KEYS, ...customKeys, ...watchlistKeys, ...observationKeys])
}

/** 根据自定义标的、持仓标的和观察标的生成完整的 CHART_TABS（包含基础列表和自定义标的） */
function getFullChartTabs(
  customSymbols: Array<{ code: string; name: string }>,
  watchlistSymbols: Array<{ code: string; name: string }> = [],
  observationSymbols: Array<{ code: string; name: string }> = [],
) {
  // 先去重 CHART_TABS 中已有的 code，避免为硬编码标的重复生成 custom_${code} Tab
  const allCodes = new Set<string>(CHART_TABS.map(t => t.code))
  const allSymbols: Array<{ code: string; name: string }> = []
  for (const sym of customSymbols) {
    if (!allCodes.has(sym.code)) {
      allCodes.add(sym.code)
      allSymbols.push(sym)
    }
  }
  for (const sym of watchlistSymbols) {
    if (!allCodes.has(sym.code)) {
      allCodes.add(sym.code)
      allSymbols.push(sym)
    }
  }
  for (const sym of observationSymbols) {
    if (!allCodes.has(sym.code)) {
      allCodes.add(sym.code)
      allSymbols.push(sym)
    }
  }
  const customTabs = allSymbols.map((sym) => {
    const key = `custom_${sym.code}` as ChartTabKey
    return {
      key,
      code: sym.code,
      tabLabel: `${sym.name}（${sym.code}）`,
      seriesName: sym.name,
      seriesName60: `${sym.name}·60m`,
    }
  })
  return [...CHART_TABS, ...customTabs]
}

type DailyTab = 'index' | ChartTabKey

function emptyChartKlineMap(
  customSymbols: Array<{ code: string }> = [],
  watchlistSymbols: Array<{ code: string }> = [],
  observationSymbols: Array<{ code: string }> = [],
): Record<ChartTabKey, IndexKlineResponse | null> {
  const o = {} as Record<ChartTabKey, IndexKlineResponse | null>
  for (const t of CHART_TABS) {
    o[t.key] = null
  }
  const allCodes = new Set<string>()
  for (const sym of customSymbols) {
    if (!allCodes.has(sym.code)) {
      allCodes.add(sym.code)
      const key = `custom_${sym.code}` as ChartTabKey
      o[key] = null
    }
  }
  for (const sym of watchlistSymbols) {
    if (!allCodes.has(sym.code)) {
      allCodes.add(sym.code)
      const key = `custom_${sym.code}` as ChartTabKey
      o[key] = null
    }
  }
  for (const sym of observationSymbols) {
    if (!allCodes.has(sym.code)) {
      allCodes.add(sym.code)
      const key = `custom_${sym.code}` as ChartTabKey
      o[key] = null
    }
  }
  return o
}

function emptyChartErrMap(
  customSymbols: Array<{ code: string }> = [],
  watchlistSymbols: Array<{ code: string }> = [],
  observationSymbols: Array<{ code: string }> = [],
): Record<ChartTabKey, string | null> {
  const o = {} as Record<ChartTabKey, string | null>
  for (const t of CHART_TABS) {
    o[t.key] = null
  }
  const allCodes = new Set<string>()
  for (const sym of customSymbols) {
    if (!allCodes.has(sym.code)) {
      allCodes.add(sym.code)
      const key = `custom_${sym.code}` as ChartTabKey
      o[key] = null
    }
  }
  for (const sym of watchlistSymbols) {
    if (!allCodes.has(sym.code)) {
      allCodes.add(sym.code)
      const key = `custom_${sym.code}` as ChartTabKey
      o[key] = null
    }
  }
  for (const sym of observationSymbols) {
    if (!allCodes.has(sym.code)) {
      allCodes.add(sym.code)
      const key = `custom_${sym.code}` as ChartTabKey
      o[key] = null
    }
  }
  return o
}

function App() {
  // 自定义标的管理
  const { customSymbols, addSymbol, removeSymbol } = useCustomSymbols()

  // 长轮询：定时检查雷达数据更新
  const pollingIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const [dailyTab, setDailyTab] = useState<DailyTab>('index')
  const [indexKline, setIndexKline] = useState<IndexKlineResponse | null>(null)
  const [indexKline60, setIndexKline60] = useState<IndexKlineResponse | null>(null)
  const [indexKline15, setIndexKline15] = useState<IndexKlineResponse | null>(null)
  const [indexDailyError, setIndexDailyError] = useState<string | null>(null)
  const [index60Error, setIndex60Error] = useState<string | null>(null)
  const [index15Error, setIndex15Error] = useState<string | null>(null)
  /** code -> 是否一级/终极/红色警报（null 表示摘要未加载） */
  const [defenseCodeToAlert, setDefenseCodeToAlert] = useState<Map<string, boolean> | null>(null)
  /** code -> 雷达摘要中的 60 分钟笔向（向上/向下/空）；与 defenseCodeToAlert 同次拉取 */
  const [defensePen60mByCode, setDefensePen60mByCode] = useState<Map<string, string> | null>(null)
  /** code -> 60分钟买点7条件（后端定时计算，前端直接使用） */
  const [defenseBuyConditionsByCode, setDefenseBuyConditionsByCode] = useState<
    Map<string, {
      radarZoneOk: boolean
      pen60mDown: boolean
      macdMomentumOk: boolean
      blueTriangleStrict: boolean
      inCCentral: boolean
      hasBottomDivInSwitch: boolean
      bollBuy: boolean
    }>
  >(() => new Map())
  /** 仅梅花2test（889999）mock：摘要中 full_trigger 为真时 Tab 橙色（其它标的不再用橙色 Tab） */
  const [meihuaMockFullTriggerTab, setMeihuaMockFullTriggerTab] = useState(false)
  /** 与 last_summary.json / 雷达 md 同步的预警原文（刷新页面后从 GET summary 拉取） */
  const [defenseAlertTextByCode, setDefenseAlertTextByCode] = useState<Map<string, string>>(
    () => new Map(),
  )
  /** 摘要生成时间 ISO，便于与磁盘 json 对照 */
  const [defenseSummaryGeneratedAt, setDefenseSummaryGeneratedAt] = useState<string | null>(null)
  /** 盘中新增显示过的非常驻标的：持久化到 localStorage，刷新页面也不消失，直到用户手动移除 */
  const STICKY_TABS_STORAGE_KEY = 'fin-analysis-sticky-tabs-v1'
  const [stickyVisibleTabKeys, setStickyVisibleTabKeys] = useState<Set<ChartTabKey>>(() => {
    try {
      const raw = localStorage.getItem(STICKY_TABS_STORAGE_KEY)
      if (raw) {
        const parsed = JSON.parse(raw) as ChartTabKey[]
        return new Set(parsed)
      }
    } catch {
      // ignore parse errors
    }
    return new Set()
  })

  /** 用户手动关闭的 Tab（通过点击「×」）：用于从 baseVisibleChartTabs 中排除条件触发的 Tab */
  const CLOSED_TABS_STORAGE_KEY = 'fin-analysis-closed-tabs-v1'
  const [closedTabKeys, setClosedTabKeys] = useState<Set<ChartTabKey>>(() => {
    try {
      const raw = localStorage.getItem(CLOSED_TABS_STORAGE_KEY)
      if (raw) {
        const parsed = JSON.parse(raw) as ChartTabKey[]
        return new Set(parsed)
      }
    } catch {
      // ignore parse errors
    }
    return new Set()
  })

  /** 用户持仓/自选列表（从 backend/data/watchlist.json 读取） */
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([])

  // 加载 watchlist
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const data = await fetchWatchlist()
        if (!cancelled) {
          setWatchlist(data.holdings)
        }
      } catch {
        // ignore
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [])

  /** 用户观察/自选列表（从 backend/data/observation.json 读取，仅显示用） */
  const [observation, setObservation] = useState<WatchlistItem[]>([])

  // 加载 observation
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const data = await fetchObservation()
        if (!cancelled) {
          setObservation(data.observations)
        }
      } catch {
        // ignore
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [])

  // 加载破位状态（由后端定时调度预计算，刷新页面后直接显示「破」字）
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const data = await fetchBrokenSymbols()
        if (!cancelled) {
          setBrokenCodeSet(new Set(data.broken_codes))
        }
      } catch {
        // ignore：首次启动或文件不存在时静默失败
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [])

  // 加载买卖信号状态（由后端定时调度预计算，刷新页面后直接显示「买」「卖」字）
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const data = await fetchBuySellSignals()
        if (!cancelled) {
          setBuyCodeSet(new Set(data.buy_codes))
          setSellCodeSet(new Set(data.sell_codes))
        }
      } catch {
        // ignore：首次启动或文件不存在时静默失败
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [])

  // 生成完整的 Tabs 列表（包含自定义标的、持仓标的和观察标的）
  const fullChartTabs = useMemo(() => getFullChartTabs(customSymbols, watchlist, observation), [customSymbols, watchlist, observation])
  const chartTabsForNav = useMemo(() => fullChartTabs, [fullChartTabs])

  const [chartDaily, setChartDaily] = useState(() => emptyChartKlineMap(customSymbols, watchlist, observation))
  const [chart60, setChart60] = useState(() => emptyChartKlineMap(customSymbols, watchlist, observation))
  const [chart15, setChart15] = useState(() => emptyChartKlineMap(customSymbols, watchlist, observation))
  const [chartDailyErr, setChartDailyErr] = useState(() => emptyChartErrMap(customSymbols, watchlist, observation))
  const [chart60Err, setChart60Err] = useState(() => emptyChartErrMap(customSymbols, watchlist, observation))
  const [chart15Err, setChart15Err] = useState(() => emptyChartErrMap(customSymbols, watchlist, observation))

  const alwaysVisibleTabKeys = useMemo(
    () => getAlwaysVisibleTabKeys(
      customSymbols.map(s => s.code),
      watchlist.map(w => w.code),
      observation.map(o => o.code),
    ),
    [customSymbols, watchlist, observation],
  )

  /** 持仓标的 code 集合，用于 Tab 上标记五角星 */
  const watchlistCodeSet = useMemo(() => new Set(watchlist.map(w => w.code)), [watchlist])

  /** 持仓标的 tab key 集合，用于排序 */
  const watchlistTabKeys = useMemo(
    () => new Set(watchlist.map(w => CODE_TO_CHART_TAB_KEY.get(w.code) ?? `custom_${w.code}` as ChartTabKey)),
    [watchlist],
  )

  /** 观察标的 tab key 集合，用于排序 */
  const observationTabKeys = useMemo(
    () => new Set(observation.map(o => CODE_TO_CHART_TAB_KEY.get(o.code) ?? `custom_${o.code}` as ChartTabKey)),
    [observation],
  )

  /** 持仓标的顺序映射（tab key -> 在 watchlist 中的索引），用于按用户配置排序 */
  const watchlistOrder = useMemo(() => {
    const m = new Map<ChartTabKey, number>()
    watchlist.forEach((w, i) => {
      const key = CODE_TO_CHART_TAB_KEY.get(w.code) ?? `custom_${w.code}` as ChartTabKey
      m.set(key, i)
    })
    return m
  }, [watchlist])

  /** 观察标的顺序映射（tab key -> 在 observation 中的索引），用于按用户配置排序 */
  const observationOrder = useMemo(() => {
    const m = new Map<ChartTabKey, number>()
    observation.forEach((o, i) => {
      const key = CODE_TO_CHART_TAB_KEY.get(o.code) ?? `custom_${o.code}` as ChartTabKey
      m.set(key, i)
    })
    return m
  }, [observation])

  /** 破位标的 code 集合，由后端定时调度预计算 */
  const [brokenCodeSet, setBrokenCodeSet] = useState<Set<string>>(new Set())

  /** 买/卖信号标的 code 集合，由后端定时调度预计算 */
  const [buyCodeSet, setBuyCodeSet] = useState<Set<string>>(new Set())
  const [sellCodeSet, setSellCodeSet] = useState<Set<string>>(new Set())

  const loadDefenseSummary = useCallback(async () => {
    try {
      const data = await fetchDefenseRadarSummary()
      const m = new Map<string, boolean>()
      const pens = new Map<string, string>()
      const texts = new Map<string, string>()
      const buyConds = new Map<string, {
        radarZoneOk: boolean
        pen60mDown: boolean
        macdMomentumOk: boolean
        blueTriangleStrict: boolean
        inCCentral: boolean
        hasBottomDivInSwitch: boolean
        bollBuy: boolean
      }>()
      let meihuaTrig = false
      for (const s of data.symbols ?? []) {
        const code = String(s.code ?? '').trim()
        if (!code) continue
        m.set(code, s.has_alert === true)
        const penRaw = typeof s.pen_60m === 'string' ? s.pen_60m.trim() : ''
        pens.set(code, penRaw)
        if (typeof s.alert === 'string' && s.alert.trim()) {
          texts.set(code, s.alert.trim())
        }
        if (code === '889999' && s.full_trigger === true) {
          meihuaTrig = true
        }
        // 存储7个买点条件（后端定时计算）
        buyConds.set(code, {
          radarZoneOk: s.radar_zone_ok === true,
          pen60mDown: s.pen_60m_down === true,
          macdMomentumOk: s.macd_momentum_ok === true,
          blueTriangleStrict: s.blue_triangle_strict === true,
          inCCentral: s.in_c_central === true,
          hasBottomDivInSwitch: s.has_bottom_div_in_switch === true,
          bollBuy: s.boll_buy === true,
        })
      }
      setDefenseCodeToAlert(m)
      setDefensePen60mByCode(pens)
      setDefenseAlertTextByCode(texts)
      setDefenseBuyConditionsByCode(buyConds)
      setMeihuaMockFullTriggerTab(meihuaTrig)
      setDefenseSummaryGeneratedAt(
        typeof data.generated_at === 'string' && data.generated_at.trim()
          ? data.generated_at.trim()
          : null,
      )
    } catch (err) {
      console.warn('双防线摘要拉取失败，仅显示常驻 Tab：', err)
      setDefenseCodeToAlert(new Map())
      setDefensePen60mByCode(new Map())
      setDefenseAlertTextByCode(new Map())
      setDefenseBuyConditionsByCode(new Map())
      setMeihuaMockFullTriggerTab(false)
      setDefenseSummaryGeneratedAt(null)
    }
  }, [])

  // 使用 ref 存储 loadDefenseSummary 避免 useEffect 重复执行
  const loadDefenseSummaryRef = useRef(loadDefenseSummary)
  loadDefenseSummaryRef.current = loadDefenseSummary

  // 长轮询：每隔30秒检查雷达数据是否有更新
  useEffect(() => {
    let isActive = true
    let lastKnownGeneratedAt = defenseSummaryGeneratedAt

    const checkForUpdates = async () => {
      if (!isActive) return
      
      try {
        // 轻量级请求：只获取摘要的 generated_at 字段
        const res = await fetch('/api/diagnosis/defense-radar/summary')
        if (!res.ok) return
        
        const data = (await res.json()) as { generated_at?: string }
        const currentGeneratedAt = data.generated_at
        
        // 如果 generated_at 变化了，说明有新数据
        if (currentGeneratedAt && currentGeneratedAt !== lastKnownGeneratedAt) {
          console.log('[Polling] 检测到雷达数据更新:', currentGeneratedAt)
          lastKnownGeneratedAt = currentGeneratedAt
          // 重新加载完整数据
          void loadDefenseSummaryRef.current()
          // 同步刷新 K 线数据，使核心伏击圈现价、图表等随定时调度更新
          const currentTab = dailyTabRef.current
          if (currentTab !== 'index') {
            void fetchDailyForTabRef.current(currentTab)
            void fetch60ForTabRef.current(currentTab)
            void fetch15ForTabRef.current(currentTab)
          }
          void loadIndexDailyKlineRef.current()
          void refreshIndex60OnlyRef.current()
          void refreshIndex15OnlyRef.current()
        }
      } catch (err) {
        console.error('[Polling] 检查更新失败:', err)
      }
    }

    // 每5分钟检查一次
    pollingIntervalRef.current = setInterval(() => {
      void checkForUpdates()
    }, 300000)

    return () => {
      isActive = false
      if (pollingIntervalRef.current) {
        clearInterval(pollingIntervalRef.current)
        pollingIntervalRef.current = null
      }
    }
  }, [defenseSummaryGeneratedAt])

  const baseVisibleChartTabs = useMemo(() => {
    const tabOrder = new Map(chartTabsForNav.map((t, i) => [t.key, i] as const))
    const list = chartTabsForNav.filter((t) => alwaysVisibleTabKeys.has(t.key))
    // 排序：持仓 > 观察/自定义 > 原始顺序；同组内按用户配置文件顺序排列
    return [...list].sort((a, b) => {
      const getPriority = (key: ChartTabKey) => {
        if (watchlistTabKeys.has(key)) return 0
        if (observationTabKeys.has(key)) return 1
        return 2
      }
      const pa = getPriority(a.key)
      const pb = getPriority(b.key)
      if (pa !== pb) return pa - pb
      // 同优先级内按 watchlist/observation 配置顺序，否则 fallback 到 CHART_TABS 原始顺序
      const oa = watchlistOrder.get(a.key) ?? observationOrder.get(a.key) ?? tabOrder.get(a.key) ?? 9999
      const ob = watchlistOrder.get(b.key) ?? observationOrder.get(b.key) ?? tabOrder.get(b.key) ?? 9999
      return oa - ob
    })
  }, [chartTabsForNav, alwaysVisibleTabKeys, watchlistTabKeys, observationTabKeys, watchlistOrder, observationOrder])

  const visibleChartTabs = useMemo(() => {
    const byKey = new Map(chartTabsForNav.map((t) => [t.key, t] as const))
    const tabOrder = new Map(chartTabsForNav.map((t, i) => [t.key, i] as const))
    const merged = new Map(baseVisibleChartTabs.map((t) => [t.key, t] as const))
    for (const key of stickyVisibleTabKeys) {
      // 如果用户手动关闭了此 Tab，不显示
      if (closedTabKeys.has(key)) continue
      const tab = byKey.get(key)
      if (tab) merged.set(key, tab)
    }
    const list = [...merged.values()]
    return list.sort((a, b) => {
      const getPriority = (key: ChartTabKey) => {
        if (watchlistTabKeys.has(key)) return 0
        if (observationTabKeys.has(key)) return 1
        return 2
      }
      const pa = getPriority(a.key)
      const pb = getPriority(b.key)
      if (pa !== pb) return pa - pb
      const oa = watchlistOrder.get(a.key) ?? observationOrder.get(a.key) ?? tabOrder.get(a.key) ?? 9999
      const ob = watchlistOrder.get(b.key) ?? observationOrder.get(b.key) ?? tabOrder.get(b.key) ?? 9999
      return oa - ob
    })
  }, [chartTabsForNav, alwaysVisibleTabKeys, watchlistTabKeys, observationTabKeys, watchlistOrder, observationOrder, baseVisibleChartTabs, stickyVisibleTabKeys, closedTabKeys])

  // 条件触发显示逻辑已关闭，保留 ref 避免后续代码报错
  const prevBaseVisibleRef = useRef<Set<ChartTabKey>>(new Set())

  useEffect(() => {
    prevBaseVisibleRef.current = new Set(baseVisibleChartTabs.map(t => t.key))
  }, [baseVisibleChartTabs])

  // 持久化 stickyVisibleTabKeys 到 localStorage
  useEffect(() => {
    try {
      const arr = Array.from(stickyVisibleTabKeys)
      localStorage.setItem(STICKY_TABS_STORAGE_KEY, JSON.stringify(arr))
    } catch {
      // ignore storage errors
    }
  }, [stickyVisibleTabKeys])

  // 持久化 closedTabKeys 到 localStorage
  useEffect(() => {
    try {
      const arr = Array.from(closedTabKeys)
      localStorage.setItem(CLOSED_TABS_STORAGE_KEY, JSON.stringify(arr))
    } catch {
      // ignore storage errors
    }
  }, [closedTabKeys])

  /**
   * 60m：默认 refresh=false，只读后端本地 CSV/缓存；与 kline_scheduler 槽位同步后的数据一致。
   * 若需盘中强制对齐网络，可改为 true 或单独做「强制刷新」按钮。
   */
  const fetch60Local = useCallback(async (symbol: string, startDate: string) => {
    return await fetchIndexKline(symbol, '60', startDate, undefined, false)
  }, [])

  /**
   * 15m：默认 refresh=false，只读后端本地 CSV/缓存；与 kline_scheduler 槽位同步后的数据一致。
   */
  const fetch15Local = useCallback(async (symbol: string, startDate: string) => {
    return await fetchIndexKline(symbol, '15', startDate, undefined, false)
  }, [])

  /** 首屏仅拉上证日线；其它标的改为切 tab 按需加载，避免首次并发过多请求 */
  const loadIndexDailyKline = useCallback(async () => {
    try {
      const dailyStart = startDateDaysAgo(380)
      const daily = await fetchIndexKline('sh000001', 'daily', dailyStart)
      setIndexKline(daily)
      setIndexDailyError(null)
    } catch (err) {
      setIndexDailyError(err instanceof Error ? err.message : '未知错误')
    }
  }, [])

  /** 切 tab 时按需拉对应日线，避免首次全量并发导致卡顿 */
  const fetchDailyForTab = useCallback(async (tabKey: ChartTabKey) => {
    const tab = fullChartTabs.find((t) => t.key === tabKey)
    if (!tab) return
    try {
      const dailyStart = startDateDaysAgo(380)
      const daily = await fetchIndexKline(tab.code, 'daily', dailyStart)
      setChartDaily((p) => ({ ...p, [tab.key]: daily }))
      setChartDailyErr((p) => ({ ...p, [tab.key]: null }))
    } catch (err) {
      setChartDailyErr((p) => ({
        ...p,
        [tab.key]: err instanceof Error ? err.message : '未知错误',
      }))
    }
  }, [fullChartTabs])

  /** 拉取单个 tab 的 60 分钟 K（按需，避免并发请求过多触发网络错误） */
  const fetch60ForTab = useCallback(async (tabKey: ChartTabKey) => {
    const h60Start = startDateDaysAgo(79)
    const tab = fullChartTabs.find((t) => t.key === tabKey)
    if (!tab) return
    try {
      const h60 = await fetch60Local(tab.code, h60Start)
      setChart60((p) => ({ ...p, [tab.key]: h60 }))
      setChart60Err((p) => ({ ...p, [tab.key]: null }))
    } catch (err) {
      setChart60Err((p) => ({
        ...p,
        [tab.key]: err instanceof Error ? err.message : '60分钟数据拉取失败',
      }))
    }
  }, [fullChartTabs, fetch60Local])

  /** 拉取单个 tab 的 15 分钟 K（按需，避免并发请求过多触发网络错误） */
  const fetch15ForTab = useCallback(async (tabKey: ChartTabKey) => {
    const h15Start = startDateDaysAgo(35)
    const tab = fullChartTabs.find((t) => t.key === tabKey)
    if (!tab) return
    try {
      const h15 = await fetch15Local(tab.code, h15Start)
      setChart15((p) => ({ ...p, [tab.key]: h15 }))
      setChart15Err((p) => ({ ...p, [tab.key]: null }))
    } catch (err) {
      setChart15Err((p) => ({
        ...p,
        [tab.key]: err instanceof Error ? err.message : '15分钟数据拉取失败',
      }))
    }
  }, [fullChartTabs, fetch15Local])

  /** 仅拉上证 60m（本地）；首屏用，不依赖 dailyTab，避免切 Tab 时整页重复请求上证 */
  const refreshIndex60Only = useCallback(async () => {
    const h60Start = startDateDaysAgo(79)
    try {
      const h60 = await fetch60Local('sh000001', h60Start)
      setIndexKline60(h60)
      setIndex60Error(null)
    } catch (err) {
      setIndex60Error(err instanceof Error ? err.message : '60分钟数据拉取失败')
    }
  }, [fetch60Local])

  /** 仅拉上证 15m（本地）；首屏用 */
  const refreshIndex15Only = useCallback(async () => {
    const h15Start = startDateDaysAgo(35)
    try {
      const h15 = await fetch15Local('sh000001', h15Start)
      setIndexKline15(h15)
      setIndex15Error(null)
    } catch (err) {
      setIndex15Error(err instanceof Error ? err.message : '15分钟数据拉取失败')
    }
  }, [fetch15Local])

  /** 上证 60m + 当前激活 tab 的 60m（切回页面等场景） */
  const refresh60MinuteKlines = useCallback(async () => {
    await refreshIndex60Only()
    if (dailyTab !== 'index') {
      await fetch60ForTab(dailyTab)
    }
  }, [dailyTab, fetch60ForTab, refreshIndex60Only])

  /** 上证 15m + 当前激活 tab 的 15m（切回页面等场景） */
  const refresh15MinuteKlines = useCallback(async () => {
    await refreshIndex15Only()
    if (dailyTab !== 'index') {
      await fetch15ForTab(dailyTab)
    }
  }, [dailyTab, fetch15ForTab, refreshIndex15Only])

  /** 与 refresh60MinuteKlines 相同；若 effect/别处误写此名，避免 ReferenceError */
  const fetch60SyncThenDisplay = refresh60MinuteKlines
  /** 与 refresh15MinuteKlines 相同 */
  const fetch15SyncThenDisplay = refresh15MinuteKlines

  // 使用 ref 存储 K 线刷新函数和当前 tab，供长轮询在检测到更新后同步刷新
  const dailyTabRef = useRef(dailyTab)
  dailyTabRef.current = dailyTab
  const fetchDailyForTabRef = useRef(fetchDailyForTab)
  fetchDailyForTabRef.current = fetchDailyForTab
  const fetch60ForTabRef = useRef(fetch60ForTab)
  fetch60ForTabRef.current = fetch60ForTab
  const fetch15ForTabRef = useRef(fetch15ForTab)
  fetch15ForTabRef.current = fetch15ForTab
  const loadIndexDailyKlineRef = useRef(loadIndexDailyKline)
  loadIndexDailyKlineRef.current = loadIndexDailyKline
  const refreshIndex60OnlyRef = useRef(refreshIndex60Only)
  refreshIndex60OnlyRef.current = refreshIndex60Only
  const refreshIndex15OnlyRef = useRef(refreshIndex15Only)
  refreshIndex15OnlyRef.current = refreshIndex15Only

  /** 摘要单独拉取，避免与 K 线并行失败时整段受影响；首屏尽快拿到 has_alert */
  useEffect(() => {
    void loadDefenseSummary()
  }, [loadDefenseSummary])

  useEffect(() => {
    void (async () => {
      await Promise.all([loadIndexDailyKline(), refreshIndex60Only(), refreshIndex15Only()])
    })()
  }, [loadIndexDailyKline, refreshIndex60Only, refreshIndex15Only])

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        void fetch60SyncThenDisplay()
        void fetch15SyncThenDisplay()
        void loadDefenseSummary()
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [fetch60SyncThenDisplay, fetch15SyncThenDisplay, loadDefenseSummary])

  useEffect(() => {
    if (defenseCodeToAlert === null) return
    const keys = new Set(visibleChartTabs.map((t) => t.key))
    if (dailyTab !== 'index' && !keys.has(dailyTab)) {
      setDailyTab('index')
    }
  }, [dailyTab, defenseCodeToAlert, visibleChartTabs])

  // 切换 tab 时补拉当前 tab 的60m（若预加载已完成则跳过，避免重复请求）
  useEffect(() => {
    if (dailyTab === 'index') return
    if (loadedKeysRef.current.has(dailyTab + '_60m')) return
    void fetch60ForTab(dailyTab)
  }, [dailyTab, fetch60ForTab])

  // 切换 tab 时补拉当前 tab 的15m（若预加载已完成则跳过，避免重复请求）
  useEffect(() => {
    if (dailyTab === 'index') return
    if (loadedKeysRef.current.has(dailyTab + '_15m')) return
    void fetch15ForTab(dailyTab)
  }, [dailyTab, fetch15ForTab])

  // 切换 tab 时按需补拉当前 tab 的日线
  useEffect(() => {
    if (dailyTab === 'index') return
    if (chartDaily[dailyTab]) return
    void fetchDailyForTab(dailyTab)
  }, [dailyTab, chartDaily, fetchDailyForTab])
  
  // 批量预加载 visibleChartTabs 的日线和60m数据（延迟2秒启动，避免阻塞首屏和交互）
  const loadedKeysRef = useRef(new Set<string>())
  useEffect(() => {
    if (watchlist.length === 0 && observation.length === 0) return

    const timer = setTimeout(() => {
      void (async () => {
        const dailyStart = startDateDaysAgo(380)
        const h60Start = startDateDaysAgo(79)
        const h15Start = startDateDaysAgo(35)
        // 只预加载当前可见的 tab，避免加载被用户关闭/隐藏的 tab
        let tabsToLoad = visibleChartTabs.filter(
          (tab) => !loadedKeysRef.current.has(tab.key + '_daily') || !loadedKeysRef.current.has(tab.key + '_60m') || !loadedKeysRef.current.has(tab.key + '_15m')
        )
        // 当前激活 tab 优先加载：排在队列最前面
        if (dailyTab !== 'index') {
          const activeIdx = tabsToLoad.findIndex((t) => t.key === dailyTab)
          if (activeIdx > 0) {
            const [activeTab] = tabsToLoad.splice(activeIdx, 1)
            tabsToLoad.unshift(activeTab)
          }
        }
        // 低并发预加载（2个），留出浏览器并发槽位给用户交互请求
        const CONCURRENCY = 2
        const queue: Promise<void>[] = []
        for (const tab of tabsToLoad) {
          const task = async () => {
            const promises: Promise<void>[] = []
            // 日线
            if (!loadedKeysRef.current.has(tab.key + '_daily')) {
              loadedKeysRef.current.add(tab.key + '_daily')
              promises.push(
                fetchIndexKline(tab.code, 'daily', dailyStart)
                  .then((daily) => {
                    setChartDaily((p) => ({ ...p, [tab.key]: daily }))
                    setChartDailyErr((p) => ({ ...p, [tab.key]: null }))
                  })
                  .catch(() => {})
              )
            }
            // 60m（与日线并行加载）
            if (!loadedKeysRef.current.has(tab.key + '_60m')) {
              loadedKeysRef.current.add(tab.key + '_60m')
              promises.push(
                fetch60Local(tab.code, h60Start)
                  .then((h60) => {
                    setChart60((p) => ({ ...p, [tab.key]: h60 }))
                    setChart60Err((p) => ({ ...p, [tab.key]: null }))
                  })
                  .catch(() => {})
              )
            }
            // 15m（与日线并行加载）
            if (!loadedKeysRef.current.has(tab.key + '_15m')) {
              loadedKeysRef.current.add(tab.key + '_15m')
              promises.push(
                fetch15Local(tab.code, h15Start)
                  .then((h15) => {
                    setChart15((p) => ({ ...p, [tab.key]: h15 }))
                    setChart15Err((p) => ({ ...p, [tab.key]: null }))
                  })
                  .catch(() => {})
              )
            }
            await Promise.allSettled(promises)
          }
          queue.push(task())
          if (queue.length >= CONCURRENCY) {
            await Promise.allSettled(queue)
            queue.length = 0
          }
        }
        if (queue.length > 0) {
          await Promise.allSettled(queue)
        }
      })()
    }, 2000)
    return () => clearTimeout(timer)
  }, [visibleChartTabs, watchlist, observation, dailyTab, fetch60Local, fetch15Local])

  const indexDailyCentrals = indexKline?.centrals?.length
    ? sortCentralsChronologically(indexKline.centrals)
    : []
  const indexDailyAZd =
    indexDailyCentrals.length > 0 ? Number(indexDailyCentrals[0].zd) : null
  const indexDailyCZd =
    indexDailyCentrals.length > 0
      ? Number(indexDailyCentrals[indexDailyCentrals.length - 1].zd)
      : null

  /** 上证指数日线双防线档位，与个股简讯对照用 */
  const indexDefenseKind = useMemo((): DefenseAlertKind | null => {
    if (!indexKline?.data?.length) return null
    const last = indexKline.data[indexKline.data.length - 1].close
    return classifyDefenseAlert(last, indexDailyCZd, indexDailyAZd)
  }, [indexKline, indexDailyCZd, indexDailyAZd])

  const activeChart = fullChartTabs.find((t) => t.key === dailyTab)
  const activeChartDaily = activeChart ? chartDaily[activeChart.key] : null
  const chartDailyCentrals =
    activeChartDaily?.centrals?.length && activeChart
      ? sortCentralsChronologically(activeChartDaily.centrals)
      : []
  const chartDailyAZd =
    chartDailyCentrals.length > 0 ? Number(chartDailyCentrals[0].zd) : null
  const chartDailyCZd =
    chartDailyCentrals.length > 0
      ? Number(chartDailyCentrals[chartDailyCentrals.length - 1].zd)
      : null
  const chartDailyMacd =
    activeChartDaily?.data?.length
      ? activeChartDaily.data[activeChartDaily.data.length - 1].macd
      : undefined
  const indexDailyMacd =
    indexKline?.data?.length
      ? indexKline.data[indexKline.data.length - 1].macd
      : undefined

  return (
    <div
      className="app"
      style={{ width: '98vw', maxWidth: 'none', margin: 0, minHeight: '100vh' }}
    >
      <main className="app-main">
        <section className="card" style={{ width: '100%', maxWidth: 'none' }}>
          <h2 className="section-title">
            日K 分析（2024-12-01 至今；个股/港股前复权，ETF 不复权，本地缓存）
            <span className="section-title-hint">
              {' '}
              · 本地缓存由后端定时更新；除上证指数与始终展示的 Tab 外，仅当双防线为一级/终极/红色警报且雷达摘要中
              60分钟笔向为「向下」时显示品种 Tab（「向上」不显示）；盘中新出现的品种本会话不自动隐藏
            </span>
          </h2>
          <div className="daily-tabs" role="tablist" aria-label="日K 品种切换">
            <button
              type="button"
              role="tab"
              aria-selected={dailyTab === 'index'}
              id="tab-index"
              className={`daily-tab ${dailyTab === 'index' ? 'daily-tab-active' : ''}`}
              onClick={() => setDailyTab('index')}
            >
              上证指数
            </button>
            {visibleChartTabs.map((tab) => {
                // 日线破位判断：使用后端定时调度预计算的 broken_symbols.json 结果
                const isBroken = brokenCodeSet.has(tab.code)
                return (
              <button
                key={tab.key}
                type="button"
                role="tab"
                aria-selected={dailyTab === tab.key}
                id={`tab-${tab.key}`}
                className={`daily-tab${tab.code === '889999' && meihuaMockFullTriggerTab ? ' daily-tab-full-trigger' : ''} ${dailyTab === tab.key ? 'daily-tab-active' : ''}`}
                onClick={() => setDailyTab(tab.key)}
              >
                {tab.tabLabel}
                {watchlistCodeSet.has(tab.code) && (
                  <span style={{ color: '#fbbf24', marginLeft: '4px', fontSize: '14px' }} title="持仓">★</span>
                )}
                {isBroken && (
                  <span style={{ color: '#ef4444', marginLeft: '3px', fontSize: '11px', fontWeight: 'bold' }} title={`日线破位: 60分钟现价 < MIN(日线A-ZD, 日线C-ZD)`}>破</span>
                )}
                {(() => {
                  const hasBuy = buyCodeSet.has(tab.code)
                  const hasSell = sellCodeSet.has(tab.code)
                  if (!hasBuy && !hasSell) return null
                  return (
                    <span
                      style={{
                        color: hasBuy ? '#22c55e' : '#ef4444',
                        marginLeft: '3px',
                        fontSize: '11px',
                        fontWeight: 'bold',
                      }}
                      title={hasBuy ? '缠论买点信号（一买/二买/三买）' : '缠论卖点信号（一卖/二卖/三卖）'}
                    >
                      {hasBuy ? '买' : '卖'}
                    </span>
                  )
                })()}
                {/* 非常驻 Tab 显示关闭按钮 */}
                {!alwaysVisibleTabKeys.has(tab.key) && (
                  <span
                    className="tab-close-btn"
                    onClick={(e) => {
                      e.stopPropagation()
                      // 从 sticky 集合中删除
                      setStickyVisibleTabKeys((prev) => {
                        const next = new Set(prev)
                        next.delete(tab.key)
                        return next
                      })
                      // 添加到关闭集合，防止条件触发再次显示
                      setClosedTabKeys((prev) => new Set([...prev, tab.key]))
                      // 如果当前选中的是要关闭的 Tab，切换到上证指数
                      if (dailyTab === tab.key) {
                        setDailyTab('index')
                      }
                    }}
                    title="隐藏此标签"
                  >
                    ×
                  </span>
                )}
              </button>
            )
          })}
          </div>

          {/* 自定义标的添加器 */}
          <div className="custom-symbol-section">
            <CustomSymbolAdder
              onAdd={(code, name) => {
                const result = addSymbol(code, name)
                if (result) {
                  // 添加成功后自动切换到新添加的标的
                  const key = `custom_${code}` as ChartTabKey
                  setDailyTab(key)
                }
                return result
              }}
              onRemove={(code) => {
                removeSymbol(code)
                const key = `custom_${code}` as ChartTabKey
                // 如果当前选中的是要删除的标的，切换到上证指数
                if (dailyTab === key) {
                  setDailyTab('index')
                }
              }}
              customSymbols={customSymbols}
            />
          </div>

          {dailyTab === 'index' && (
            <div role="tabpanel" aria-labelledby="tab-index">
              {indexDailyError && <div className="alert alert-error">{indexDailyError}</div>}
              {indexKline && (
                <div className="chart-block">
                  <DailyChanChart
                    key="daily-index"
                    data={indexKline}
                    seriesName="上证指数"
                    indexAlertKind={indexDefenseKind}
                    isIndexSelf
                    currentPrice={indexKline60?.data?.length ? indexKline60.data[indexKline60.data.length - 1].close : undefined}
                  />
                </div>
              )}
              <h3 className="hourly-section-title">
                60 分钟缠论（上证指数，近 90 日 60min K 线；与日线同一套合并/笔/有效笔/线段/中枢逻辑）
              </h3>
              {index60Error && <div className="alert alert-error">{index60Error}</div>}
              {indexKline60 && (
                <div className="chart-block chart-block-hourly">
                  <HourlyChanChart
                    key="hourly-index"
                    data={indexKline60}
                    seriesName="上证指数·60m"
                    dailyAZd={indexDailyAZd}
                    dailyCZd={indexDailyCZd}
                    dailyMacd={indexDailyMacd}
                    buyConditions={undefined}
                  />
                </div>
              )}
              <h3 className="hourly-section-title">
                15 分钟缠论（上证指数，近 35 日 15min K 线；与日线同一套合并/笔/有效笔/线段/中枢逻辑）
              </h3>
              {index15Error && <div className="alert alert-error">{index15Error}</div>}
              {indexKline15 && (
                <div className="chart-block chart-block-hourly">
                  <HourlyChanChart
                    key="hourly-15-index"
                    data={indexKline15}
                    seriesName="上证指数·15m"
                    dailyAZd={indexDailyAZd}
                    dailyCZd={indexDailyCZd}
                    dailyMacd={indexDailyMacd}
                    buyConditions={undefined}
                  />
                </div>
              )}
            </div>
          )}

          {activeChart && (
            <div role="tabpanel" aria-labelledby={`tab-${activeChart.key}`}>
              {chartDailyErr[activeChart.key] && (
                <div className="alert alert-error">{chartDailyErr[activeChart.key]}</div>
              )}
              {chartDaily[activeChart.key] && (
                <div className="chart-block">
                  <DailyChanChart
                    key={`daily-${activeChart.key}`}
                    data={chartDaily[activeChart.key]!}
                    seriesName={activeChart.seriesName}
                    indexAlertKind={indexDefenseKind}
                    radarSummaryAlert={defenseAlertTextByCode.get(String(activeChart.code)) ?? null}
                    radarSummaryGeneratedAt={defenseSummaryGeneratedAt}
                    currentPrice={chart60[activeChart.key]?.data?.length ? chart60[activeChart.key]!.data[chart60[activeChart.key]!.data.length - 1].close : undefined}
                  />
                </div>
              )}
              {(() => {
                const holding = watchlist.find((w) => w.code === activeChart.code)
                return (
                  <>
                    <h3 className="hourly-section-title">
                      60 分钟缠论（{activeChart.code}{holding ? ` ★持仓·${holding.name}` : ''}，近 90 日 60min K 线；与日线同一套合并/笔/有效笔/线段/中枢逻辑）
                    </h3>
                    {chart60Err[activeChart.key] && (
                      <div className="alert alert-error">{chart60Err[activeChart.key]}</div>
                    )}
                    {chart60[activeChart.key] && (
                      <div className="chart-block chart-block-hourly">
                        <HourlyChanChart
                          key={`hourly-${activeChart.key}`}
                          data={chart60[activeChart.key]!}
                          seriesName={activeChart.seriesName60}
                          dailyAZd={chartDailyAZd}
                          dailyCZd={chartDailyCZd}
                          dailyMacd={chartDailyMacd}
                          buyConditions={defenseBuyConditionsByCode.get(activeChart.code)}
                          holdingInfo={holding}
                        />
                      </div>
                    )}
                    <h3 className="hourly-section-title">
                      15 分钟缠论（{activeChart.code}{holding ? ` ★持仓·${holding.name}` : ''}，近 35 日 15min K 线；与日线同一套合并/笔/有效笔/线段/中枢逻辑）
                    </h3>
                    {chart15Err[activeChart.key] && (
                      <div className="alert alert-error">{chart15Err[activeChart.key]}</div>
                    )}
                    {chart15[activeChart.key] && (
                      <div className="chart-block chart-block-hourly">
                        <HourlyChanChart
                          key={`hourly-15-${activeChart.key}`}
                          data={chart15[activeChart.key]!}
                          seriesName={activeChart.seriesName60.replace('60m', '15m')}
                          dailyAZd={chartDailyAZd}
                          dailyCZd={chartDailyCZd}
                          dailyMacd={chartDailyMacd}
                          buyConditions={undefined}
                          holdingInfo={holding}
                        />
                      </div>
                    )}
                  </>
                )
              })()}
            </div>
          )}
        </section>
      </main>
    </div>
  )
}

export default App
