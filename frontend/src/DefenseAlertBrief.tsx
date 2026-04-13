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

const BRIEF: Record<
  Exclude<DefenseAlertKind, 'unknown'>,
  { title: string; meaning: string; action: string }
> = {
  level1: {
    title: '【一级警报】绝对防线伏击圈',
    meaning:
      '现价落在绝对防线 MIN(C-ZD, A-ZD) ±1% 缓冲带内：进入伏击区，等待买点确认。',
    action:
      '状态：一级战备。打开本标的 60 分钟图，盯最右侧是否出现蓝三角（底分型）；出现后再按你的仓位纪律执行。下单前务必对照上证指数日线双防线档位（见下方「大盘」）。',
  },
  red: {
    title: '【红色警报】跌破绝对防线',
    meaning:
      '现价已跌破绝对防线 MIN(C-ZD, A-ZD)：中枢破坏逻辑，原上涨结构失效，易进入单边下跌。',
    action:
      '状态：禁买。将该标的移出狙击池/拉黑，保护本金，不参与盘中诱多。若大盘仍强仅个股破位，更说明该股弱势，勿抄底。',
  },
  safe_above: {
    title: '未入伏击区',
    meaning: '现价高于绝对防线 MIN(C-ZD, A-ZD) 的 +1% 缓冲带，尚未进入伏击圈。',
    action:
      '双防线狙击节奏未触发；可继续用其他规则观察。若大盘已进入伏击圈而本标的仍在上方，属分化，轻仓或观望。',
  },
}

/** 上证日线档位一句话（与 classifyDefenseAlert 一致） */
const INDEX_TIER_LINE: Record<Exclude<DefenseAlertKind, 'unknown'>, string> = {
  level1: '上证日线：绝对防线 ±1% 伏击圈（一级档）',
  red: '上证日线：跌破绝对防线（破位档）',
  safe_above: '上证日线：未入伏击区（偏高观望）',
}

function marketSyncHint(self: DefenseAlertKind, idx: DefenseAlertKind): string {
  if (self === 'unknown') return ''
  if (self === 'red') {
    return '（个股已红档禁买；与大盘是否同档不改变禁买。）'
  }
  if (idx === 'red') {
    return '大盘已红档走弱，个股信号一律降权，不宜重仓逆势赌反弹。'
  }
  if (self === 'level1' && idx === 'level1') {
    return '与大盘同处一级伏击带，共振时更宜重视 60 分钟买点。'
  }
  if (self === 'level1' && idx === 'safe_above') {
    return '大盘仍偏高、个股已入一级伏击，分化行情，控制仓位、择优。'
  }
  if (idx === 'safe_above' && self === 'level1') {
    return '大盘未踩伏击带而个股已入伏击圈，注意强弱背离，宁可等大盘靠拢或个股信号极强。'
  }
  return '档位不一致时，以更安全的一侧为锚；宁可错过，避免逆势重仓。'
}

export function DefenseAlertBrief({
  price,
  cZd,
  aZd,
  /** 上证指数日线双防线档位；未传表示上证 K 线尚未加载 */
  indexAlertKind,
  /** 当前图为上证指数时，不重复展示「大盘」块 */
  isIndexSelf = false,
}: {
  price: number
  cZd: number | null
  aZd: number | null
  indexAlertKind?: DefenseAlertKind | null
  isIndexSelf?: boolean
}) {
  const idx = indexAlertKind
  const kind = classifyDefenseAlert(price, cZd, aZd)
  if (kind === 'unknown') {
    return (
      <div className="defense-alert-panel defense-alert-panel--unknown" role="status">
        <div className="defense-alert-panel-heading">双防线简讯</div>
        <p className="defense-alert-panel-line">暂无日线 C-ZD / A-ZD，无法分级</p>
      </div>
    )
  }
  const row = BRIEF[kind]
  const marketBlock = isIndexSelf ? (
    <p className="defense-alert-panel-market">
      当前为上证指数：下图档位即大盘双防线；交易个股时请切换标的 Tab，并对照上证与个股的档位是否共振。
    </p>
  ) : idx != null && idx !== 'unknown' ? (
    <>
      <p className="defense-alert-panel-market-line">
        <span className="defense-alert-panel-market-label">【大盘】</span>
        {INDEX_TIER_LINE[idx]}
      </p>
      <p className="defense-alert-panel-market-hint">{marketSyncHint(kind, idx)}</p>
    </>
  ) : idx === 'unknown' ? (
    <p className="defense-alert-panel-market">
      【大盘】上证日线暂无法分级（缺中枢或数据不全），请先打开「上证指数」日 K 核对后再决策个股。
    </p>
  ) : (
    <p className="defense-alert-panel-market">
      【大盘】上证日线尚未加载完成时，请先等待首屏拉取或点击「上证指数」Tab，再对照大盘双防线。
    </p>
  )

  return (
    <div className={`defense-alert-panel defense-alert-panel--${kind}`} role="status">
      <div className="defense-alert-panel-heading">{row.title}</div>
      <p className="defense-alert-panel-meaning">{row.meaning}</p>
      <p className="defense-alert-panel-action">{row.action}</p>
      <div className="defense-alert-panel-market-wrap">{marketBlock}</div>
    </div>
  )
}
