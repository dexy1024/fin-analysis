import { useCallback, useEffect, useMemo, useState } from 'react'
import './App.css'
import { classifyDefenseAlert, type DefenseAlertKind } from './DefenseAlertBrief'
import { DailyChanChart } from './DailyChanChart'
import { HourlyChanChart } from './HourlyChanChart'
import { fetchDefenseRadarSummary, fetchIndexKline, type IndexKlineResponse } from './api/stock'

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
]

/**
 * 顶栏始终展示（不按双防线摘要隐藏）：
 * 四只核心 ETF + 长江电力（上证指数按钮本就独立常驻）
 */
const ALWAYS_VISIBLE_TAB_KEYS: ReadonlySet<ChartTabKey> = new Set([
  'etf300',
  'etf588000',
  'etf159915',
  'etf513130',
  's600900', // 长江电力
])

/** 顶栏候选：除港股小米外全部；非 ALWAYS_VISIBLE 品种须 has_alert 且摘要 pen_60m 为「向下」（「向上」不显示） */
const CHART_TABS_FOR_NAV = CHART_TABS.filter((t) => t.key !== 'hk01810')

type DailyTab = 'index' | ChartTabKey

function emptyChartKlineMap(): Record<ChartTabKey, IndexKlineResponse | null> {
  const o = {} as Record<ChartTabKey, IndexKlineResponse | null>
  for (const t of CHART_TABS) {
    o[t.key] = null
  }
  return o
}

function emptyChartErrMap(): Record<ChartTabKey, string | null> {
  const o = {} as Record<ChartTabKey, string | null>
  for (const t of CHART_TABS) {
    o[t.key] = null
  }
  return o
}

