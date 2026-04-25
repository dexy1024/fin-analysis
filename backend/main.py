import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from services.buy_sell_signals import load_buy_sell_signals_json
from services.defense_radar import get_defense_radar_summary_for_api, load_broken_symbols_json, run_defense_radar, DEFENSE_RADAR_WATCHLIST
from services.first_buy_point import detect_first_buy_point, scan_first_buy_points
from services.indicators import get_history_indicators, get_index_kline, get_latest_indicators
from services.kline_scheduler import setup_kline_scheduler, shutdown_kline_scheduler, set_sse_callback, get_scheduler_status
from services import position_manager as pm

WATCHLIST_FILE = Path(__file__).resolve().parents[0] / "data" / "watchlist.json"
OBSERVATION_FILE = Path(__file__).resolve().parents[0] / "data" / "observation.json"

# SSE 客户端队列 - 存储 asyncio.Queue 对象
_sse_clients: list = []
_sse_clients_lock = threading.Lock()


def notify_sse_clients(include_daily: bool, timestamp: str):
    """调度完成后通知所有 SSE 客户端（在线程中调用）"""
    import asyncio
    
    message = {
        "type": "radar_updated",
        "timestamp": timestamp,
        "include_daily": include_daily,
        "message": "双防线雷达数据已更新"
    }
    _send_sse_message(message)


def notify_stop_loss(code: str, reason: str, price: float):
    """止损触发时 SSE 推送告警"""
    message = {
        "type": "stop_loss_triggered",
        "code": code,
        "reason": reason,
        "price": price,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": f"【止损告警】{code} 触发{reason}，现价 {price:.2f}，已自动清仓！"
    }
    _send_sse_message(message)


def _send_sse_message(message: dict):
    """通用 SSE 消息发送（线程安全）"""
    import asyncio

    with _sse_clients_lock:
        clients_snapshot = list(_sse_clients)

    disconnected = []
    for client in clients_snapshot:
        try:
            if hasattr(client, '_loop'):
                client._loop.call_soon_threadsafe(client.put_nowait, message)
            else:
                client.put_nowait(message)
        except Exception as e:
            logging.debug("SSE: 客户端队列写入失败: %s", e)
            disconnected.append(client)

    with _sse_clients_lock:
        for client in disconnected:
            if client in _sse_clients:
                _sse_clients.remove(client)
        remaining_count = len(_sse_clients)

    logging.info("SSE: %s 已通知 %d 个客户端", message.get("type", "unknown"), remaining_count)

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
        # 设置持仓管理 SSE 回调
        pm.set_sse_callback(notify_stop_loss)
        setup_kline_scheduler()
    except Exception:
        # 启动路径必须兜底：任何异常都不应阻断 FastAPI 服务启动
        logging.exception("后台 K 线定时任务启动失败（进程仍可服务 API）")
    yield
    shutdown_kline_scheduler()


app = FastAPI(
    title="A股指标查询服务",
    description="基于 akshare + pandas 的日线级别指标查询接口",
    version="1.0.0",
    lifespan=lifespan,
)

_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS",
    "http://127.0.0.1:5173,http://localhost:5173"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _ALLOWED_ORIGINS if o.strip()],
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
    except (OSError, TypeError, KeyError, RuntimeError) as exc:
        logging.exception("获取股票指标失败: %s", code)
        raise HTTPException(status_code=500, detail="服务器内部错误") from exc
    except Exception as exc:
        # 兜底：记录未知异常的具体上下文，再返回通用错误
        logging.exception("获取股票指标未知异常: %s", code)
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
    except (OSError, TypeError, KeyError, RuntimeError) as exc:
        logging.exception("获取股票历史指标失败: %s", code)
        raise HTTPException(status_code=500, detail="服务器内部错误") from exc
    except Exception as exc:
        logging.exception("获取股票历史指标未知异常: %s", code)
        raise HTTPException(status_code=500, detail="服务器内部错误") from exc

    return result


