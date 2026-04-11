import type { IndexKlinePoint } from './api/stock'

/**
 * 主图价格轴范围：仅用 K 线 OHLC + 可选价位（中枢 ZD/ZG、日线参考线等），
 * 不把笔/线段/分型纳入极值，避免个别异常点或辅助线把纵轴拉得过宽。
 */
export function mainChartYExtent(
  rows: ReadonlyArray<IndexKlinePoint>,
  extraPrices: ReadonlyArray<number | null | undefined>,
  padRatio = 0.06,
): { min: number; max: number } {
  let lo = Infinity
  let hi = -Infinity
  for (const p of rows) {
    const { open, high, low, close } = p
    if (![open, high, low, close].every((v) => typeof v === 'number' && Number.isFinite(v))) {
      continue
    }
    lo = Math.min(lo, open, high, low, close)
    hi = Math.max(hi, open, high, low, close)
  }
  for (const x of extraPrices) {
    if (x != null && typeof x === 'number' && Number.isFinite(x)) {
      lo = Math.min(lo, x)
      hi = Math.max(hi, x)
    }
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
    return { min: 0, max: 1 }
  }
  if (lo === hi) {
    const c = lo || 1
    const p = Math.abs(c) * 0.02 || 0.01
    return { min: c - p, max: c + p }
  }
  const span = hi - lo
  const pad = span * padRatio
  return { min: lo - pad, max: hi + pad }
}

/** 主图价格轴刻度：固定 1 位小数，避免浮点长串挤占左侧 */
export function formatPriceYAxisLabel(value: number | string): string {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n.toFixed(1) : String(value)
}

/** MACD 子图纵轴：短格式，避免过长浮点 */
export function formatMacdYAxisLabel(value: number | string): string {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n.toFixed(3) : String(value)
}
