import ReactECharts from 'echarts-for-react'
import type { IndexKlinePoint, IndexKlineResponse, IndexPen } from './api/stock'
import { buildBollLineData, bollExtentPrices } from './bollSeries'
import {
  appendMacdTooltipBlock,
  divergenceArrowPointsFromDownPens,
  KLINE_DOWN_GREEN,
  KLINE_UP_RED,
} from './chartMacd'
import { computeHourlyBuySellState } from './hourlyBuySellSignals'
import {
  formatMacdYAxisLabel,
  formatPriceYAxisLabel,
  mainChartYExtent,
} from './priceAxisExtent'

function segmentPolylineFromEffective(
  ep: IndexPen[],
  startIdx: number,
  endIdx: number,
): [string, number][] {
  if (startIdx < 0 || endIdx >= ep.length || startIdx > endIdx) {
    return []
  }
  const pts: [string, number][] = [[ep[startIdx].start_date, ep[startIdx].start_price]]
  for (let k = startIdx; k <= endIdx; k++) {
    const p = ep[k]
    const last = pts[pts.length - 1]
    if (last[0] === p.end_date && Math.abs(last[1] - p.end_price) < 1e-9) continue
    pts.push([p.end_date, p.end_price])
  }
  return pts
}

const CENTRAL_LABELS = ['A中枢', 'B中枢', 'C中枢'] as const

function pctVsRef(close: number, ref: number | null | undefined): string {
  if (ref == null || !Number.isFinite(ref) || ref === 0) return '—'
  return (((close - ref) / ref) * 100).toFixed(2)
}

type CentralTipEntry = {
  label: string
  zg: number
  zd: number
  start_date: string
  end_date: string
  potential_divergence?: boolean
  macd_area_enter?: number
  macd_area_leave?: number | null
}

function dateInCentralRangeFull(axisDate: string, start: string, end: string): boolean {
  const d = axisDate.trim()
  return d >= start.trim() && d <= end.trim()
}

function buildAxisTooltipHtmlHourly(
  params: unknown,
  candleSeriesName: string,
  centralTips: CentralTipEntry[],
  klineRows?: IndexKlinePoint[],
  periodLabel: string = '60分钟',
): string {
  if (!Array.isArray(params) || params.length === 0) return ''
  const first = params[0] as { axisValue?: string; axisValueLabel?: string }
  const dateLine = String(first.axisValueLabel ?? first.axisValue ?? '').trim()

  const lines: string[] = []
  lines.push(
    `<div style="font-size:12px;font-weight:600;margin-bottom:6px;color:#e5e7eb">${dateLine}</div>`,
  )

  for (const raw of params) {
    const p = raw as {
      seriesName?: string
      seriesType?: string
      value?: unknown
    }
    if (p.seriesType === 'candlestick' && p.seriesName === candleSeriesName && Array.isArray(p.value)) {
      const rawV = p.value as number[]
      const ohlc =
        rawV.length >= 5 ? rawV.slice(-4) : rawV.length === 4 ? rawV : null
      if (!ohlc) break
      const [open, close, low, high] = ohlc
      lines.push(
        `<div style="color:#cbd5e1;font-size:12px">开 ${open.toFixed(3)}　收 ${close.toFixed(3)}</div>`,
      )
      lines.push(
        `<div style="color:#cbd5e1;font-size:12px">低 ${low.toFixed(3)}　高 ${high.toFixed(3)}</div>`,
      )
      if (rawV.length >= 5 && klineRows?.length) {
        const idx = Number(rawV[0])
        const pt = Number.isFinite(idx) ? klineRows[Math.floor(idx)] : undefined
        const b = pt?.boll
        if (
          b?.upper != null &&
          b?.middle != null &&
          b?.lower != null &&
          [b.upper, b.middle, b.lower].every((x) => Number.isFinite(x))
        ) {
          lines.push(
            `<div style="color:#93c5fd;font-size:11px;margin-top:4px">${periodLabel} BOLL(20,2) 上 ${b.upper.toFixed(3)}　中 ${b.middle.toFixed(3)}　下 ${b.lower.toFixed(3)}</div>`,
          )
        }
      }
      break
    }
  }

  for (const c of centralTips) {
    if (!dateInCentralRangeFull(dateLine, c.start_date, c.end_date)) continue
    const zg = Number(c.zg)
    const zd = Number(c.zd)
    const dd = (zg + zd) / 2
    const divHint =
      c.potential_divergence &&
      c.macd_area_enter != null &&
      c.macd_area_leave != null
        ? `<div style="color:#fbbf24;font-size:12px;margin-top:6px;font-weight:600">潜在背驰</div>` +
          `<div style="color:#94a3b8;font-size:11px;margin-top:2px">离开笔 MACD 面积小于进入笔（进入 ${c.macd_area_enter.toFixed(4)} / 离开 ${c.macd_area_leave.toFixed(4)}）</div>`
        : c.potential_divergence
          ? `<div style="color:#fbbf24;font-size:12px;margin-top:6px;font-weight:600">潜在背驰</div>` +
            `<div style="color:#94a3b8;font-size:11px;margin-top:2px">离开笔 MACD 面积小于进入笔</div>`
          : ''
    lines.push(
      `<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(148,163,184,0.35)">` +
        `<div style="color:#94a3b8;font-size:11px;margin-bottom:4px">${c.label}价位</div>` +
        `<div style="color:#e5e7eb;font-size:12px">ZG <span style="color:#38bdf8">${zg.toFixed(2)}</span></div>` +
        `<div style="color:#e5e7eb;font-size:12px">DD <span style="color:#a8b0bd">${dd.toFixed(2)}</span>（中轴）</div>` +
        `<div style="color:#e5e7eb;font-size:12px">ZD <span style="color:#f87171">${zd.toFixed(2)}</span></div>` +
        divHint +
        `</div>`,
    )
  }

  return lines.join('')
}

function buildHourlyTooltip(
  params: unknown,
  candleSeriesName: string,
  centralTips: CentralTipEntry[],
  klineRows?: IndexKlinePoint[],
  periodLabel: string = '60分钟',
): string {
  return (
    buildAxisTooltipHtmlHourly(params, candleSeriesName, centralTips, klineRows, periodLabel) +
    appendMacdTooltipBlock(params)
  )
}

export interface HourlyBuyConditions {
  /** 条件1：现价在一级或极限防线 ±1% 带内 */
  radarZoneOk: boolean
  /** 条件2：60m 有效笔末笔向下 */
  pen60mDown: boolean
  /** 条件3：MACD 绿柱面积较上一跌段缩小 */
  macdMomentumOk: boolean
  /** 条件4：合并后末三 K 严格底分型 */
  blueTriangleStrict: boolean
  /** 条件5：60m 现价在 C 中枢内（ZD～ZG） */
  inCCentral: boolean
  /** 条件6：60m 底背驰点落在当前向上笔内 */
  hasBottomDivInSwitch: boolean
  /** 条件7：60m BOLL 站回中轨 */
  bollBuy: boolean
}

/** 用户持仓信息（从 watchlist.json 读取） */
export interface HoldingInfo {
  code: string
  name: string
  cost?: number
  shares?: number
  note?: string
}

