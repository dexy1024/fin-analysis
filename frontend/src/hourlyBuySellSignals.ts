/**
 * 60m 图「买/卖」条件与右侧面板自检：纯函数，仅依赖 get_index_kline 响应 + 日线 A/C-ZD。
 * 告警哨兵、后端镜像逻辑等可复用 {@link computeHourlyBuySellState}，无需重算 K 线合并/分型。
 */
import type { IndexKlinePoint, IndexKlineResponse, IndexPen } from './api/stock'
import { divergenceArrowPointsFromDownPens } from './chartMacd'

export type HourlyBuyConditionRow = { label: string; ok: boolean }

export type HourlySignalMarker = {
  text: string
  date: string
  y: number
  color: string
  reasons: string[]
}

/** 与自检面板 7 条一一对应的布尔量 */
export type HourlyBuyConditionFlags = {
  keepDailySupport: boolean
  inCCentral: boolean
  switchedDownToUp: boolean
  hasBottomFractalInSwitch: boolean
  hasBottomDivInSwitch: boolean
  macdBuy: boolean
  bollBuy: boolean
}

export type HourlyBuySellState = {
  signalMarker: HourlySignalMarker | null
  buyConditionChecklist: HourlyBuyConditionRow[] | null
  sellSignalActive: boolean
  /** 原始条件，供哨兵订阅 */
  flags: HourlyBuyConditionFlags
  buySignal: boolean
  sellSignal: boolean
}

function sortCentralsForHourly(
  raw: NonNullable<IndexKlineResponse['centrals']>,
): NonNullable<IndexKlineResponse['centrals']> {
  return [...raw].sort((a, b) => {
    const byStart = a.start_date.localeCompare(b.start_date)
    if (byStart !== 0) return byStart
    return a.end_date.localeCompare(b.end_date)
  })
}

function buildDateToIdx(data: IndexKlinePoint[]): Map<string, number> {
  return new Map(data.map((p, i) => [p.date, i] as const))
}

/** 顶背驰：相邻向上笔创新高且红柱面积缩小（与 HourlyChanChart 原逻辑一致） */
function buildTopDivergenceSquares(data: IndexKlinePoint[], pensEff: IndexPen[]): [string, number][] {
  const dateToIdx = buildDateToIdx(data)
  const up = pensEff.filter((p) => p.direction === 'up')
  if (up.length < 2) return []
  const res: [string, number][] = []
  const areaOf = (pen: IndexPen) => {
    const sIdx = dateToIdx.get(pen.start_date)
    const eIdx = dateToIdx.get(pen.end_date)
    if (sIdx == null || eIdx == null || sIdx > eIdx) return null
    let area = 0
    for (const b of data.slice(sIdx, eIdx + 1)) {
      const m = b.macd?.macd
      if (m != null && Number.isFinite(m) && m > 0) area += Math.abs(m)
    }
    return area
  }
  for (let i = 1; i < up.length; i += 1) {
    const prev = up[i - 1]
    const cur = up[i]
    const prevArea = areaOf(prev)
    const curArea = areaOf(cur)
    if (prevArea == null || curArea == null) continue
    if (cur.end_price > prev.end_price && curArea < prevArea) {
      res.push([cur.end_date, cur.end_price])
    }
  }
  return res
}

const BUY_CHECKLIST_LABELS: [
  keyof HourlyBuyConditionFlags,
  string,
][] = [
  ['keepDailySupport', '【日线】未跌破 C-ZD 与 A-ZD'],
  ['inCCentral', '【60m】现价在 C 中枢内（ZD～ZG）'],
  ['switchedDownToUp', '【60m】有效笔：前一下笔、当前上笔'],
  ['hasBottomFractalInSwitch', '【60m】当前向上笔内有底分型'],
  ['hasBottomDivInSwitch', '【60m】底背驰点落在当前向上笔内'],
  ['macdBuy', '【60m】MACD 转强'],
  ['bollBuy', '【60m】BOLL 站回中轨'],
]

