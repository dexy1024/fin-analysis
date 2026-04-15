import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import akshare as ak

from services.defense_radar import get_defense_radar_summary_for_api, run_defense_radar, DEFENSE_RADAR_WATCHLIST
from services.indicators import get_history_indicators, get_index_kline, get_latest_indicators
from services.kline_scheduler import setup_kline_scheduler, shutdown_kline_scheduler

# 配置日志输出（确保调度等 INFO 级别日志可见）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        setup_kline_scheduler()
    except Exception:  # noqa: BLE001
        logging.exception("后台 K 线定时任务启动失败（进程仍可服务 API）")
    yield
    shutdown_kline_scheduler()


app = FastAPI(
    title="A股指标查询服务",
    description="基于 akshare + pandas 的日线级别指标查询接口",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/stock/indicators")
async def stock_indicators(code: str = Query(..., description="A股股票代码，例如 600000 或 000001")):
    # 为了更容易定位问题，这里把异常记录到日志里
    try:
        result = get_latest_indicators(code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logging.exception("获取股票指标失败: %s", code)
        raise HTTPException(status_code=500, detail="服务器内部错误") from exc

    return result


@app.get("/api/stock/history-indicators")
async def stock_history_indicators(
    code: str = Query(..., description="A股股票代码，例如 600000 或 000001"),
    start_date: str = Query("2026-01-01", description="起始日期，格式为 YYYY-MM-DD"),
):
    try:
        result = get_history_indicators(code, start_date=start_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logging.exception("获取股票历史指标失败: %s", code)
        raise HTTPException(status_code=500, detail="服务器内部错误") from exc

    return result


@app.get("/api/index/kline")
def index_kline(
    symbol: str = Query(
        "sh000001",
        description="K线标的：指数 sh000001；A 股/ETF 6 位；港股 5 位如 01810 或 hk01810",
    ),
    period: str = Query("daily", description="K线周期: daily 或 60；支持指数/A 股/ETF/港股"),
    start_date: str = Query("2024-12-01", description="起始日期，格式 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期，格式 YYYY-MM-DD，默认今天"),
    refresh: bool = Query(
        False,
        description="为 true 时强制从网络拉取；60 分钟默认优先读本地缓存，refresh=true 才强制走线上并更新缓存",
    ),
):
    try:
        result = get_index_kline(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            period=period,
            refresh=refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logging.exception("获取指数K线失败: %s", symbol)
        raise HTTPException(status_code=500, detail="服务器内部错误") from exc

    return result


@app.get("/api/diagnosis/defense-radar/summary")
def defense_radar_summary(
    refresh: bool = Query(
        False,
        description="须为 false（默认）：只读本地缓存；true 仅排障",
    ),
):
    """供前端筛选 tab：优先读 logs/defense_radar/last_summary.json，与最近一次雷达任务一致。"""
    payload = get_defense_radar_summary_for_api(refresh=refresh)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@app.post("/api/diagnosis/defense-radar")
def defense_radar_diagnosis(
    refresh: bool = Query(
        False,
        description="须为 false（默认）：只读本地缓存，假定 kline_scheduler 已更新；true 仅排障强制拉网",
    ),
):
    """
    双防线雷达：写出 logs/defense_radar/defense_radar_*.md。
    常规由后台 APScheduler 在 60m 同步后执行；此处供手动触发。
    """
    try:
        path = run_defense_radar(refresh=refresh)
        return {"ok": True, "path": str(path)}
    except Exception as exc:  # noqa: BLE001
        logging.exception("defense_radar 执行失败")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/")
async def root():
    return {"message": "A股指标查询服务运行中"}


# 股票名称缓存
_stock_name_cache: dict[str, str] = {}


def _build_stock_name_cache():
    """构建股票名称缓存（A股 + ETF）"""
    global _stock_name_cache
    if _stock_name_cache:
        return
    
    # 从 watchlist 预加载已知名称
    for code, name in DEFENSE_RADAR_WATCHLIST:
        _stock_name_cache[code.lower()] = name
    
    try:
        # A股
        df_a = ak.stock_zh_a_spot_em()
        for _, row in df_a.iterrows():
            code = str(row.get("代码", "")).strip()
            name = str(row.get("名称", "")).strip()
            if code and name:
                _stock_name_cache[code.lower()] = name
    except Exception as e:
        logging.warning("构建A股名称缓存失败: %s", e)
    
    try:
        # ETF
        df_etf = ak.fund_etf_spot_em()
        for _, row in df_etf.iterrows():
            code = str(row.get("代码", "")).strip()
            name = str(row.get("名称", "")).strip()
            if code and name:
                _stock_name_cache[code.lower()] = name
    except Exception as e:
        logging.warning("构建ETF名称缓存失败: %s", e)


@app.get("/api/stock/name")
async def stock_name(
    code: str = Query(..., description="股票代码，例如 600000、000001、510300"),
):
    """根据股票代码获取股票名称"""
    normalized_code = code.strip().lower()
    
    # 处理 sh/sz 前缀
    if normalized_code.startswith("sh") or normalized_code.startswith("sz"):
        normalized_code = normalized_code[2:]
    
    if not normalized_code:
        raise HTTPException(status_code=400, detail="股票代码不能为空")
    
    # 先查缓存
    _build_stock_name_cache()
    if normalized_code in _stock_name_cache:
        return {"code": code.strip(), "name": _stock_name_cache[normalized_code]}
    
    # 港股特殊处理
    if normalized_code.startswith("hk"):
        hk_code = normalized_code[2:]
        # 从 watchlist 查找
        for wcode, wname in DEFENSE_RADAR_WATCHLIST:
            if wcode.lower() == normalized_code:
                return {"code": code.strip(), "name": wname}
        return {"code": code.strip(), "name": f"港股{hk_code}"}
    
    # 未找到
    raise HTTPException(status_code=404, detail=f"未找到股票代码 {code} 对应的名称")