export function HourlyChanChart({
  data: indexKline,
  seriesName,
  dailyAZd,
  dailyCZd,
  dailyMacd,
  buyConditions,
  holdingInfo,
}: {
  data: IndexKlineResponse
  seriesName: string
  /** 日线 A 中枢下沿（与日线图一致，来自日线 centrals[0].zd） */
  dailyAZd: number | null
  /** 日线 C 中枢下沿（与日线图一致，来自日线最后一根中枢 zd） */
  dailyCZd: number | null
  /** 日线最后一根K线MACD（用于卖点防卖飞过滤） */
  dailyMacd?: { macd: number }
  /** 60分钟买点7条件（后端定时计算） */
  buyConditions?: HourlyBuyConditions
  /** 用户持仓信息（可选） */
  holdingInfo?: HoldingInfo
}) {
  const topFractals = (indexKline.fractals ?? [])
    .filter((f) => f.type === 'top')
    .map((f) => [f.date, f.price])
  const bottomFractals = (indexKline.fractals ?? [])
    .filter((f) => f.type === 'bottom')
    .map((f) => [f.date, f.price])
  const upPens = (indexKline.pens ?? []).filter((p) => p.direction === 'up')
  const downPens = (indexKline.pens ?? []).filter((p) => p.direction === 'down')
  const upPenSeries = upPens.map((p) => ({
    name: '一笔上（绿）',
    type: 'line' as const,
    data: [
      [p.start_date, p.start_price],
      [p.end_date, p.end_price],
    ],
    showSymbol: false,
    z: 2,
    lineStyle: { color: '#22c55e', width: 2 },
  }))
  const downPenSeries = downPens.map((p) => ({
    name: '一笔下（红）',
    type: 'line' as const,
    data: [
      [p.start_date, p.start_price],
      [p.end_date, p.end_price],
    ],
    showSymbol: false,
    z: 2,
    lineStyle: { color: '#ef4444', width: 2 },
  }))
  const segments = indexKline.segments ?? []
  const centralsRaw = indexKline.centrals ?? []
  const centrals = [...centralsRaw].sort((a, b) => {
    const byStart = a.start_date.localeCompare(b.start_date)
    if (byStart !== 0) return byStart
    return a.end_date.localeCompare(b.end_date)
  })
  const lastPoint = indexKline.data.length > 0 ? indexKline.data[indexKline.data.length - 1] : null
  const lastClose = lastPoint?.close ?? 0
  const lastDate = lastPoint?.date ?? ''
  const neutralLine = '#64748b'
  const cCentralIdx = centrals.length > 0 ? centrals.length - 1 : -1
  const periodLabel = indexKline.period === '15' ? '15分钟' : '60分钟'

  const centralLegendName = (i: number) => {
    if (centrals.length === 1) return 'C中枢'
    if (i === cCentralIdx) return 'C中枢'
    return CENTRAL_LABELS[i] ?? `中枢${i + 1}`
  }

  const pensEff = indexKline.pens_effective ?? []
  const lastPen = pensEff.length > 0 ? pensEff[pensEff.length - 1] : null
  const penDirLabel = lastPen
    ? lastPen.direction === 'up'
      ? '向上'
      : '向下'
    : '—'

  const dates = indexKline.data.map((p) => p.date)
  const difSeries = indexKline.data.map((p) =>
    p.macd?.dif != null && Number.isFinite(p.macd.dif) ? p.macd.dif : '-',
  )
  const deaSeries = indexKline.data.map((p) =>
    p.macd?.dea != null && Number.isFinite(p.macd.dea) ? p.macd.dea : '-',
  )
  const macdBarSeries = indexKline.data.map((p) =>
    p.macd?.macd != null && Number.isFinite(p.macd.macd) ? p.macd.macd : '-',
  )
  const divergenceArrows = divergenceArrowPointsFromDownPens(indexKline.data, pensEff)

  const dateToIdx = new Map(indexKline.data.map((p, i) => [p.date, i] as const))

  const centralTips: CentralTipEntry[] = centrals.map((c, i) => ({
    label: centralLegendName(i),
    zg: Number(c.zg),
    zd: Number(c.zd),
    start_date: c.start_date,
    end_date: c.end_date,
    potential_divergence: Boolean(c.potential_divergence),
    macd_area_enter: c.macd_area_enter,
    macd_area_leave: c.macd_area_leave ?? null,
  }))

  const bollData = buildBollLineData(indexKline.data)

  /**
   * 东阿阿胶：基于“双级别 C 中枢共振”的 3B（三类买点）识别
   * 仅用 60m 数据完成：比较收盘/最低/成交量与 MACD 绿柱（负值）面积。
   *
   * 注意：数据源分辨率为 60m，步骤 6 的“15 分钟撤销”无法精确到分钟级，
   * 这里采用“下一根 60m bar 若仍跌破 Daily_C_ZD 则撤销”的近似口径。
   */
  const ENABLE_3B = indexKline.symbol === '000423'
  const Daily_C_ZD = 57.41
  const H1_C_ZG = 56.39
  const EPS = 1e-6

  type PenMetrics = {
    lowMin: number
    closeMin: number
    highMax: number
    volumeSum: number
    macdNegAreaSum: number
    startIdx: number
    endIdx: number
  }

  function computePenMetrics(p: IndexPen): PenMetrics | null {
    const sIdx = dateToIdx.get(p.start_date)
    const eIdx = dateToIdx.get(p.end_date)
    if (sIdx == null || eIdx == null || sIdx > eIdx) return null
    const bars = indexKline.data.slice(sIdx, eIdx + 1)

    let lowMin = Infinity
    let closeMin = Infinity
    let highMax = -Infinity
    let volumeSum = 0
    let macdNegAreaSum = 0

    for (const b of bars) {
      if (![b.low, b.close, b.high, b.volume].every((v) => Number.isFinite(v))) continue
      lowMin = Math.min(lowMin, b.low)
      closeMin = Math.min(closeMin, b.close)
      highMax = Math.max(highMax, b.high)
      volumeSum += b.volume

      const m = b.macd?.macd
      if (m != null && Number.isFinite(m) && m < 0) {
        macdNegAreaSum += Math.abs(m)
      }
    }

    if (!Number.isFinite(lowMin) || !Number.isFinite(closeMin) || !Number.isFinite(highMax)) return null
    return { lowMin, closeMin, highMax, volumeSum, macdNegAreaSum, startIdx: sIdx, endIdx: eIdx }
  }

  function isInRange(date: string, start: string, end: string): boolean {
    return date >= start && date <= end
  }

  const threeBSignals = (() => {
    if (!ENABLE_3B) return []

    const pensSorted = [...(indexKline.pens ?? [])].sort((a, b) => {
      const byStart = a.start_date.localeCompare(b.start_date)
      if (byStart !== 0) return byStart
      return a.end_date.localeCompare(b.end_date)
    })

    const bottomFractalsInOrder = (indexKline.fractals ?? [])
      .filter((f) => f.type === 'bottom')
      .map((f) => [f.date, f.price] as const)
      .sort((a, b) => a[0].localeCompare(b[0]))

    let lastUpPen: IndexPen | null = null
    let lastUpMetrics: PenMetrics | null = null
    let lastDownPenMetrics: PenMetrics | null = null

    const res: { date: string; y: number }[] = []

    for (const pen of pensSorted) {
      if (pen.direction === 'up') {
        const m = computePenMetrics(pen)
        if (m) {
          lastUpPen = pen
          lastUpMetrics = m
        }
        continue
      }

      // red pen（向下笔）
      const curDown = computePenMetrics(pen)
      if (!curDown) {
        lastDownPenMetrics = null
        continue
      }

      // Step 2：确认已有向上笔
      if (!lastUpPen || !lastUpMetrics) {
        lastDownPenMetrics = curDown
        continue
      }

      const upHighOk = lastUpMetrics.highMax > Daily_C_ZD + EPS && lastUpMetrics.highMax > H1_C_ZG + EPS

      // Step 3：共振回踩条件
      const lowOk = curDown.lowMin >= Daily_C_ZD - EPS
      const closeAllOk = curDown.closeMin > H1_C_ZG - EPS

      // Step 4：过滤
      const volumeOk = curDown.volumeSum < lastUpMetrics.volumeSum * 0.5
      const macdAreaOk =
        lastDownPenMetrics != null ? curDown.macdNegAreaSum < lastDownPenMetrics.macdNegAreaSum : false

      if (upHighOk && lowOk && closeAllOk && volumeOk && macdAreaOk) {
        // Step 5：红笔出现底分型（转折确认）
        const bottomsInPen = bottomFractalsInOrder.filter(([d]) =>
          isInRange(d, pen.start_date, pen.end_date),
        )
        if (bottomsInPen.length > 0) {
          const signalDate = bottomsInPen[bottomsInPen.length - 1][0]
          const signalIdx = dateToIdx.get(signalDate)

          // Step 6：撤销（15 分钟无法精确，近似为下一根 60m 若再跌破则撤销）
          let cancelled = false
          if (signalIdx != null) {
            const nextBars = indexKline.data.slice(signalIdx + 1, signalIdx + 2)
            cancelled = nextBars.some((b) => b.low < Daily_C_ZD - EPS)
          }

          if (!cancelled) {
            // 箭头放在日线共振支撑下方一点，保证在 K 线下方
            const arrowY = Daily_C_ZD - 0.15
            res.push({ date: signalDate, y: arrowY })
          }
        }
      }

      lastDownPenMetrics = curDown
    }

    return res
  })()

  const threeBLast = threeBSignals.length ? threeBSignals[threeBSignals.length - 1] : null
  const threeBExtraMinPrice = threeBLast ? threeBLast.y : null

  // 使用后端定时计算的7个条件，如果没有则回退到前端实时计算
  const {
    signalMarker,
    flags,
    sellSignalActive,
    firstBuyPoint,
    secondBuyPoint,
    firstBuyFailed,
    secondBuyFailed,
    thirdBuyFailed,
    thirdBuyPoint,
    firstSellPoint,
    secondSellPoint,
    thirdSellPoint,
  } = computeHourlyBuySellState(indexKline, dailyAZd, dailyCZd, dailyMacd)

  // ========== 日线破位强制降级 ==========
  // 若当前价跌破日线绝对防线 MIN(C-ZD, A-ZD)，所有买点信号强制灰度化，isExecutable=false
  const isDailyBroken =
    firstBuyPoint?.isExecutable === false ||
    secondBuyPoint?.isExecutable === false ||
    thirdBuyPoint?.isExecutable === false
  const DEGRADED_COLOR = '#666666'

  // ========== 日线核心伏击圈跨级别风控 ==========
  // 核心伏击圈基准线：取 C-ZD 和 A-ZD 中较大的一个（即绝对防线）
  const baseZd =
    dailyAZd != null && dailyCZd != null
      ? Math.max(dailyAZd, dailyCZd)
      : null
  const lastPrice = indexKline.data.length > 0 ? indexKline.data[indexKline.data.length - 1].close : 0
  // 伏击圈 = 绝对防线向上 3% 范围内
  const isInAmbushZone = baseZd != null && lastPrice > 0 ? lastPrice <= baseZd * 1.03 : true
  const dailyBias = baseZd != null && lastPrice > 0 ? ((lastPrice - baseZd) / baseZd) * 100 : 0
  const AMBUSH_WARNING_COLOR = '#f97316' // 橙色警示

  /** 统一生成买点标签：已失效 > 日线破位 > 高乖离 > 正常 */
  function getBuyLabel(
    point: { isDestroyed?: boolean },
    type: string,
    defaultLabel: string,
  ): string {
    if (point.isDestroyed) return `${type}(已失效)`
    if (isDailyBroken) return `${type}(日线破位·放弃)`
    if (!isInAmbushZone) return `[高乖离]${type}`
    return defaultLabel
  }

  /** 统一生成买点颜色：已失效/日线破位=灰色，高乖离=橙色，正常=原色 */
  function getBuyColor(
    point: { isDestroyed?: boolean },
    defaultColor: string,
  ): string {
    if (point.isDestroyed || isDailyBroken) return DEGRADED_COLOR
    if (!isInAmbushZone) return AMBUSH_WARNING_COLOR
    return defaultColor
  }

  /** 高乖离风控仓位建议文本 */
  function getAmbushPositionAdvice(): string {
    return `⚠️ 脱离日线伏击圈(乖离率 ${dailyBias.toFixed(2)}%)，建议放弃或极轻仓(10%)！`
  }

  // 二三买共振：同一根K线上同时出现二买和三买信号
  const isBuy23Resonance =
    secondBuyPoint?.hasSignal &&
    thirdBuyPoint?.hasSignal &&
    secondBuyPoint.date === thirdBuyPoint.date

  // 优先使用后端定时计算的7个条件
  const buyConditionChecklist = buyConditions
    ? [
        { label: '【日线】未跌破绝对防线 MIN(C-ZD, A-ZD)', ok: buyConditions.radarZoneOk },
        { label: '【60m】现价在 C 中枢内（ZD～ZG）', ok: buyConditions.inCCentral },
        { label: '【60m】有效笔：前一下笔、当前上笔', ok: buyConditions.pen60mDown },
        { label: '【60m】当前向上笔内有底分型', ok: buyConditions.blueTriangleStrict },
        { label: '【60m】底背驰点落在当前向上笔内', ok: buyConditions.hasBottomDivInSwitch },
        { label: '【60m】MACD 转强', ok: buyConditions.macdMomentumOk },
        { label: '【60m】BOLL 站回中轨', ok: buyConditions.bollBuy },
      ]
    : [
        { label: '【日线】未跌破绝对防线 MIN(C-ZD, A-ZD)', ok: flags.keepDailySupport },
        { label: '【60m】现价在 C 中枢内（ZD～ZG）', ok: flags.inCCentral },
        { label: '【60m】有效笔：前一下笔、当前上笔', ok: flags.switchedDownToUp },
        { label: '【60m】当前向上笔内有底分型', ok: flags.hasBottomFractalInSwitch },
        { label: '【60m】底背驰点落在当前向上笔内', ok: flags.hasBottomDivInSwitch },
        { label: '【60m】MACD 转强', ok: flags.macdBuy },
        { label: '【60m】BOLL 站回中轨', ok: flags.bollBuy },
      ]

  const priceYExtent = mainChartYExtent(indexKline.data, [
    ...centrals.flatMap((c) => [Number(c.zd), Number(c.zg)]),
    dailyAZd,
    dailyCZd,
    ...bollExtentPrices(indexKline.data),
    threeBExtraMinPrice,
    signalMarker?.y,
    firstBuyPoint?.hasSignal && !firstBuyPoint?.suppressed ? firstBuyPoint.price : undefined,
    firstBuyPoint?.suppressed ? firstBuyPoint.price : undefined,
    secondBuyPoint?.hasSignal ? secondBuyPoint.price : undefined,
    firstBuyFailed?.hasSignal ? firstBuyFailed.price : undefined,
    secondBuyFailed?.hasSignal ? secondBuyFailed.price : undefined,
    thirdBuyFailed?.hasSignal ? thirdBuyFailed.price : undefined,
    thirdBuyPoint?.hasSignal ? thirdBuyPoint.price : undefined,
    firstSellPoint?.hasSignal ? firstSellPoint.price : undefined,
    secondSellPoint?.hasSignal ? secondSellPoint.price : undefined,
    thirdSellPoint?.hasSignal ? thirdSellPoint.price : undefined,
  ])

  const centralMarkLineData: unknown[] = []
  if (dailyCZd != null && Number.isFinite(dailyCZd)) {
    centralMarkLineData.push({
      name: '日线 C-ZD',
      yAxis: dailyCZd,
      lineStyle: { type: 'dashed' as const, color: '#dc2626', width: 3 },
      label: { show: false },
      emphasis: {
        label: {
          show: true,
          formatter: '日线 C-ZD',
          color: '#fca5a5',
          fontSize: 10,
          position: 'end',
          backgroundColor: 'rgba(15,23,42,0.85)',
          padding: [2, 5],
          borderRadius: 4,
        },
      },
    })
  }
  if (dailyAZd != null && Number.isFinite(dailyAZd)) {
    centralMarkLineData.push({
      name: '日线 A-ZD',
      yAxis: dailyAZd,
      lineStyle: { type: 'dashed' as const, color: '#dc2626', width: 3 },
      label: { show: false },
      emphasis: {
        label: {
          show: true,
          formatter: '日线 A-ZD',
          color: '#fca5a5',
          fontSize: 10,
          position: 'end',
          backgroundColor: 'rgba(15,23,42,0.85)',
          padding: [2, 5],
          borderRadius: 4,
        },
      },
    })
  }
  if (centrals.length > 0) {
    centrals.forEach((c, i) => {
      const zg = Number(c.zg)
      const zd = Number(c.zd)
      const dd = (zg + zd) / 2
      const centralName = centralLegendName(i)
      const zgColor = lastClose > zg ? '#22c55e' : neutralLine
      const zdColor = lastClose < zd ? '#ef4444' : neutralLine
      const ddColor = '#94a3b8'
      const sd = c.start_date
      const ed = c.end_date
      const hideTip = { show: false as const }
      centralMarkLineData.push({
        yAxis: zg,
        lineStyle: { type: 'dashed' as const, color: zgColor, width: 2 },
        label: { show: false },
        emphasis: {
          label: {
            show: true,
            formatter: `${centralName} ZG`,
            color: '#cbd5e1',
            fontSize: 9,
            position: 'start',
            backgroundColor: 'rgba(15,23,42,0.85)',
            padding: [2, 5],
            borderRadius: 4,
          },
        },
      })
      centralMarkLineData.push({
        yAxis: zd,
        lineStyle: { type: 'dashed' as const, color: zdColor, width: 2 },
        label: { show: false },
        emphasis: {
          label: {
            show: true,
            formatter: `${centralName} ZD`,
            color: '#cbd5e1',
            fontSize: 9,
            position: 'start',
            backgroundColor: 'rgba(15,23,42,0.85)',
            padding: [2, 5],
            borderRadius: 4,
          },
        },
      })
      centralMarkLineData.push([
        {
          coord: [sd, dd] as [string, number],
          symbol: 'none',
          lineStyle: { type: 'solid' as const, color: ddColor, width: 1 },
          label: hideTip,
        },
        { coord: [ed, dd] as [string, number], symbol: 'none' },
      ])
    })
  }

  const centralMarkLine =
    centralMarkLineData.length > 0
      ? {
          symbol: 'none',
          z: 2,
          animation: false,
          data: centralMarkLineData,
        }
      : undefined

  const segmentSeries = segments.map((s) => {
    let lineData: [string, number][]
    if (s.points && s.points.length >= 2) {
      lineData = s.points.map((pt) => [pt[0], pt[1]] as [string, number])
    } else if (
      typeof s.effective_pen_start_idx === 'number' &&
      typeof s.effective_pen_end_idx === 'number' &&
      pensEff.length > 0
    ) {
      const pl = segmentPolylineFromEffective(
        pensEff,
        s.effective_pen_start_idx,
        s.effective_pen_end_idx,
      )
      lineData =
        pl.length >= 2
          ? pl
          : [
              [s.start_date, s.start_price],
              [s.end_date, s.end_price],
            ]
    } else {
      lineData = [
        [s.start_date, s.start_price],
        [s.end_date, s.end_price],
      ]
    }
    return {
      name: '线段（紫）',
      type: 'line' as const,
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: lineData,
      showSymbol: false,
      z: 3,
      lineStyle: {
        color: 'rgba(147, 51, 234, 0.42)',
        width: 6,
      },
    }
  })

  const lightBlueArea = {
    color: 'rgba(59, 130, 246, 0.14)',
    borderColor: 'rgba(96, 165, 250, 0.35)',
    borderWidth: 0.5,
  }
  const lightBlueAreaPotentialDiv = {
    ...lightBlueArea,
    borderColor: 'rgba(251, 191, 36, 0.92)',
    borderWidth: 2.5,
  }

  const legendItems = [
    seriesName,
    ...(bollData.hasAny ? (['BOLL(20,2)', 'BOLL中轨'] as const) : []),
    '顶分型',
    '底分型',
    '一笔上（绿）',
    '一笔下（红）',
    '线段（紫）',
    'MACD柱',
    'DIF',
    'DEA',
    ...(divergenceArrows.length ? ['底背驰'] : []),
    ...(firstBuyPoint?.hasSignal && !firstBuyPoint?.suppressed ? [getBuyLabel(firstBuyPoint, '一买', '一买')] : []),
    ...(firstBuyPoint?.suppressed ? [getBuyLabel(firstBuyPoint, '一买↑', '一买(已升级)')] : []),
    ...(isBuy23Resonance ? [getBuyLabel({ isDestroyed: (secondBuyPoint?.isDestroyed || false) || (thirdBuyPoint?.isDestroyed || false) }, '二三买共振', '二三买共振')] : []),
    ...(secondBuyPoint?.hasSignal && !isBuy23Resonance ? [getBuyLabel(secondBuyPoint, '二买', '二买')] : []),
    ...(thirdBuyPoint?.hasSignal && !isBuy23Resonance ? [getBuyLabel(thirdBuyPoint, '三买', '三买')] : []),
    ...(firstBuyFailed?.hasSignal ? ['一买失败'] : []),
    ...(secondBuyFailed?.hasSignal ? ['二买失败'] : []),
    ...(thirdBuyFailed?.hasSignal ? ['三买失败'] : []),
    ...(firstSellPoint?.hasSignal ? ['一卖'] : []),
    ...(secondSellPoint?.hasSignal ? ['二卖'] : []),
    ...(thirdSellPoint?.hasSignal ? ['三卖'] : []),
  ]

  return (
    <div className="daily-chart-shell hourly-chart-shell">
      <div className="daily-chart-row">
        <div className="daily-chart-chart-wrap">
          <ReactECharts
            opts={{ renderer: 'svg' }}
            notMerge
            option={{
              axisPointer: {
                link: [{ xAxisIndex: [0, 1] }],
              },
              tooltip: {
                trigger: 'axis',
                confine: true,
                backgroundColor: 'rgba(15, 23, 42, 0.94)',
                borderColor: 'rgba(148, 163, 184, 0.35)',
                textStyle: { color: '#e5e7eb' },
                formatter: (params: unknown) => {
                  const pArr = Array.isArray(params) ? params : [params]
                  const axisDate = pArr.length > 0 && (pArr[0] as { axisValue?: string }).axisValue ? (pArr[0] as { axisValue: string }).axisValue : ''
                  const base = buildHourlyTooltip(params, seriesName, centralTips, indexKline.data, periodLabel)
                  let extra = ''
                  // 显示买卖信号（仅当信号时间与当前K线匹配时）
                  if (signalMarker?.reasons?.length && signalMarker.date === axisDate) {
                    extra += `<div style="margin-top:6px;color:${signalMarker.color};font-weight:700">信号：${signalMarker.text}</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${signalMarker.date}<br/>` +
                      signalMarker.reasons.map((r) => `- ${r}`).join('<br/>') +
                      `</div>`
                  }
                  // 显示一买信号（仅当信号时间与当前K线匹配时）
                  if (firstBuyPoint?.hasSignal && !firstBuyPoint?.suppressed && firstBuyPoint.date === axisDate) {
                    const b1Destroyed = firstBuyPoint.isDestroyed === true
                    const b1AmbushWarn = !b1Destroyed && !isDailyBroken && !isInAmbushZone
                    const b1Label = getBuyLabel(firstBuyPoint, '一买', '一买信号')
                    const b1Color = getBuyColor(firstBuyPoint, '#f59e0b')
                    extra += `<div style="margin-top:6px;color:${b1Color};font-weight:700">${b1Label}</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${firstBuyPoint.date}<br/>` +
                      `- 触发价: ${firstBuyPoint.price.toFixed(2)}<br/>` +
                      `- 止损线: ${firstBuyPoint.stopLoss.toFixed(2)}<br/>` +
                      `- 背驰强度: ${(firstBuyPoint.areaRatio * 100).toFixed(1)}%<br/>` +
                      `- <span style="color:${b1AmbushWarn ? AMBUSH_WARNING_COLOR : '#fbbf24'}">${b1AmbushWarn ? getAmbushPositionAdvice() : '[左侧试探] 建议建仓 20% (约 1万)'}</span><br/>` +
                      `${b1Destroyed ? '- <span style="color:' + DEGRADED_COLOR + '">⚠ 已失效：后续价格跌破止损线，原买点结构被破坏</span><br/>' : ''}` +
                      `${!b1Destroyed && isDailyBroken ? '- <span style="color:' + DEGRADED_COLOR + '">⚠ 日线破位：已跌破绝对防线 MIN(C-ZD, A-ZD)，信号强制降级为不可执行</span><br/>' : ''}` +
                      `${b1AmbushWarn ? '- <span style="color:' + AMBUSH_WARNING_COLOR + '">⚠ 跨级别风控：当前不在日线核心伏击圈内，高乖离状态建议大幅削减仓位</span><br/>' : ''}` +
                      `</div>`
                  }
                  // 显示已升级的一买信号（被二买覆盖，仅当信号时间与当前K线匹配时）
                  if (firstBuyPoint?.suppressed && firstBuyPoint.date === axisDate) {
                    const b1Destroyed = firstBuyPoint.isDestroyed === true
                    const b1AmbushWarn = !b1Destroyed && !isDailyBroken && !isInAmbushZone
                    const b1Color = getBuyColor(firstBuyPoint, '#9ca3af')
                    extra += `<div style="margin-top:6px;color:${b1Color};font-weight:700">${getBuyLabel(firstBuyPoint, '一买↑', '一买（已升级为二买）')}</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${firstBuyPoint.date}<br/>` +
                      `- 触发价: ${firstBuyPoint.price.toFixed(2)}<br/>` +
                      `- 背驰强度: ${(firstBuyPoint.areaRatio * 100).toFixed(1)}%<br/>` +
                      `- <span style="color:${b1AmbushWarn ? AMBUSH_WARNING_COLOR : '#fbbf24'}">${b1AmbushWarn ? getAmbushPositionAdvice() : '[左侧试探] 建议建仓 20% (约 1万)'}</span><br/>` +
                      firstBuyPoint.reasons.filter((r) => r.includes('已升级')).map((r) => `- ${r}`).join('<br/>') +
                      `${b1Destroyed ? '<br/>- <span style="color:' + DEGRADED_COLOR + '">⚠ 已失效：后续价格跌破止损线，原买点结构被破坏</span>' : ''}` +
                      `${!b1Destroyed && isDailyBroken ? '<br/>- <span style="color:' + DEGRADED_COLOR + '">⚠ 日线破位：已跌破绝对防线 MIN(C-ZD, A-ZD)，信号强制降级为不可执行</span>' : ''}` +
                      `${b1AmbushWarn ? '<br/>- <span style="color:' + AMBUSH_WARNING_COLOR + '">⚠ 跨级别风控：当前不在日线核心伏击圈内，高乖离状态建议大幅削减仓位</span>' : ''}` +
                      `</div>`
                  }
                  // 二三买共振合并显示
                  if (isBuy23Resonance && secondBuyPoint.date === axisDate) {
                    const b23Destroyed = secondBuyPoint.isDestroyed === true || thirdBuyPoint.isDestroyed === true
                    const b23AmbushWarn = !b23Destroyed && !isDailyBroken && !isInAmbushZone
                    const b23Color = getBuyColor({ isDestroyed: b23Destroyed }, '#a855f7')
                    extra += `<div style="margin-top:6px;color:${b23Color};font-weight:700">${getBuyLabel({ isDestroyed: b23Destroyed }, '二三买共振', '二三买共振')}</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${secondBuyPoint.date}<br/>` +
                      `- 【二买】` + secondBuyPoint.reasons.map((r) => `${r}`).join(' | ') + `<br/>` +
                      `- 【三买】` + thirdBuyPoint.reasons.map((r) => `${r}`).join(' | ') + `<br/>` +
                      `${b23AmbushWarn ? '- <span style="color:' + AMBUSH_WARNING_COLOR + '">' + getAmbushPositionAdvice() + '</span><br/>' : ''}` +
                      `${b23Destroyed ? '- <span style="color:' + DEGRADED_COLOR + '">⚠ 已失效：后续价格跌破止损线，原买点结构被破坏</span>' : ''}` +
                      `${!b23Destroyed && isDailyBroken ? '- <span style="color:' + DEGRADED_COLOR + '">⚠ 日线破位：已跌破绝对防线 MIN(C-ZD, A-ZD)，信号强制降级为不可执行</span>' : ''}` +
                      `${b23AmbushWarn ? '- <span style="color:' + AMBUSH_WARNING_COLOR + '">⚠ 跨级别风控：当前不在日线核心伏击圈内，高乖离状态建议大幅削减仓位</span>' : ''}` +
                      `</div>`
                  }
                  // 显示二买信号（仅当信号时间与当前K线匹配时，且未与三买共振）
                  if (secondBuyPoint?.hasSignal && secondBuyPoint.date === axisDate && !isBuy23Resonance) {
                    const b2Destroyed = secondBuyPoint.isDestroyed === true
                    const b2AmbushWarn = !b2Destroyed && !isDailyBroken && !isInAmbushZone
                    const b2Color = getBuyColor(secondBuyPoint, '#8b5cf6')
                    extra += `<div style="margin-top:6px;color:${b2Color};font-weight:700">${getBuyLabel(secondBuyPoint, '二买', '二买信号')}</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${secondBuyPoint.date}<br/>` +
                      secondBuyPoint.reasons.map((r) => `- ${r}`).join('<br/>') +
                      `${b2AmbushWarn ? '<br/>- <span style="color:' + AMBUSH_WARNING_COLOR + '">' + getAmbushPositionAdvice() + '</span>' : ''}` +
                      `${b2Destroyed ? '<br/>- <span style="color:' + DEGRADED_COLOR + '">⚠ 已失效：后续价格跌破止损线，原买点结构被破坏</span>' : ''}` +
                      `${!b2Destroyed && isDailyBroken ? '<br/>- <span style="color:' + DEGRADED_COLOR + '">⚠ 日线破位：已跌破绝对防线 MIN(C-ZD, A-ZD)，信号强制降级为不可执行</span>' : ''}` +
                      `${b2AmbushWarn ? '<br/>- <span style="color:' + AMBUSH_WARNING_COLOR + '">⚠ 跨级别风控：当前不在日线核心伏击圈内，高乖离状态建议大幅削减仓位</span>' : ''}` +
                      `</div>`
                  }
                  // 显示一买失败信号（仅当信号时间与当前K线匹配时）
                  if (firstBuyFailed?.hasSignal && firstBuyFailed.date === axisDate) {
                    extra += `<div style="margin-top:6px;color:#ef4444;font-weight:700">一买失败（左侧试错失败）</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${firstBuyFailed.date}<br/>` +
                      firstBuyFailed.reasons.map((r) => `- ${r}`).join('<br/>') +
                      `</div>`
                  }
                  // 显示二买失败信号（仅当信号时间与当前K线匹配时）
                  if (secondBuyFailed?.hasSignal && secondBuyFailed.date === axisDate) {
                    extra += `<div style="margin-top:6px;color:#ef4444;font-weight:700">二买失败（右侧确认失败）</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${secondBuyFailed.date}<br/>` +
                      secondBuyFailed.reasons.map((r) => `- ${r}`).join('<br/>') +
                      `</div>`
                  }
                  // 显示三买信号（仅当信号时间与当前K线匹配时，且未与二买共振）
                  if (thirdBuyPoint?.hasSignal && thirdBuyPoint.date === axisDate && !isBuy23Resonance) {
                    const b3Destroyed = thirdBuyPoint.isDestroyed === true
                    const b3AmbushWarn = !b3Destroyed && !isDailyBroken && !isInAmbushZone
                    const b3Color = getBuyColor(thirdBuyPoint, '#f97316')
                    extra += `<div style="margin-top:6px;color:${b3Color};font-weight:700">${getBuyLabel(thirdBuyPoint, '三买', '三买信号')}</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${thirdBuyPoint.date}<br/>` +
                      thirdBuyPoint.reasons.map((r) => `- ${r}`).join('<br/>') +
                      `${b3AmbushWarn ? '<br/>- <span style="color:' + AMBUSH_WARNING_COLOR + '">' + getAmbushPositionAdvice() + '</span>' : ''}` +
                      `${b3Destroyed ? '<br/>- <span style="color:' + DEGRADED_COLOR + '">⚠ 已失效：后续价格跌破止损线，原买点结构被破坏</span>' : ''}` +
                      `${!b3Destroyed && isDailyBroken ? '<br/>- <span style="color:' + DEGRADED_COLOR + '">⚠ 日线破位：已跌破绝对防线 MIN(C-ZD, A-ZD)，信号强制降级为不可执行</span>' : ''}` +
                      `${b3AmbushWarn ? '<br/>- <span style="color:' + AMBUSH_WARNING_COLOR + '">⚠ 跨级别风控：当前不在日线核心伏击圈内，高乖离状态建议大幅削减仓位</span>' : ''}` +
                      `</div>`
                  }
                  // 显示三买失败信号（仅当信号时间与当前K线匹配时）
                  if (thirdBuyFailed?.hasSignal && thirdBuyFailed.date === axisDate) {
                    extra += `<div style="margin-top:6px;color:#ef4444;font-weight:700">三买失败·止损</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${thirdBuyFailed.date}<br/>` +
                      thirdBuyFailed.reasons.map((r) => `- ${r}`).join('<br/>') +
                      `</div>`
                  }
                  // 显示一卖信号（仅当信号时间与当前K线匹配时，根据tier显示不同标题）
                  if (firstSellPoint?.hasSignal && firstSellPoint.date === axisDate) {
                    const tier = firstSellPoint.tier
                    const sellTitle = tier === 'weak' ? '弱卖（洗盘）' : tier === 'half' ? '半仓卖' : '清仓卖'
                    const sellColor = tier === 'weak' ? '#f59e0b' : tier === 'half' ? '#f97316' : '#ef4444'
                    extra += `<div style="margin-top:6px;color:${sellColor};font-weight:700">一卖·${sellTitle}</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${firstSellPoint.date}<br/>` +
                      `- 触发价: ${firstSellPoint.price.toFixed(2)}<br/>` +
                      `- 止损线: ${firstSellPoint.stopLoss.toFixed(2)}<br/>` +
                      `- 背驰强度: ${(firstSellPoint.areaRatio * 100).toFixed(1)}%<br/>` +
                      firstSellPoint.reasons.map((r) => `- ${r}`).join('<br/>') +
                      `</div>`
                  }
                  // 显示二卖信号（仅当信号时间与当前K线匹配时，根据tier显示不同标题）
                  if (secondSellPoint?.hasSignal && secondSellPoint.date === axisDate) {
                    const tier = secondSellPoint.tier
                    const sellTitle = tier === 'weak' ? '弱卖（洗盘）' : tier === 'half' ? '半仓卖' : '清仓卖'
                    const sellColor = tier === 'weak' ? '#f59e0b' : tier === 'half' ? '#f97316' : '#ef4444'
                    extra += `<div style="margin-top:6px;color:${sellColor};font-weight:700">二卖·${sellTitle}</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${secondSellPoint.date}<br/>` +
                      secondSellPoint.reasons.map((r) => `- ${r}`).join('<br/>') +
                      `</div>`
                  }
                  // 显示三卖信号（仅当信号时间与当前K线匹配时，三卖无条件清仓）
                  if (thirdSellPoint?.hasSignal && thirdSellPoint.date === axisDate) {
                    extra += `<div style="margin-top:6px;color:#ef4444;font-weight:700">三卖·清仓卖</div>` +
                      `<div style="color:#cbd5e1;font-size:11px;line-height:1.45">` +
                      `- 时间: ${thirdSellPoint.date}<br/>` +
                      thirdSellPoint.reasons.map((r) => `- ${r}`).join('<br/>') +
                      `</div>`
                  }
                  return base + extra
                },
              },
              legend: {
                type: 'scroll',
                top: 6,
                left: 'center',
                width: '92%',
                data: legendItems,
                textStyle: { color: '#9ca3af', fontSize: 11 },
              },
              grid: [
                { left: 40, right: 48, top: 52, bottom: 138 },
                { left: 40, right: 48, bottom: 38, height: 100 },
              ],
              xAxis: [
                {
                  type: 'category',
                  data: dates,
                  gridIndex: 0,
                  axisLabel: { show: false },
                  axisTick: { show: false },
                },
                {
                  type: 'category',
                  data: dates,
                  gridIndex: 1,
                  axisLabel: {
                    color: '#9ca3af',
                    rotate: 45,
                    fontSize: 9,
                    formatter: (value: string, index: number) => {
                      const [datePart, timePart = ''] = String(value).split(' ')
                      const mmdd = datePart?.length >= 10 ? datePart.slice(5) : datePart
                      const hhmm = timePart?.length >= 5 ? timePart.slice(0, 5) : timePart
                      if (index <= 0) return hhmm ? `${mmdd}\n${hhmm}` : mmdd
                      const prev = String(dates[index - 1] ?? '')
                      const [prevDatePart = ''] = prev.split(' ')
                      const isNewDay = prevDatePart !== datePart
                      if (isNewDay) return hhmm ? `${mmdd}\n${hhmm}` : mmdd
                      return hhmm || mmdd
                    },
                  },
                },
              ],
              yAxis: [
                {
                  gridIndex: 0,
                  scale: true,
                  type: 'value',
                  min: priceYExtent.min,
                  max: priceYExtent.max,
                  axisLabel: {
                    color: '#9ca3af',
                    formatter: formatPriceYAxisLabel,
                  },
                  splitLine: { lineStyle: { color: '#1f2937' } },
                },
                {
                  gridIndex: 1,
                  scale: true,
                  type: 'value',
                  axisLabel: {
                    color: '#9ca3af',
                    fontSize: 10,
                    formatter: formatMacdYAxisLabel,
                  },
                  splitLine: { lineStyle: { color: '#1f2937' } },
                },
              ],
              dataZoom: [
                { type: 'inside', xAxisIndex: [0, 1] },
                { type: 'slider', xAxisIndex: [0, 1], height: 18, bottom: 5 },
              ],
              series: [
                ...(bollData.hasAny
                  ? [
                      {
                        name: 'BOLL(20,2)',
                        type: 'line' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: bollData.lower,
                        showSymbol: false,
                        stack: 'boll',
                        silent: true,
                        z: 0,
                        lineStyle: {
                          width: 1,
                          color: 'rgba(96, 165, 250, 0.35)',
                        },
                      },
                      {
                        name: 'BOLL(20,2)',
                        type: 'line' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: bollData.bandWidth,
                        showSymbol: false,
                        stack: 'boll',
                        silent: true,
                        z: 0,
                        lineStyle: {
                          width: 1,
                          color: 'rgba(96, 165, 250, 0.35)',
                        },
                        areaStyle: {
                          color: 'rgba(59, 130, 246, 0.14)',
                        },
                      },
                      {
                        name: 'BOLL中轨',
                        type: 'line' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: bollData.middle,
                        showSymbol: false,
                        silent: true,
                        z: 1,
                        lineStyle: {
                          type: 'dashed' as const,
                          width: 1,
                          color: 'rgba(148, 163, 184, 0.85)',
                        },
                      },
                    ]
                  : []),
                {
                  name: seriesName,
                  type: 'candlestick',
                  xAxisIndex: 0,
                  yAxisIndex: 0,
                  z: 4,
                  data: indexKline.data.map((p) => [p.open, p.close, p.low, p.high]),
                  itemStyle: {
                    color: '#ef4444',
                    color0: '#22c55e',
                    borderColor: '#ef4444',
                    borderColor0: '#22c55e',
                  },
                  markArea: centrals.length
                    ? {
                        silent: true,
                        z: 1,
                        data: centrals.map((c, i) => {
                          const pot = Boolean(c.potential_divergence)
                          const areaStyle = pot ? lightBlueAreaPotentialDiv : lightBlueArea
                          return [
                            {
                              name: pot
                                ? `${centralLegendName(i)} · 潜在背驰`
                                : centralLegendName(i),
                              xAxis: c.start_date,
                              yAxis: c.zg,
                              itemStyle: areaStyle,
                            },
                            {
                              xAxis: c.end_date,
                              yAxis: c.zd,
                              itemStyle: areaStyle,
                            },
                          ]
                        }),
                      }
                    : undefined,
                  markLine: centralMarkLine,
                },
                {
                  name: '顶分型',
                  type: 'scatter',
                  xAxisIndex: 0,
                  yAxisIndex: 0,
                  z: 5,
                  data: topFractals,
                  symbol: 'triangle',
                  symbolSize: 10,
                  itemStyle: { color: '#f59e0b' },
                },
                {
                  name: '底分型',
                  type: 'scatter',
                  xAxisIndex: 0,
                  yAxisIndex: 0,
                  z: 5,
                  data: bottomFractals,
                  symbol: 'triangle',
                  symbolRotate: 180,
                  symbolSize: 10,
                  itemStyle: { color: '#60a5fa' },
                },
                ...(threeBLast
                  ? [
                      {
                        name: '极品3B',
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[threeBLast.date, threeBLast.y]] as unknown as [string, number][],
                        symbol: 'triangle',
                        symbolRotate: 0, // 向上箭头
                        symbolSize: [18, 18],
                        itemStyle: {
                          color: '#fbbf24',
                          borderColor: '#d97706',
                          borderWidth: 2,
                        },
                        label: {
                          show: true,
                          formatter: '极品3B: 57.41共振支撑',
                          color: '#fbbf24',
                          fontWeight: 'bold',
                          fontSize: 12,
                          position: 'bottom',
                          distance: 6,
                        },
                        z: 6,
                        silent: true,
                      },
                    ]
                  : []),
                ...upPenSeries.map((s) => ({ ...s, xAxisIndex: 0, yAxisIndex: 0, z: 5 })),
                ...downPenSeries.map((s) => ({ ...s, xAxisIndex: 0, yAxisIndex: 0, z: 5 })),
                ...segmentSeries.map((s) => ({ ...s, z: 6 })),
                ...(divergenceArrows.length
                  ? [
                      {
                        name: '底背驰',
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: divergenceArrows,
                        symbol: 'rect',
                        symbolSize: [10, 10],
                        itemStyle: {
                          color: '#fde047',
                          borderColor: 'rgba(113, 63, 18, 0.55)',
                          borderWidth: 1,
                        },
                        z: 12,
                        symbolOffset: [0, 10],
                      },
                    ]
                  : []),
                ...(signalMarker
                  ? [
                      {
                        name: signalMarker.text,
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[signalMarker.date, signalMarker.y]] as [string, number][],
                        symbol: 'circle',
                        symbolSize: 4,
                        itemStyle: { color: signalMarker.color },
                        label: {
                          show: true,
                          formatter: signalMarker.text,
                          position: 'top',
                          distance: 8,
                          color: signalMarker.color,
                          fontWeight: 'bold',
                          fontSize: 13,
                        },
                        z: 15,
                      },
                    ]
                  : []),
                // 第一类买点（一买）标记 — 正常状态（isDestroyed 优先于 isDailyBroken）
                ...(firstBuyPoint?.hasSignal && !firstBuyPoint?.suppressed
                  ? [
                      {
                        name: getBuyLabel(firstBuyPoint, '一买', '一买'),
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[firstBuyPoint.date, firstBuyPoint.price]] as [string, number][],
                        symbol: 'diamond',
                        symbolSize: firstBuyPoint.isDestroyed ? 9 : (isDailyBroken ? 10 : (!isInAmbushZone ? 11 : 12)),
                        itemStyle: { color: getBuyColor(firstBuyPoint, '#f59e0b'), opacity: firstBuyPoint.isDestroyed ? 0.5 : (isDailyBroken ? 0.6 : 1) },
                        label: {
                          show: true,
                          formatter: firstBuyPoint.isDestroyed ? '一买(已失效)' : (isDailyBroken ? '一买(日线破位·放弃)' : (!isInAmbushZone ? '[高乖离]一买·左试20%' : '一买·左试20%')),
                          position: 'bottom',
                          distance: firstBuyPoint.isDestroyed ? 10 : (isDailyBroken ? 12 : 15),
                          color: getBuyColor(firstBuyPoint, '#f59e0b'),
                          fontWeight: 'bold',
                          fontSize: firstBuyPoint.isDestroyed ? 12 : (isDailyBroken ? 14 : (!isInAmbushZone ? 16 : 20)),
                        },
                        z: 25,
                      },
                    ]
                  : []),
                // 第一类买点（一买）标记 — 已升级状态（被二买覆盖，灰显；isDestroyed 优先降级）
                ...(firstBuyPoint?.suppressed
                  ? [
                      {
                        name: getBuyLabel(firstBuyPoint, '一买↑', '一买(已升级)'),
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[firstBuyPoint.date, firstBuyPoint.price]] as [string, number][],
                        symbol: 'diamond',
                        symbolSize: firstBuyPoint.isDestroyed ? 9 : 10,
                        itemStyle: { color: getBuyColor(firstBuyPoint, '#9ca3af'), opacity: firstBuyPoint.isDestroyed ? 0.5 : 0.6 },
                        label: {
                          show: true,
                          formatter: firstBuyPoint.isDestroyed ? '一买↑(已失效)' : (isDailyBroken ? '一买↑(日线破位·放弃)' : (!isInAmbushZone ? '[高乖离]一买↑' : '一买↑')),
                          position: 'bottom',
                          distance: firstBuyPoint.isDestroyed ? 10 : 12,
                          color: getBuyColor(firstBuyPoint, '#9ca3af'),
                          fontWeight: 'bold',
                          fontSize: firstBuyPoint.isDestroyed ? 12 : 14,
                        },
                        z: 24,
                      },
                    ]
                  : []),
                // 二三买共振标记（同一K线二买+三买合并；isDestroyed 优先降级）
                ...(isBuy23Resonance
                  ? [
                      {
                        name: getBuyLabel({ isDestroyed: secondBuyPoint.isDestroyed || thirdBuyPoint.isDestroyed }, '二三买共振', '二三买共振'),
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[secondBuyPoint.date, secondBuyPoint.price]] as [string, number][],
                        symbol: 'diamond',
                        symbolSize: (secondBuyPoint.isDestroyed || thirdBuyPoint.isDestroyed) ? 10 : (isDailyBroken ? 12 : 16),
                        itemStyle: { color: getBuyColor({ isDestroyed: secondBuyPoint.isDestroyed || thirdBuyPoint.isDestroyed }, '#a855f7'), opacity: (secondBuyPoint.isDestroyed || thirdBuyPoint.isDestroyed) ? 0.5 : (isDailyBroken ? 0.6 : 1) },
                        label: {
                          show: true,
                          formatter: (secondBuyPoint.isDestroyed || thirdBuyPoint.isDestroyed) ? '二三买共振(已失效)' : (isDailyBroken ? '二三买共振(日线破位·放弃)' : (!isInAmbushZone ? '[高乖离]二三买共振·重仓80%' : '二三买共振·重仓80%')),
                          position: 'bottom',
                          distance: (secondBuyPoint.isDestroyed || thirdBuyPoint.isDestroyed) ? 12 : (isDailyBroken ? 14 : 18),
                          color: getBuyColor({ isDestroyed: secondBuyPoint.isDestroyed || thirdBuyPoint.isDestroyed }, '#a855f7'),
                          fontWeight: 'bold',
                          fontSize: (secondBuyPoint.isDestroyed || thirdBuyPoint.isDestroyed) ? 12 : (isDailyBroken ? 14 : 20),
                        },
                        z: 28,
                      },
                    ]
                  : []),
                // 第二类买点（二买）标记（未共振时独立显示；isDestroyed 优先降级）
                ...(secondBuyPoint?.hasSignal && !isBuy23Resonance
                  ? [
                      {
                        name: getBuyLabel(secondBuyPoint, '二买', '二买'),
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[secondBuyPoint.date, secondBuyPoint.price]] as [string, number][],
                        symbol: 'diamond',
                        symbolSize: secondBuyPoint.isDestroyed ? 9 : (isDailyBroken ? 10 : (!isInAmbushZone ? 11 : 12)),
                        itemStyle: { color: getBuyColor(secondBuyPoint, '#8b5cf6'), opacity: secondBuyPoint.isDestroyed ? 0.5 : (isDailyBroken ? 0.6 : 1) },
                        label: {
                          show: true,
                          formatter: secondBuyPoint.isDestroyed ? '二买(已失效)' : (isDailyBroken ? '二买(日线破位·放弃)' : (!isInAmbushZone ? '[高乖离]二买·确认50%' : '二买·确认50%')),
                          position: 'bottom',
                          distance: secondBuyPoint.isDestroyed ? 10 : (isDailyBroken ? 12 : 15),
                          color: getBuyColor(secondBuyPoint, '#8b5cf6'),
                          fontWeight: 'bold',
                          fontSize: secondBuyPoint.isDestroyed ? 12 : (isDailyBroken ? 14 : (!isInAmbushZone ? 16 : 20)),
                        },
                        z: 25,
                      },
                    ]
                  : []),
                // 一买失败标记
                ...(firstBuyFailed?.hasSignal
                  ? [
                      {
                        name: '一买失败',
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[firstBuyFailed.date, firstBuyFailed.price]] as [string, number][],
                        symbol: 'diamond',
                        symbolSize: 14,
                        itemStyle: { color: '#ef4444' }, // 红色
                        label: {
                          show: true,
                          formatter: '一买失败',
                          position: 'bottom',
                          distance: 18,
                          color: '#ef4444',
                          fontWeight: 'bold',
                          fontSize: 20,
                        },
                        z: 26,
                      },
                    ]
                  : []),
                // 二买失败标记
                ...(secondBuyFailed?.hasSignal
                  ? [
                      {
                        name: '二买失败',
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[secondBuyFailed.date, secondBuyFailed.price]] as [string, number][],
                        symbol: 'diamond',
                        symbolSize: 14,
                        itemStyle: { color: '#ef4444' }, // 红色
                        label: {
                          show: true,
                          formatter: '二买失败',
                          position: 'bottom',
                          distance: 18,
                          color: '#ef4444',
                          fontWeight: 'bold',
                          fontSize: 20,
                        },
                        z: 26,
                      },
                    ]
                  : []),
                // 三买失败标记
                ...(thirdBuyFailed?.hasSignal
                  ? [
                      {
                        name: '三买失败',
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[thirdBuyFailed.date, thirdBuyFailed.price]] as [string, number][],
                        symbol: 'diamond',
                        symbolSize: 14,
                        itemStyle: { color: '#ef4444' }, // 红色
                        label: {
                          show: true,
                          formatter: '三买失败·止损',
                          position: 'bottom',
                          distance: 18,
                          color: '#ef4444',
                          fontWeight: 'bold',
                          fontSize: 20,
                        },
                        z: 26,
                      },
                    ]
                  : []),
                // 第三类买点（三买）标记（未共振时独立显示；isDestroyed 优先降级）
                ...(thirdBuyPoint?.hasSignal && !isBuy23Resonance
                  ? [
                      {
                        name: getBuyLabel(thirdBuyPoint, '三买', '三买'),
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[thirdBuyPoint.date, thirdBuyPoint.price]] as [string, number][],
                        symbol: 'diamond',
                        symbolSize: thirdBuyPoint.isDestroyed ? 9 : (isDailyBroken ? 10 : (!isInAmbushZone ? 11 : 12)),
                        itemStyle: { color: getBuyColor(thirdBuyPoint, '#f97316'), opacity: thirdBuyPoint.isDestroyed ? 0.5 : (isDailyBroken ? 0.6 : 1) },
                        label: {
                          show: true,
                          formatter: thirdBuyPoint.isDestroyed ? '三买(已失效)' : (isDailyBroken ? '三买(日线破位·放弃)' : (!isInAmbushZone ? '[高乖离]三买·重仓80%' : '三买·重仓80%')),
                          position: 'bottom',
                          distance: thirdBuyPoint.isDestroyed ? 10 : (isDailyBroken ? 12 : 15),
                          color: getBuyColor(thirdBuyPoint, '#f97316'),
                          fontWeight: 'bold',
                          fontSize: thirdBuyPoint.isDestroyed ? 12 : (isDailyBroken ? 14 : (!isInAmbushZone ? 16 : 20)),
                        },
                        z: 25,
                      },
                    ]
                  : []),
                // 第一类卖点（一卖）标记
                ...(firstSellPoint?.hasSignal
                  ? (() => {
                      const tier = firstSellPoint.tier
                      const color = tier === 'weak' ? '#f59e0b' : tier === 'half' ? '#f97316' : '#ef4444'
                      const labelText = tier === 'weak' ? '弱卖' : tier === 'half' ? '半仓' : '清仓'
                      return [
                        {
                          name: '一卖',
                          type: 'scatter' as const,
                          xAxisIndex: 0,
                          yAxisIndex: 0,
                          data: [[firstSellPoint.date, firstSellPoint.price]] as [string, number][],
                          symbol: 'diamond',
                          symbolSize: 12,
                          itemStyle: { color },
                          label: {
                            show: true,
                            formatter: `一卖·${labelText}`,
                            position: 'top',
                            distance: 15,
                            color,
                            fontWeight: 'bold',
                            fontSize: 20,
                          },
                          z: 25,
                        },
                      ]
                    })()
                  : []),
                // 第二类卖点（二卖）标记
                ...(secondSellPoint?.hasSignal
                  ? (() => {
                      const tier = secondSellPoint.tier
                      const color = tier === 'weak' ? '#f59e0b' : tier === 'half' ? '#f97316' : '#ef4444'
                      const labelText = tier === 'weak' ? '弱卖' : tier === 'half' ? '半仓' : '清仓'
                      return [
                        {
                          name: '二卖',
                          type: 'scatter' as const,
                          xAxisIndex: 0,
                          yAxisIndex: 0,
                          data: [[secondSellPoint.date, secondSellPoint.price]] as [string, number][],
                          symbol: 'diamond',
                          symbolSize: 12,
                          itemStyle: { color },
                          label: {
                            show: true,
                            formatter: `二卖·${labelText}`,
                            position: 'top',
                            distance: 15,
                            color,
                            fontWeight: 'bold',
                            fontSize: 20,
                          },
                          z: 25,
                        },
                      ]
                    })()
                  : []),
                // 第三类卖点（三卖）标记
                ...(thirdSellPoint?.hasSignal
                  ? [
                      {
                        name: '三卖',
                        type: 'scatter' as const,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        data: [[thirdSellPoint.date, thirdSellPoint.price]] as [string, number][],
                        symbol: 'diamond',
                        symbolSize: 12,
                        itemStyle: { color: '#111827' }, // 黑色
                        label: {
                          show: true,
                          formatter: '三卖·清仓',
                          position: 'top',
                          distance: 15,
                          color: '#111827',
                          fontWeight: 'bold',
                          fontSize: 20,
                        },
                        z: 25,
                      },
                    ]
                  : []),
                {
                  name: 'MACD柱',
                  type: 'bar',
                  xAxisIndex: 1,
                  yAxisIndex: 1,
                  data: macdBarSeries,
                  barMaxWidth: 6,
                  z: 1,
                  itemStyle: {
                    color: (params: { value?: number | string }) => {
                      const v = params.value
                      const n = typeof v === 'number' ? v : Number(v)
                      if (!Number.isFinite(n)) return KLINE_UP_RED
                      return n >= 0 ? KLINE_UP_RED : KLINE_DOWN_GREEN
                    },
                    opacity: 0.85,
                  },
                  markLine: {
                    silent: true,
                    symbol: 'none',
                    data: [{ yAxis: 0 }],
                    lineStyle: { color: '#94a3b8', width: 2.5, opacity: 0.95 },
                    label: { show: false },
                  },
                },
                {
                  name: 'DIF',
                  type: 'line',
                  xAxisIndex: 1,
                  yAxisIndex: 1,
                  data: difSeries,
                  showSymbol: false,
                  z: 2,
                  lineStyle: { width: 1.2, color: '#fbbf24' },
                },
                {
                  name: 'DEA',
                  type: 'line',
                  xAxisIndex: 1,
                  yAxisIndex: 1,
                  data: deaSeries,
                  showSymbol: false,
                  z: 2,
                  lineStyle: { width: 1.2, color: '#e5e7eb' },
                },
              ],
            }}
            style={{
              width: '100%',
              height: 'clamp(460px, 56vh, 720px)',
            }}
          />
        </div>
        <aside className="central-compare-aside" aria-label={`${periodLabel}现价与日线中枢对比`}>
          <div className="central-compare-aside-title">实时对比</div>
          {indexKline.period === '15' ? (
            <>
              <div className="central-compare-price" style={{ marginTop: '8px' }}>
                <div style={{ fontSize: '13px', color: '#e2e8f0', marginBottom: '4px' }}>{seriesName}</div>
                现价 <strong>{lastClose.toFixed(3)}</strong>
                <span className="central-compare-time" style={{ marginLeft: '0.5rem', fontSize: '0.85em', color: '#94a3b8', fontWeight: 400 }}>
                  {lastDate}
                </span>
              </div>
              <div className="central-compare-row central-compare-row--spaced" style={{ marginTop: '12px' }}>
                <span className="central-compare-label">日线 C-ZD</span>
                <span className="central-compare-ref">
                  {dailyCZd != null ? dailyCZd.toFixed(2) : '—'}
                </span>
              </div>
              <div className="central-compare-metric">
                相对日线 C-ZD
                <span className="central-compare-pct">{pctVsRef(lastClose, dailyCZd)}%</span>
              </div>
              <div className="central-compare-row central-compare-row--spaced" style={{ marginTop: '8px' }}>
                <span className="central-compare-label">日线 A-ZD</span>
                <span className="central-compare-ref">
                  {dailyAZd != null ? dailyAZd.toFixed(2) : '—'}
                </span>
              </div>
              <div className="central-compare-metric">
                相对日线 A-ZD
                <span className="central-compare-pct">{pctVsRef(lastClose, dailyAZd)}%</span>
              </div>
            </>
          ) : (
            <>
              {holdingInfo && (
                <div className="holding-info-card" style={{
                  marginTop: '8px',
                  marginBottom: '10px',
                  padding: '8px 10px',
                  background: 'rgba(251,191,36,0.12)',
                  border: '1px solid rgba(251,191,36,0.35)',
                  borderRadius: '6px',
                  fontSize: '12px',
                }}>
                  <div style={{ color: '#fbbf24', fontWeight: 700, marginBottom: '4px' }}>
                    ★ 持仓：{holdingInfo.name}（{holdingInfo.code}）
                  </div>
                  {holdingInfo.cost != null && (
                    <div style={{ color: '#cbd5e1', lineHeight: '1.5' }}>
                      成本: {holdingInfo.cost.toFixed(2)}&nbsp;&nbsp;
                      <span style={{
                        color: lastClose >= holdingInfo.cost ? '#4ade80' : '#f87171',
                        fontWeight: 600,
                      }}>
                        {(((lastClose - holdingInfo.cost) / holdingInfo.cost) * 100).toFixed(2)}%
                      </span>
                    </div>
                  )}
                  {holdingInfo.shares != null && (
                    <div style={{ color: '#94a3b8', fontSize: '11px', marginTop: '2px' }}>
                      股数: {holdingInfo.shares.toLocaleString()}
                      {holdingInfo.cost != null && (
                        <>&nbsp;&nbsp;市值: {(lastClose * holdingInfo.shares).toFixed(0)}元</>
                      )}
                    </div>
                  )}
                  {holdingInfo.note && (
                    <div style={{ color: '#94a3b8', fontSize: '11px', marginTop: '2px' }}>
                      备注: {holdingInfo.note}
                    </div>
                  )}
                </div>
              )}
              <div className="central-compare-price">
                现价 <strong>{lastClose.toFixed(3)}</strong>
                <span className="central-compare-time" style={{ marginLeft: '0.5rem', fontSize: '0.85em', color: '#94a3b8', fontWeight: 400 }}>
                  {lastDate}
                </span>
              </div>
              <div className="central-compare-row">
                <span className="central-compare-label">当前笔（60min）</span>
                <span className="central-compare-ref">{penDirLabel}</span>
              </div>
              <div className="central-compare-row central-compare-row--spaced">
                <span className="central-compare-label">日线 C-ZD</span>
                <span className="central-compare-ref">
                  {dailyCZd != null ? dailyCZd.toFixed(2) : '—'}
                </span>
              </div>
              <div className="central-compare-metric">
                相对日线 C-ZD
                <span className="central-compare-pct">{pctVsRef(lastClose, dailyCZd)}%</span>
              </div>
              <div className="central-compare-row central-compare-row--spaced">
                <span className="central-compare-label">日线 A-ZD</span>
                <span className="central-compare-ref">
                  {dailyAZd != null ? dailyAZd.toFixed(2) : '—'}
                </span>
              </div>
              <div className="central-compare-metric">
                相对日线 A-ZD
                <span className="central-compare-pct">{pctVsRef(lastClose, dailyAZd)}%</span>
              </div>
              {buyConditionChecklist && buyConditionChecklist.length > 0 && (
                <div className="buy-checklist" aria-label="买条件自检">
                  <div className="buy-checklist-title">「买」条件自检（须全部满足）</div>
                  {sellSignalActive && (
                    <p className="buy-checklist-note">
                      当前已触发卖条件，不会显示「买」；下列仅表示各项是否单独成立。
                    </p>
                  )}
                  <ul className="buy-checklist-ul">
                    {buyConditionChecklist.map((row) => (
                      <li key={row.label} className="buy-checklist-li">
                        <span className={row.ok ? 'buy-checklist-yes' : 'buy-checklist-no'}>
                          {row.ok ? '✓' : '✗'}
                        </span>
                        <span className="buy-checklist-text">{row.label}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {divergenceArrows.length > 0 && (
                <p className="central-compare-muted" style={{ marginTop: '0.5rem' }}>
                  图中亮黄色方块：相邻向下笔创新低且 MACD 绿柱面积缩小（或笔长更短）的底背驰位置
                </p>
              )}
            </>
          )}
        </aside>
      </div>
    </div>
  )
}
