/**
 * 日期工具函数
 * 所有日期计算统一使用东八区（Asia/Shanghai），与后端保持一致
 */

/** 返回 N 天前的日期字符串（YYYY-MM-DD），基于东八区 */
export function startDateDaysAgo(days: number): string {
  // 使用 Intl.DateTimeFormat 确保东八区计算
  const now = new Date()
  const shanghaiNow = new Date(
    now.toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }),
  )
  shanghaiNow.setDate(shanghaiNow.getDate() - days)
  const year = shanghaiNow.getFullYear()
  const month = String(shanghaiNow.getMonth() + 1).padStart(2, '0')
  const day = String(shanghaiNow.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}
