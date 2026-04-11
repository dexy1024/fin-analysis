import type { IndexKlineResponse } from './api/stock'

/** 主图 ECharts 用：BOLL(20,2) 下轨、上下轨间距（堆叠）、中轨；无数据时用 '-' */
export function buildBollLineData(data: IndexKlineResponse['data']) {
  const lower = data.map((p) => {
    const v = p.boll?.lower
    return v != null && Number.isFinite(v) ? v : '-'
  })
  const bandWidth = data.map((p) => {
    const u = p.boll?.upper
    const l = p.boll?.lower
    return u != null && l != null && Number.isFinite(u) && Number.isFinite(l) ? u - l : '-'
  })
  const middle = data.map((p) => {
    const v = p.boll?.middle
    return v != null && Number.isFinite(v) ? v : '-'
  })
  const hasAny = data.some((p) => p.boll?.middle != null)
  return { lower, bandWidth, middle, hasAny }
}

/** 参与主图 y 轴极值：BOLL 上/下轨 */
export function bollExtentPrices(data: IndexKlineResponse['data']): number[] {
  const out: number[] = []
  for (const p of data) {
    const b = p.boll
    if (!b) continue
    if (b.upper != null && Number.isFinite(b.upper)) out.push(b.upper)
    if (b.lower != null && Number.isFinite(b.lower)) out.push(b.lower)
    if (b.middle != null && Number.isFinite(b.middle)) out.push(b.middle)
  }
  return out
}
