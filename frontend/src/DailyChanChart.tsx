import ReactECharts from 'echarts-for-react'
import type { IndexKlinePoint, IndexKlineResponse, IndexPen } from './api/stock'
import { buildBollLineData, bollExtentPrices } from './bollSeries'
import {
  appendMacdTooltipBlock,
  divergenceArrowPointsFromDownPens,
  KLINE_DOWN_GREEN,
  KLINE_UP_RED,
} from './chartMacd'
import { DefenseAlertBrief, type DefenseAlertKind } from './DefenseAlertBrief'
import {
  formatMacdYAxisLabel,
  formatPriceYAxisLabel,
  mainChartYExtent,
} from './priceAxisExtent'

/** 与后端 _segment_polyline_points 一致：沿有效笔端点转折 */
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

/** 现价相对参考价的偏离%：(close-ref)/ref*100 */
function pctVsRef(close: number, ref: number | null | undefined): string {
  if (ref == null || !Number.isFinite(ref) || ref === 0) return '—'
  return (((close - ref) / ref) * 100).toFixed(2)
}

function normAxisDate(s: string): string {
  const t = String(s).trim()
  return t.length >= 10 ? t.slice(0, 10) : t
}

/** 悬停日期是否落在 [start_date, end_date]（含端点） */
function dateInCentralRange(axisDate: string, start: string, end: string): boolean {
  const d = normAxisDate(axisDate)
  return d >= normAxisDate(start) && d <= normAxisDate(end)
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

function buildAxisTooltipHtml(
  params: unknown,
  candleSeriesName: string,
  centralTips: CentralTipEntry[],
  klineRows?: IndexKlinePoint[],
): string {
  if (!Array.isArray(params) || params.length === 0) return ''
  const first = params[0] as { axisValue?: string; axisValueLabel?: string }
  const axisRaw = first.axisValueLabel ?? first.axisValue ?? ''
  const dateLine = normAxisDate(String(axisRaw))

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
      // category 轴时 ECharts 会在数据前插入该点下标，raw 为 [index, open, close, low, high]
      const raw = p.value as number[]
      const ohlc =
        raw.length >= 5 ? raw.slice(-4) : raw.length === 4 ? raw : null
      if (!ohlc) break
      const [open, close, low, high] = ohlc
      lines.push(
        `<div style="color:#cbd5e1;font-size:12px">开 ${open.toFixed(3)}　收 ${close.toFixed(3)}</div>`,
      )
      lines.push(
        `<div style="color:#cbd5e1;font-size:12px">低 ${low.toFixed(3)}　高 ${high.toFixed(3)}</div>`,
      )
      if (raw.length >= 5 && klineRows?.length) {
        const idx = Number(raw[0])
        const pt = Number.isFinite(idx) ? klineRows[Math.floor(idx)] : undefined
        const b = pt?.boll
        if (
          b?.upper != null &&
          b?.middle != null &&
          b?.lower != null &&
          [b.upper, b.middle, b.lower].every((x) => Number.isFinite(x))
        ) {
          lines.push(
            `<div style="color:#93c5fd;font-size:11px;margin-top:4px">BOLL(20,2) 上 ${b.upper.toFixed(3)}　中 ${b.middle.toFixed(3)}　下 ${b.lower.toFixed(3)}</div>`,
          )
        }
      }
      break
    }
  }

  for (const c of centralTips) {
    if (!dateInCentralRange(dateLine, c.start_date, c.end_date)) continue
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

function buildDailyTooltip(
  params: unknown,
  candleSeriesName: string,
  centralTips: CentralTipEntry[],
  klineRows?: IndexKlinePoint[],
): string {
  return (
    buildAxisTooltipHtml(params, candleSeriesName, centralTips, klineRows) +
    appendMacdTooltipBlock(params)
  )
}

export function DailyChanChart({
  data: indexKline,
  seriesName,
  indexAlertKind,
  isIndexSelf = false,
  radarSummaryAlert,
  radarSummaryGeneratedAt,
}: {
  data: IndexKlineResponse
  seriesName: string
  /** 上证指数日线双防线档位（由 App 传入） */
  indexAlertKind?: DefenseAlertKind | null
  /** 当前图为上证指数 */
  isIndexSelf?: boolean
  /** 与 last_summary.json / 雷达 md 同步的预警原文（刷新后由 GET summary 注入） */
  radarSummaryAlert?: string | null
  /** 摘要 generated_at，与 json 一致 */
  radarSummaryGeneratedAt?: string | null
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
  /** 时间轴最右侧的中枢视为「当前 C 中枢」（仅该中枢 markArea 绿/红填充；ZG/ZD/DD 线与 A/B 一致为全屏 ZG/ZD + 框内 DD） */
  const cCentralIdx = centrals.length > 0 ? centrals.length - 1 : -1

  const centralLegendName = (i: number) => {
    if (centrals.length === 1) return 'C中枢'
    if (i === cCentralIdx) return 'C中枢'
    return CENTRAL_LABELS[i] ?? `中枢${i + 1}`
  }

  const aZd = centrals.length > 0 ? Number(centrals[0].zd) : null
  const cZd = centrals.length > 0 ? Number(centrals[cCentralIdx].zd) : null
  const breakBelowAZd = aZd != null && lastClose < aZd

  const priceYExtent = mainChartYExtent(indexKline.data, [
    ...centrals.flatMap((c) => [Number(c.zd), Number(c.zg)]),
    ...bollExtentPrices(indexKline.data),
  ])

  const bollData = buildBollLineData(indexKline.data)

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

  const centralMarkLineData: unknown[] = []
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
  const pensEff = indexKline.pens_effective ?? []
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

  const legendBase = [
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
  ]

  return (
    <div
      className={`daily-chart-shell${breakBelowAZd ? ' daily-chart-shell--below-a-zd' : ''}`}
    >
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
                formatter: (params: unknown) =>
                  buildDailyTooltip(params, seriesName, centralTips, indexKline.data),
              },
              legend: {
                type: 'scroll',
                top: 6,
                left: 'center',
                width: '92%',
                data: legendBase,
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
                  axisLabel: { color: '#9ca3af', fontSize: 10 },
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
                // 勿写死 start/end：否则每次 option 更新（定时拉 K 线）会重置缩放，滑块拖不动
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
                          const isC = i === cCentralIdx
                          const zdN = Number(c.zd)
                          const pot = Boolean(c.potential_divergence)
                          let itemStyle: {
                            color: string
                            borderColor: string
                            borderWidth: number
                          } = isC
                            ? lastClose < zdN
                              ? {
                                  color: 'rgba(239, 68, 68, 0.16)',
                                  borderColor: 'rgba(248, 113, 113, 0.42)',
                                  borderWidth: 1,
                                }
                              : {
                                  color: 'rgba(34, 197, 94, 0.16)',
                                  borderColor: 'rgba(74, 222, 128, 0.48)',
                                  borderWidth: 1,
                                }
                            : {
                                color: 'rgba(249, 115, 22, 0.1)',
                                borderColor: 'rgba(234, 88, 12, 0.28)',
                                borderWidth: 0.5,
                              }
                          if (pot) {
                            itemStyle = {
                              ...itemStyle,
                              borderColor: 'rgba(251, 191, 36, 0.92)',
                              borderWidth: Math.max(itemStyle.borderWidth, 2.5),
                            }
                          }
                          return [
                            {
                              name: pot
                                ? `${centralLegendName(i)} · 潜在背驰`
                                : centralLegendName(i),
                              xAxis: c.start_date,
                              yAxis: c.zg,
                              itemStyle,
                            },
                            {
                              xAxis: c.end_date,
                              yAxis: c.zd,
                              itemStyle,
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
              height: 'clamp(520px, 68vh, 960px)',
            }}
          />
        </div>
        <aside className="central-compare-aside" aria-label="现价与中枢下沿对比">
          <DefenseAlertBrief
            price={lastClose}
            cZd={cZd}
            aZd={aZd}
            indexAlertKind={indexAlertKind}
            isIndexSelf={isIndexSelf}
          />
          {radarSummaryAlert ? (
            <div className="defense-radar-sync-block">
              {radarSummaryGeneratedAt ? (
                <p className="defense-radar-sync-time" title={radarSummaryGeneratedAt}>
                  雷达摘要时间 {radarSummaryGeneratedAt}
                </p>
              ) : null}
              <p className="defense-radar-sync-body">{radarSummaryAlert}</p>
            </div>
          ) : null}
          <div className="central-compare-aside-title">实时对比</div>
          <div className="central-compare-price">
            现价 <strong>{lastClose.toFixed(3)}</strong>
            <span className="central-compare-time" style={{ marginLeft: '0.5rem', fontSize: '0.85em', color: '#94a3b8', fontWeight: 400 }}>
              {lastDate}
            </span>
          </div>
          {centrals.length === 0 ? (
            <p className="central-compare-muted">暂无中枢数据</p>
          ) : (
            <>
              <div className="central-compare-row">
                <span className="central-compare-label">C-ZD</span>
                <span className="central-compare-ref">
                  {cZd != null ? cZd.toFixed(2) : '—'}
                </span>
              </div>
              <div className="central-compare-metric">
                压力（相对 C-ZD）
                <span className="central-compare-pct">
                  {pctVsRef(lastClose, cZd)}%
                </span>
              </div>
              <div className="central-compare-row central-compare-row--spaced">
                <span className="central-compare-label">A-ZD</span>
                <span className="central-compare-ref">
                  {aZd != null ? aZd.toFixed(2) : '—'}
                </span>
              </div>
              <div className="central-compare-metric">
                支撑（相对 A-ZD）
                <span className="central-compare-pct">
                  {pctVsRef(lastClose, aZd)}%
                </span>
              </div>
              {breakBelowAZd && (
                <p className="central-compare-warn">已跌破 A-ZD，全图预警中</p>
              )}
            </>
          )}
        </aside>
      </div>
    </div>
  )
}
