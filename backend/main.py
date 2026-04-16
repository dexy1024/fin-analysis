import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from services.defense_radar import get_defense_radar_summary_for_api, run_defense_radar, DEFENSE_RADAR_WATCHLIST
from services.indicators import get_history_indicators, get_index_kline, get_latest_indicators
from services.kline_scheduler import setup_kline_scheduler, shutdown_kline_scheduler, set_sse_callback

# SSE 客户端队列 - 存储 asyncio.Queue 对象
_sse_clients: list = []


def notify_sse_clients(include_daily: bool, timestamp: str):
    """调度完成后通知所有 SSE 客户端（在线程中调用）"""
    import asyncio
    
    message = {
        "type": "radar_updated",
        "timestamp": timestamp,
        "include_daily": include_daily,
        "message": "双防线雷达数据已更新"
    }
    
    # 将消息添加到所有客户端队列（在线程中安全地操作队列）
    disconnected = []
    for client in _sse_clients:
        try:
            # 使用 call_soon_threadsafe 在线程中安全地调度异步操作
            if hasattr(client, '_loop'):
                client._loop.call_soon_threadsafe(client.put_nowait, message)
            else:
                client.put_nowait(message)
        except Exception as e:
            logging.debug("SSE: 客户端队列写入失败: %s", e)
            disconnected.append(client)
    
    # 清理失效的客户端
    for client in disconnected:
        if client in _sse_clients:
            _sse_clients.remove(client)
    
    logging.info("SSE: 已通知 %d 个客户端", len(_sse_clients))

# 配置日志输出（确保调度等 INFO 级别日志可见）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # 设置 SSE 广播回调
        set_sse_callback(notify_sse_clients)
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


@app.get("/api/sse/radar-updates")
async def sse_radar_updates():
    """SSE 端点：实时推送雷达数据更新"""
    from asyncio import Queue
    
    client_queue = Queue()
    # 保存当前事件循环，供线程回调使用
    client_queue._loop = asyncio.get_event_loop()
    _sse_clients.append(client_queue)
    
    async def event_generator():
        # 发送初始连接成功消息
        yield f"data: {json.dumps({'type': 'connected', 'message': 'SSE连接已建立'})}\n\n"
        
        try:
            while True:
                # 等待消息（最多30秒，保持连接活跃）
                try:
                    message = await asyncio.wait_for(client_queue.get(), timeout=30)
                    yield f"data: {json.dumps(message)}\n\n"
                except asyncio.TimeoutError:
                    # 发送心跳保持连接
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        except asyncio.CancelledError:
            logging.info("SSE: 客户端连接取消")
        except Exception as e:
            logging.warning("SSE 客户端异常: %s", e)
        finally:
            # 客户端断开，从列表中移除
            if client_queue in _sse_clients:
                _sse_clients.remove(client_queue)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


# 股票名称缓存（从 last_summary.json 加载）
_stock_name_cache: dict[str, str] = {}


def _build_stock_name_cache():
    """构建股票名称缓存（从 last_summary.json + watchlist）"""
    global _stock_name_cache
    if _stock_name_cache:
        return
    
    # 从 watchlist 预加载已知名称
    for code, name in DEFENSE_RADAR_WATCHLIST:
        _stock_name_cache[code.lower()] = name
    
    # 从 last_summary.json 加载名称
    try:
        summary_path = Path(__file__).resolve().parents[1] / "logs" / "defense_radar" / "last_summary.json"
        if summary_path.exists():
            with open(summary_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for sym in data.get("symbols", []):
                    sym_code = str(sym.get("code", "")).strip()
                    sym_name = str(sym.get("name", "")).strip()
                    if sym_code and sym_name:
                        _stock_name_cache[sym_code.lower()] = sym_name
            logging.info("股票名称缓存已加载: %d 个", len(_stock_name_cache))
        else:
            logging.warning("last_summary.json 不存在，仅使用 watchlist 缓存")
    except Exception as e:
        logging.warning("从 last_summary.json 加载名称缓存失败: %s", e)


@app.get("/api/stock/name")
async def stock_name(
    code: str = Query(..., description="股票代码，例如 600000、000001、510300"),
):
    """根据股票代码获取股票名称（从本地 last_summary.json 读取）"""
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

