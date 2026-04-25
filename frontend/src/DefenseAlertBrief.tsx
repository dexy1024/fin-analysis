/**
 * 与后端 defense_radar._classify 一致：用日线 C-ZD / A-ZD 与现价 P 划分双防线伏击带。
 */

export type DefenseAlertKind =
  | 'level1'      // 绝对防线 ±1% 缓冲带
  | 'red'         // 跌破绝对防线
  | 'safe_above'  // 高于缓冲带
  | 'unknown'

export function classifyDefenseAlert(
  p: number,
  cZd: number | null,
  aZd: number | null,
): DefenseAlertKind {
  if (cZd == null || aZd == null || !Number.isFinite(p)) return 'unknown'
  const vc = Number(cZd)
  const va = Number(aZd)
  if (!Number.isFinite(vc) || !Number.isFinite(va)) return 'unknown'
  const absoluteBottom = Math.min(vc, va)  // 绝对防线：较低者
  const bufferUpper = absoluteBottom * 1.01
  const bufferLower = absoluteBottom * 0.99
  if (p >= bufferLower && p <= bufferUpper) return 'level1'
  if (p < absoluteBottom) return 'red'
  return 'safe_above'
}

export function DefenseAlertBrief({
  price,
  cZd,
  aZd,
}: {
  price: number
  cZd: number | null
  aZd: number | null
}) {
  // 核心伏击圈基准线：取 C-ZD 和 A-ZD 中较大的一个
  const baseZd =
    cZd != null && aZd != null
      ? Math.max(cZd, aZd)
      : cZd != null
        ? cZd
        : aZd != null
          ? aZd
          : null

  if (baseZd == null || !Number.isFinite(price)) {
    return (
      <div className="defense-alert-panel defense-alert-panel--unknown" role="status">
        <div className="defense-alert-panel-heading">核心伏击圈</div>
        <p className="defense-alert-panel-line">—</p>
      </div>
    )
  }

  const lowerBound = Math.min(cZd ?? Infinity, aZd ?? Infinity)
  const upperBound = baseZd * 1.03
  const isInSafeZone = price >= lowerBound && price <= upperBound

  return (
    <div
      className={`defense-alert-panel defense-alert-panel--${isInSafeZone ? 'level1' : 'safe_above'}`}
      role="status"
    >
      <div className="defense-alert-panel-heading">核心伏击圈</div>
      <p
        className="defense-alert-panel-line"
        style={{
          fontSize: '1.5rem',
          fontWeight: 700,
          color: isInSafeZone ? '#22c55e' : '#94a3b8',
          margin: '0.5rem 0',
        }}
      >
        {isInSafeZone ? '是' : '否'}
      </p>
      <p
        className="defense-alert-panel-meaning"
        style={{ fontSize: '0.8rem', color: '#94a3b8', lineHeight: 1.4 }}
      >
        区间 [{lowerBound.toFixed(3)}, {upperBound.toFixed(3)}]
        <br />
        现价 {price.toFixed(3)}
      </p>
    </div>
  )
}
