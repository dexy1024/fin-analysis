/**
 * 与后端 defense_radar._classify 一致：用日线 C-ZD / A-ZD 与现价 P 划分双防线伏击带。
 */

export type DefenseAlertKind =
  | 'level1'
  | 'ultimate'
  | 'red'
  | 'gap'
  | 'safe_above'
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
  const supportHigh = Math.max(vc, va)
  const supportLow = Math.min(vc, va)
  const z1Upper = supportHigh * 1.01
  const z1Lower = supportHigh * 0.99
  const z2Upper = supportLow * 1.01
  const z2Lower = supportLow * 0.99
  if (p >= z1Lower && p <= z1Upper) return 'level1'
  if (p < z1Lower && p > z2Upper) return 'gap'
  if (p >= z2Lower && p <= z2Upper) return 'ultimate'
  if (p < z2Lower) return 'red'
  if (p > z1Upper) return 'safe_above'
  return 'unknown'
}

const BRIEF: Record<
  Exclude<DefenseAlertKind, 'unknown'>,
  { title: string; meaning: string; action: string }
> = {
  level1: {
    title: '【一级警报】第一防线伏击圈',
    meaning:
      '现价落在较高防线（C-ZD 与 A-ZD 中较高者）±1% 带内：常见「健康洗盘」区，大趋势仍强、意在洗筹。',
    action:
      '状态：一级战备。打开本标的 60 分钟图，盯最右侧是否出现蓝三角（底分型）；出现后再按你的仓位纪律执行。下单前务必对照上证指数日线双防线档位（见下方「大盘」）。',
  },
  ultimate: {
    title: '【终极警报】极限防线伏击圈',
    meaning:
      '现价落在较低防线（两 ZD 中较低者）±1% 带内：第一伏击带已下方，深水博弈；反弹弹性大，破位风险也高。',
    action:
      '状态：高度谨慎。60 分钟图 + 优先看清大盘是否同步在极限带或已企稳；宜等蓝三角与 MACD 底背驰（黄块）共振再动，缺一宁可错过。',
  },
  red: {
    title: '【红色警报】防线崩溃',
    meaning:
      '现价已跌破极限防线下沿再 −1%：中枢破坏逻辑，原上涨结构失效，易进入单边下跌。',
    action:
      '状态：禁买。将该标的移出狙击池/拉黑，保护本金，不参与盘中诱多。若大盘仍强仅个股破位，更说明该股弱势，勿抄底。',
  },
  gap: {
    title: '观望 · 两伏击带之间',
    meaning: '现价夹在上下两伏击带之间，尚未进入任一 ±1% 伏击圈。',
    action:
      '不按双防线强行开火；等待进入伏击带或方向明朗后再决策。同时看大盘是否同步走出中间带，避免个股与指数背离硬上。',
  },
  safe_above: {
    title: '未入下方伏击区',
    meaning: '现价高于第一防线上沿（+1%），未踩双防线构成的下方伏击结构。',
    action:
      '双防线狙击节奏未触发；可继续用其他规则观察。若大盘已进入伏击圈而本标的仍在上方，属分化，轻仓或观望。',
  },
}

/** 上证日线档位一句话（与 classifyDefenseAlert 一致） */
const INDEX_TIER_LINE: Record<Exclude<DefenseAlertKind, 'unknown'>, string> = {
  level1: '上证日线：第一防线 ±1% 伏击圈（一级档）',
  ultimate: '上证日线：极限防线 ±1% 伏击圈（终极档）',
  red: '上证日线：红色崩溃档（已破极限下沿 −1%）',
  gap: '上证日线：两伏击带之间（观望档）',
  safe_above: '上证日线：未入下方伏击区（偏高）',
}

function marketSyncHint(self: DefenseAlertKind, idx: DefenseAlertKind): string {
  if (self === 'unknown') return ''
  if (self === 'red') {
    return '（个股已红档禁买；与大盘是否同档不改变禁买。）'
  }
  if (idx === 'red') {
    return '大盘已红档走弱，个股信号一律降权，不宜重仓逆势赌反弹。'
  }
  if (idx === 'ultimate' && (self === 'level1' || self === 'gap')) {
    return '大盘在极限深水带，个股宜轻仓或等上证先企稳再共振。'
  }
  if (self === 'level1' && idx === 'level1') {
    return '与大盘同处一级伏击带，共振时更宜重视 60 分钟买点。'
  }
  if (self === 'level1' && idx === 'safe_above') {
    return '大盘仍偏高、个股已入一级伏击，分化行情，控制仓位、择优。'
  }
  if (idx === 'safe_above' && (self === 'ultimate' || self === 'gap' || self === 'level1')) {
    return '大盘未踩伏击带而个股已偏深，注意强弱背离，宁可等大盘靠拢或个股信号极强。'
  }
  if (self === 'ultimate' && idx === 'level1') {
    return '大盘仍在一级带、个股已入极限带，个股显著弱于指数，抄底条件要更严。'
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