function App() {
  const [dailyTab, setDailyTab] = useState<DailyTab>('index')
  const [indexKline, setIndexKline] = useState<IndexKlineResponse | null>(null)
  const [indexKline60, setIndexKline60] = useState<IndexKlineResponse | null>(null)
  const [chartDaily, setChartDaily] = useState(emptyChartKlineMap)
  const [chart60, setChart60] = useState(emptyChartKlineMap)
  const [indexDailyError, setIndexDailyError] = useState<string | null>(null)
  const [index60Error, setIndex60Error] = useState<string | null>(null)
  const [chartDailyErr, setChartDailyErr] = useState(emptyChartErrMap)
  const [chart60Err, setChart60Err] = useState(emptyChartErrMap)
  /** code -> 是否一级/终极/红色警报（null 表示摘要未加载） */
  const [defenseCodeToAlert, setDefenseCodeToAlert] = useState<Map<string, boolean> | null>(null)
  /** code -> 雷达摘要中的 60 分钟笔向（向上/向下/空）；与 defenseCodeToAlert 同次拉取 */
  const [defensePen60mByCode, setDefensePen60mByCode] = useState<Map<string, string> | null>(null)
  /** 仅梅花2test（889999）mock：摘要中 full_trigger 为真时 Tab 橙色（其它标的不再用橙色 Tab） */
  const [meihuaMockFullTriggerTab, setMeihuaMockFullTriggerTab] = useState(false)
  /** 与 last_summary.json / 雷达 md 同步的预警原文（刷新页面后从 GET summary 拉取） */
  const [defenseAlertTextByCode, setDefenseAlertTextByCode] = useState<Map<string, string>>(
    () => new Map(),
  )
  /** 摘要生成时间 ISO，便于与磁盘 json 对照 */
  const [defenseSummaryGeneratedAt, setDefenseSummaryGeneratedAt] = useState<string | null>(null)
  /** 盘中新增显示过的非常驻标的：本会话内保持显示，不自动隐藏 */
  const [stickyVisibleTabKeys, setStickyVisibleTabKeys] = useState<Set<ChartTabKey>>(new Set())

  const loadDefenseSummary = useCallback(async () => {
    try {
      const data = await fetchDefenseRadarSummary()
      const m = new Map<string, boolean>()
      const pens = new Map<string, string>()
      const texts = new Map<string, string>()
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
      }
      setDefenseCodeToAlert(m)
      setDefensePen60mByCode(pens)
      setDefenseAlertTextByCode(texts)
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
      setMeihuaMockFullTriggerTab(false)
      setDefenseSummaryGeneratedAt(null)
    }
  }, [])

  const baseVisibleChartTabs = useMemo(() => {
    const tabOrder = new Map(CHART_TABS_FOR_NAV.map((t, i) => [t.key, i] as const))
    let list: typeof CHART_TABS_FOR_NAV
    if (defenseCodeToAlert === null || defensePen60mByCode === null) {
      list = CHART_TABS_FOR_NAV.filter((t) => ALWAYS_VISIBLE_TAB_KEYS.has(t.key))
    } else {
      list = CHART_TABS_FOR_NAV.filter((tab) => {
        if (ALWAYS_VISIBLE_TAB_KEYS.has(tab.key)) return true
        const hasAlert = defenseCodeToAlert.get(String(tab.code)) === true
        if (!hasAlert) return false
        const pen = defensePen60mByCode.get(String(tab.code)) ?? ''
        if (pen === '向上') return false
        return pen === '向下'
      })
    }
    // 始终展示的 Tab 排在最前（避免换行后误以为「消失」）
    return [...list].sort((a, b) => {
      const pa = ALWAYS_VISIBLE_TAB_KEYS.has(a.key) ? 0 : 1
      const pb = ALWAYS_VISIBLE_TAB_KEYS.has(b.key) ? 0 : 1
      if (pa !== pb) return pa - pb
      return (tabOrder.get(a.key) ?? 0) - (tabOrder.get(b.key) ?? 0)
    })
  }, [defenseCodeToAlert, defensePen60mByCode])

  const visibleChartTabs = useMemo(() => {
    const byKey = new Map(CHART_TABS_FOR_NAV.map((t) => [t.key, t] as const))
    const tabOrder = new Map(CHART_TABS_FOR_NAV.map((t, i) => [t.key, i] as const))
    const merged = new Map(baseVisibleChartTabs.map((t) => [t.key, t] as const))
    for (const key of stickyVisibleTabKeys) {
      const tab = byKey.get(key)
      if (tab) merged.set(key, tab)
    }
    const list = [...merged.values()]
    return list.sort((a, b) => {
      const pa = ALWAYS_VISIBLE_TAB_KEYS.has(a.key) ? 0 : 1
      const pb = ALWAYS_VISIBLE_TAB_KEYS.has(b.key) ? 0 : 1
      if (pa !== pb) return pa - pb
      return (tabOrder.get(a.key) ?? 0) - (tabOrder.get(b.key) ?? 0)
    })
  }, [baseVisibleChartTabs, stickyVisibleTabKeys])

  useEffect(() => {
    // 新触发显示条件的非常驻标的，加入“保持显示”集合
    setStickyVisibleTabKeys((prev) => {
      const next = new Set(prev)
      for (const t of baseVisibleChartTabs) {
        if (!ALWAYS_VISIBLE_TAB_KEYS.has(t.key)) {
          next.add(t.key)
        }
      }
      if (next.size === prev.size) return prev
      return next
    })
  }, [baseVisibleChartTabs])

  /**
   * 60m：默认 refresh=false，只读后端本地 CSV/缓存；与 kline_scheduler 槽位同步后的数据一致。
   * 若需盘中强制对齐网络，可改为 true 或单独做「强制刷新」按钮。
   */
  const fetch60Local = useCallback(async (symbol: string, startDate: string) => {
    return await fetchIndexKline(symbol, '60', startDate, undefined, false)
  }, [])

  /** 首屏仅拉上证日线；其它标的改为切 tab 按需加载，避免首次并发过多请求 */
  const loadIndexDailyKline = useCallback(async () => {
    try {
      const daily = await fetchIndexKline('sh000001', 'daily', '2024-12-01')
      setIndexKline(daily)
      setIndexDailyError(null)
    } catch (err) {
      setIndexDailyError(err instanceof Error ? err.message : '未知错误')
    }
  }, [])

  /** 切 tab 时按需拉对应日线，避免首次全量并发导致卡顿 */
  const fetchDailyForTab = useCallback(async (tabKey: ChartTabKey) => {
    const tab = CHART_TABS.find((t) => t.key === tabKey)
    if (!tab || tab.key === 'hk01810') return
    try {
      const daily = await fetchIndexKline(tab.code, 'daily', '2024-12-01')
      setChartDaily((p) => ({ ...p, [tab.key]: daily }))
      setChartDailyErr((p) => ({ ...p, [tab.key]: null }))
    } catch (err) {
      setChartDailyErr((p) => ({
        ...p,
        [tab.key]: err instanceof Error ? err.message : '未知错误',
      }))
    }
  }, [])

  /** 拉取单个 tab 的 60 分钟 K（按需，避免并发请求过多触发网络错误） */
  const fetch60ForTab = useCallback(async (tabKey: ChartTabKey) => {
    const h60Start = startDateDaysAgo(90)
    const tab = CHART_TABS.find((t) => t.key === tabKey)
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
  }, [fetch60Local])

  /** 仅拉上证 60m（本地）；首屏用，不依赖 dailyTab，避免切 Tab 时整页重复请求上证 */
  const refreshIndex60Only = useCallback(async () => {
    const h60Start = startDateDaysAgo(90)
    try {
      const h60 = await fetch60Local('sh000001', h60Start)
      setIndexKline60(h60)
      setIndex60Error(null)
    } catch (err) {
      setIndex60Error(err instanceof Error ? err.message : '60分钟数据拉取失败')
    }
  }, [fetch60Local])

  /** 上证 60m + 当前激活 tab 的 60m（切回页面等场景） */
  const refresh60MinuteKlines = useCallback(async () => {
    await refreshIndex60Only()
    if (dailyTab !== 'index') {
      await fetch60ForTab(dailyTab)
    }
  }, [dailyTab, fetch60ForTab, refreshIndex60Only])

  /** 与 refresh60MinuteKlines 相同；若 effect/别处误写此名，避免 ReferenceError */
  const fetch60SyncThenDisplay = refresh60MinuteKlines

  /** 摘要单独拉取，避免与 K 线并行失败时整段受影响；首屏尽快拿到 has_alert */
  useEffect(() => {
    void loadDefenseSummary()
  }, [loadDefenseSummary])

  useEffect(() => {
    void (async () => {
      await Promise.all([loadIndexDailyKline(), refreshIndex60Only()])
    })()
  }, [loadIndexDailyKline, refreshIndex60Only])

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        void fetch60SyncThenDisplay()
        void loadDefenseSummary()
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [fetch60SyncThenDisplay, loadDefenseSummary])

  useEffect(() => {
    if (defenseCodeToAlert === null) return
    const keys = new Set(visibleChartTabs.map((t) => t.key))
    if (dailyTab !== 'index' && !keys.has(dailyTab)) {
      setDailyTab('index')
    }
  }, [dailyTab, defenseCodeToAlert, visibleChartTabs])

  // 切换 tab 时按需补拉当前 tab 的 60m，避免首屏并发全量请求
  useEffect(() => {
    if (dailyTab === 'index') return
    if (chart60[dailyTab]) return
    void fetch60ForTab(dailyTab)
  }, [dailyTab, chart60, fetch60ForTab])

  // 切换 tab 时按需补拉当前 tab 的日线
  useEffect(() => {
    if (dailyTab === 'index') return
    if (chartDaily[dailyTab]) return
    void fetchDailyForTab(dailyTab)
  }, [dailyTab, chartDaily, fetchDailyForTab])

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

  const activeChart = CHART_TABS.find((t) => t.key === dailyTab)
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
            {visibleChartTabs.map((tab) => (
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
              </button>
            ))}
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
                  />
                </div>
              )}
              <h3 className="hourly-section-title">
                60 分钟缠论（{activeChart.code}，近 90 日 60min K 线；与日线同一套合并/笔/有效笔/线段/中枢逻辑）
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
                  />
                </div>
              )}
            </div>
          )}
        </section>
      </main>
    </div>
  )
}

export default App