export function computeHourlyBuySellState(
  indexKline: IndexKlineResponse,
  dailyAZd: number | null,
  dailyCZd: number | null,
): HourlyBuySellState {
  const emptyFlags: HourlyBuyConditionFlags = {
    keepDailySupport: false,
    inCCentral: false,
    switchedDownToUp: false,
    hasBottomFractalInSwitch: false,
    hasBottomDivInSwitch: false,
    macdBuy: false,
    bollBuy: false,
  }

  if (indexKline.data.length < 3) {
    return {
      signalMarker: null,
      buyConditionChecklist: null,
      sellSignalActive: false,
      flags: emptyFlags,
      buySignal: false,
      sellSignal: false,
    }
  }

  const data = indexKline.data
  const last = data[data.length - 1]
  const prev = data[data.length - 2]
  const prev2 = data[data.length - 3]
  const m0 = last.macd?.macd
  const m1 = prev.macd?.macd
  const m2 = prev2.macd?.macd
  const dif0 = last.macd?.dif
  const dif1 = prev.macd?.dif
  const dea0 = last.macd?.dea
  const dea1 = prev.macd?.dea
  const b0 = last.boll
  const b1 = prev.boll

  const centralsRaw = indexKline.centrals ?? []
  const centrals = sortCentralsForHourly(centralsRaw)
  const cCentralIdx = centrals.length > 0 ? centrals.length - 1 : -1
  const c = cCentralIdx >= 0 ? centrals[cCentralIdx] : null
  const cZd = c ? Number(c.zd) : null
  const cZg = c ? Number(c.zg) : null
  const inCCentral =
    cZd != null && cZg != null && Number.isFinite(cZd) && Number.isFinite(cZg)
      ? last.close >= cZd && last.close <= cZg
      : false
  const keepDailySupport =
    dailyCZd != null && dailyAZd != null ? last.close >= dailyCZd && last.close >= dailyAZd : false

  const pensEff = indexKline.pens_effective ?? []
  const divergenceArrows = divergenceArrowPointsFromDownPens(data, pensEff)
  const topDivergenceSquares = buildTopDivergenceSquares(data, pensEff)

  const switchedDownToUp =
    pensEff.length >= 2 &&
    pensEff[pensEff.length - 2].direction === 'down' &&
    pensEff[pensEff.length - 1].direction === 'up'
  const switchedUpToDown =
    pensEff.length >= 2 &&
    pensEff[pensEff.length - 2].direction === 'up' &&
    pensEff[pensEff.length - 1].direction === 'down'
  const lastUpPen = switchedDownToUp ? pensEff[pensEff.length - 1] : null
  const hasBottomFractalInSwitch =
    lastUpPen != null &&
    (indexKline.fractals ?? []).some(
      (f) => f.type === 'bottom' && f.date >= lastUpPen.start_date && f.date <= lastUpPen.end_date,
    )
  const hasBottomDivInSwitch =
    lastUpPen != null &&
    divergenceArrows.some((pt) => pt[0] >= lastUpPen.start_date && pt[0] <= lastUpPen.end_date)
  const hasTopFractal = (indexKline.fractals ?? []).some((f) => f.type === 'top' && f.date === last.date)
  const hasTopDivNow = topDivergenceSquares.some((pt) => pt[0] === last.date)

  const macdBuy =
    m0 != null &&
    m1 != null &&
    m2 != null &&
    dif0 != null &&
    dif1 != null &&
    dea0 != null &&
    dea1 != null &&
    m0 < 0 &&
    Math.abs(m0) < Math.abs(m1) &&
    (dif0 > dif1 || (dif1 <= dea1 && dif0 > dea0)) &&
    !(m0 < 0 && m1 < 0 && m2 < 0 && Math.abs(m0) > Math.abs(m1) && Math.abs(m1) > Math.abs(m2))

  const bollBuy =
    b0?.middle != null &&
    b1?.middle != null &&
    b0?.lower != null &&
    b1?.lower != null &&
    last.close > b0.middle &&
    (prev.close <= b1.middle || (prev.low <= b1.lower * 1.01 && last.close > b0.middle))

  const flags: HourlyBuyConditionFlags = {
    keepDailySupport,
    inCCentral,
    switchedDownToUp,
    hasBottomFractalInSwitch,
    hasBottomDivInSwitch,
    macdBuy,
    bollBuy,
  }

  const buyReasons: string[] = []
  if (keepDailySupport) buyReasons.push('【日线】未跌破 C-ZD 与 A-ZD')
  if (inCCentral) buyReasons.push('【60m】现价在 C 中枢内（ZD～ZG）')
  if (switchedDownToUp) buyReasons.push('【60m】有效笔：前一下笔、当前上笔')
  if (hasBottomFractalInSwitch) buyReasons.push('【60m】当前向上笔内有底分型')
  if (hasBottomDivInSwitch) buyReasons.push('【60m】底背驰点落在当前向上笔内')
  if (macdBuy) buyReasons.push('【60m】MACD 转强')
  if (bollBuy) buyReasons.push('【60m】BOLL 站回中轨')
  const buySignal =
    inCCentral &&
    keepDailySupport &&
    hasBottomFractalInSwitch &&
    hasBottomDivInSwitch &&
    switchedDownToUp &&
    macdBuy &&
    bollBuy

  const dailyBreak = (dailyCZd != null && last.close < dailyCZd) || (dailyAZd != null && last.close < dailyAZd)
  const macdSell =
    m0 != null &&
    m1 != null &&
    dif0 != null &&
    dif1 != null &&
    dea0 != null &&
    dea1 != null &&
    ((m1 > 0 && m0 > 0 && m0 < m1) || (dif1 >= dea1 && dif0 < dea0) || m0 < 0)
  const bollSellMiddle =
    b0?.middle != null &&
    b1?.middle != null &&
    last.close < b0.middle &&
    prev.close < b1.middle

  const bollSellLower =
    b0?.lower != null &&
    b1?.lower != null &&
    last.close < b0.lower &&
    prev.close < b1.lower

  const bollSell = bollSellMiddle || bollSellLower
  const sellReasons: string[] = []
  if (dailyBreak) sellReasons.push('日线跌破C-ZD/A-ZD')
  if (switchedUpToDown) sellReasons.push('向上笔转向下笔')
  if (hasTopFractal && hasTopDivNow) sellReasons.push('顶分型+顶背驰')
  if (macdSell) sellReasons.push('MACD转弱')
  if (bollSellMiddle) sellReasons.push('60分钟 BOLL 跌破中轨')
  if (bollSellLower) sellReasons.push('60分钟 BOLL 跌破下轨')
  const sellSignal = dailyBreak || switchedUpToDown || (hasTopFractal && hasTopDivNow) || macdSell || bollSell

  const buyConditionChecklist: HourlyBuyConditionRow[] = BUY_CHECKLIST_LABELS.map(([key, label]) => ({
    label,
    ok: flags[key],
  }))

  if (sellSignal) {
    return {
      signalMarker: {
        text: '卖',
        date: last.date,
        y: last.high * 1.01,
        color: '#ef4444',
        reasons: sellReasons,
      },
      buyConditionChecklist,
      sellSignalActive: true,
      flags,
      buySignal,
      sellSignal,
    }
  }
  if (buySignal) {
    return {
      signalMarker: {
        text: '买',
        date: last.date,
        y: last.low * 0.99,
        color: '#22c55e',
        reasons: buyReasons,
      },
      buyConditionChecklist,
      sellSignalActive: false,
      flags,
      buySignal,
      sellSignal,
    }
  }
  return {
    signalMarker: null,
    buyConditionChecklist,
    sellSignalActive: false,
    flags,
    buySignal,
    sellSignal,
  }
}