@app.get("/api/index/kline")
def index_kline(
    symbol: str = Query(
        "sh000001",
        description="K线标的：指数 sh000001；A 股/ETF 6 位；港股 5 位如 01810 或 hk01810",
    ),
    period: str = Query("daily", description="K线周期: daily、60 或 15；支持指数/A 股/ETF/港股"),
    start_date: str = Query("2025-04-13", description="起始日期，格式 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期，格式 YYYY-MM-DD，默认今天"),
    refresh: bool = Query(
        False,
        description="为 true 时强制从网络拉取；60/15 分钟默认优先读本地缓存，refresh=true 才强制走线上并更新缓存",
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
    except (OSError, TypeError, KeyError, RuntimeError) as exc:
        logging.exception("获取指数K线失败: %s", symbol)
        raise HTTPException(status_code=500, detail="服务器内部错误") from exc
    except Exception as exc:
        logging.exception("获取指数K线未知异常: %s", symbol)
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


@app.get("/api/scheduler/status")
def scheduler_status():
    """查询 kline_scheduler 健康状态。"""
    return get_scheduler_status()


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
    except (OSError, TypeError, KeyError, RuntimeError, ValueError) as exc:
        logging.exception("defense_radar 执行失败")
        raise HTTPException(status_code=500, detail="雷达执行失败，请查看后端日志") from exc
    except Exception as exc:
        logging.exception("defense_radar 未知异常")
        raise HTTPException(status_code=500, detail="雷达执行失败，请查看后端日志") from exc


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
    with _sse_clients_lock:
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
            with _sse_clients_lock:
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


@app.get("/api/first-buy-point")
async def first_buy_point(
    code: str = Query(..., description="股票代码，例如 600000、000001"),
):
    """
    检测60分钟第一类买点（一买）
    
    返回一买信号详情，如果没有则返回 null
    """
    try:
        # 从 watchlist 查找名称
        name = ""
        for c, n in DEFENSE_RADAR_WATCHLIST:
            if c == code:
                name = n
                break
        
        signal = detect_first_buy_point(code, name, refresh=False)
        
        if signal:
            return {
                "code": signal.code,
                "name": signal.name,
                "date": signal.date,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "area_ratio": signal.area_ratio,
                "b_area": signal.b_area,
                "c_area": signal.c_area,
                "hub_b_low": signal.hub_b_low,
                "current_low": signal.current_low,
                "has_signal": True,
            }
        else:
            return {"code": code, "has_signal": False}
            
    except (ValueError, OSError, TypeError, KeyError, RuntimeError) as e:
        logging.exception("一买检测失败: %s", code)
        raise HTTPException(status_code=500, detail="检测失败，请查看后端日志") from e
    except Exception as e:
        logging.exception("一买检测未知异常: %s", code)
        raise HTTPException(status_code=500, detail="检测失败，请查看后端日志") from e


@app.get("/api/first-buy-point/scan")
async def scan_first_buy_point():
    """
    扫描监控列表中的所有一买信号
    
    返回所有检测到一买信号的标的列表
    """
    try:
        signals = scan_first_buy_points(DEFENSE_RADAR_WATCHLIST, refresh=False)
        
        return {
            "count": len(signals),
            "signals": [
                {
                    "code": s.code,
                    "name": s.name,
                    "date": s.date,
                    "price": s.price,
                    "stop_loss": s.stop_loss,
                    "area_ratio": s.area_ratio,
                }
                for s in signals
            ],
        }
            
    except (ValueError, OSError, TypeError, KeyError, RuntimeError) as e:
        logging.exception("扫描一买信号失败")
        raise HTTPException(status_code=500, detail="扫描失败，请查看后端日志") from e
    except Exception as e:
        logging.exception("扫描一买信号未知异常")
        raise HTTPException(status_code=500, detail="扫描失败，请查看后端日志") from e


@app.get("/api/positions")
async def get_positions():
    """获取当前所有持仓"""
    holdings = pm.get_holdings()
    return {
        "count": len(holdings),
        "positions": [
            {
                "code": p.code,
                "name": p.name,
                "signal_type": p.signal_type,
                "buy_date": p.buy_date,
                "buy_price": p.buy_price,
                "amount": p.amount,
                "tactical_stop": p.tactical_stop,
                "strategic_stop": p.strategic_stop,
            }
            for p in holdings
        ],
    }


@app.post("/api/positions/buy")
async def position_buy(
    code: str = Query(..., description="股票代码"),
    name: str = Query("", description="股票名称"),
    signal_type: str = Query(..., description="信号类型: first_buy 或 second_buy"),
    price: float = Query(..., description="买入价格"),
    amount: float = Query(..., description="买入金额（元）"),
    tactical_stop: float = Query(..., description="战术止损线"),
    strategic_stop: float = Query(..., description="战略止损线"),
):
    """手动记录买入持仓"""
    position = pm.buy(code, name, signal_type, price, amount, tactical_stop, strategic_stop)
    return {"ok": True, "position": asdict(position)}


@app.post("/api/positions/sell")
async def position_sell(
    code: str = Query(..., description="股票代码"),
    price: float = Query(..., description="卖出价格"),
    reason: str = Query(..., description="清仓原因"),
):
    """手动清仓"""
    position = pm.sell_all(code, price, reason)
    if position:
        return {"ok": True, "position": asdict(position)}
    return {"ok": False, "message": "该代码没有持仓"}


@app.get("/api/positions/history")
async def get_position_history():
    """获取所有持仓历史（含已清仓）"""
    all_positions = pm.get_all_positions()
    return {
        "count": len(all_positions),
        "positions": [asdict(p) for p in all_positions],
    }


@app.get("/api/watchlist")
async def get_watchlist():
    """读取用户持仓/自选列表（backend/data/watchlist.json）"""
    if not WATCHLIST_FILE.exists():
        return {"holdings": []}
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 过滤掉内部注释字段
        holdings = [item for item in data.get("holdings", []) if isinstance(item, dict) and item.get("code")]
        return {"holdings": holdings}
    except Exception as exc:
        logging.warning("读取 watchlist.json 失败: %s", exc)
        return {"holdings": []}


@app.get("/api/observation")
async def get_observation():
    """读取用户观察/自选列表（backend/data/observation.json），仅用于前端显示"""
    if not OBSERVATION_FILE.exists():
        return {"observations": []}
    try:
        with open(OBSERVATION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        observations = [item for item in data.get("observations", []) if isinstance(item, dict) and item.get("code")]
        return {"observations": observations}
    except Exception as exc:
        logging.warning("读取 observation.json 失败: %s", exc)
        return {"observations": []}


@app.get("/api/broken-symbols")
async def get_broken_symbols():
    """
    获取 watchlist + observation 中所有标的的破位状态。
    由定时调度在每次 60m/日线同步后计算并写入 broken_symbols.json，前端刷新页面后直接读取。
    """
    payload = load_broken_symbols_json()
    if payload is None:
        # 文件不存在时返回空结果（首次启动或尚未完成第一次调度）
        return {
            "generated_at": None,
            "broken_codes": [],
            "details": [],
        }
    return payload


@app.get("/api/config/symbols")
async def get_symbols_config():
    """
    返回系统标的配置：核心监控列表 + 用户自定义列表。
    前端启动时拉取，确保标的信息与后端一致，消除前后端配置双源头。
    """
    # 核心监控列表（来自 DEFENSE_RADAR_WATCHLIST）
    core_symbols = [
        {"code": code, "name": name}
        for code, name in DEFENSE_RADAR_WATCHLIST
    ]

    # 用户自定义持仓/自选列表
    watchlist_symbols = []
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            watchlist_symbols = [
                {"code": str(item["code"]).strip(), "name": str(item.get("name", "")).strip()}
                for item in data.get("holdings", [])
                if isinstance(item, dict) and item.get("code")
            ]
        except (OSError, json.JSONDecodeError, TypeError):
            logging.warning("读取 watchlist.json 失败")

    # 用户观察列表
    observation_symbols = []
    if OBSERVATION_FILE.exists():
        try:
            with open(OBSERVATION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            observation_symbols = [
                {"code": str(item["code"]).strip(), "name": str(item.get("name", "")).strip()}
                for item in data.get("observations", [])
                if isinstance(item, dict) and item.get("code")
            ]
        except (OSError, json.JSONDecodeError, TypeError):
            logging.warning("读取 observation.json 失败")

    # 合并自定义列表（去重）
    custom_codes = {s["code"] for s in watchlist_symbols}
    merged_custom = list(watchlist_symbols)
    for sym in observation_symbols:
        if sym["code"] not in custom_codes:
            merged_custom.append(sym)
            custom_codes.add(sym["code"])

    return {
        "core": core_symbols,
        "custom": merged_custom,
        "total_count": len(core_symbols) + len(merged_custom),
    }


@app.get("/api/buy-sell-signals")
async def get_buy_sell_signals():
    """
    获取 watchlist + observation 中所有标的的买卖信号状态。
    由定时调度在每次 60m/日线同步后计算并写入 buy_sell_signals.json，前端刷新页面后直接读取。
    """
    payload = load_buy_sell_signals_json()
    if payload is None:
        return {
            "generated_at": None,
            "buy_codes": [],
            "sell_codes": [],
            "details": [],
        }
    return payload

