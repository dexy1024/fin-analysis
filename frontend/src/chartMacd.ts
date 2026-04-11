import type { IndexKlinePoint, IndexPen } from './api/stock'

/** 与 K 线 itemStyle 涨跌色一致，用于 MACD 柱 */
export const KLINE_UP_RED = '#ef4444'
export const KLINE_DOWN_GREEN = '#22c55e'

export function macdNegArea(data: IndexKlinePoint[], d0: string, d1: string): number {
  let s = 0
  for (const row of data) {
    if (row.date < d0) continue
    if (row.date > d1) break
    const m = row.macd?.macd
    if (m != null && m < 0) s += Math.abs(m)
  }
  return s
}

/**
 * 相邻两根向下笔：终点创新低，且绿柱面积缩小或笔长度更短 → 在终点 K 线处标底背驰（方形标记）。
 */
export function divergenceArrowPointsFromDownPens(
  data: IndexKlinePoint[],
  pensEff: IndexPen[],
): [string, number][] {
  const downs = pensEff.filter((p) => p.direction === 'down')
  const out: [string, number][] = []
  for (let i = 1; i < downs.length; i++) {
    const prev = downs[i - 1]
    const last = downs[i]
    if (last.end_price >= prev.end_price) continue
    const areaPrev = macdNegArea(data, prev.start_date, prev.end_date)
    const areaLast = macdNegArea(data, last.start_date, last.end_date)
    const lenPrev = Math.abs(prev.end_price - prev.start_price)
    const lenLast = Math.abs(last.end_price - last.start_price)
    const weaker =
      lenLast < lenPrev || (areaPrev > 1e-8 && areaLast < areaPrev)
    if (!weaker) continue
    const row = data.find((r) => r.date === last.end_date)
    const y = row ? row.low : last.end_price
    out.push([last.end_date, y])
  }
  return out
}

export function appendMacdTooltipBlock(params: unknown): string {
  if (!Array.isArray(params)) return ''
  let dif: number | undefined
  let dea: number | undefined
  let macd: number | undefined
  for (const raw of params) {
    const p = raw as { seriesName?: string; value?: unknown }
    const val = p.value
    const n = typeof val === 'number' ? val : null
    if (n == null || Number.isNaN(n)) continue
    if (p.seriesName === 'DIF') dif = n
    if (p.seriesName === 'DEA') dea = n
    if (p.seriesName === 'MACD柱') macd = n
  }
  if (dif == null && dea == null && macd == null) return ''
  const fmt = (x: number | undefined) =>
    x != null && Number.isFinite(x) ? x.toFixed(4) : '—'
  return (
    `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(148,163,184,0.35)">` +
    `<div style="color:#94a3b8;font-size:11px;margin-bottom:4px">MACD(12,26,9)</div>` +
    `<div style="color:#fbbf24;font-size:12px">DIF ${fmt(dif)}</div>` +
    `<div style="color:#e5e7eb;font-size:12px">DEA ${fmt(dea)}</div>` +
    `<div style="color:#cbd5e1;font-size:12px">MACD ${fmt(macd)}</div>` +
    `</div>`
  )
}
