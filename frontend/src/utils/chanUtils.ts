import type { IndexKlineResponse } from '../api/stock'

/** 与 DailyChanChart 一致：按中枢起始日（再按结束日）排序，首段为 A、末段为 C */
export function sortCentralsChronologically(
  raw: NonNullable<IndexKlineResponse['centrals']>,
): NonNullable<IndexKlineResponse['centrals']> {
  return [...raw].sort((a, b) => {
    const byStart = a.start_date.localeCompare(b.start_date)
    if (byStart !== 0) return byStart
    return a.end_date.localeCompare(b.end_date)
  })
}
