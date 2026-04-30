"""
邮件通知服务：在每日 14:46 快照后，推送非持仓/观望的异动标的摘要。

环境变量（启动前必须设置）：
    EMAIL_SENDER      发件人邮箱（如 xxx@qq.com）
    EMAIL_PASSWORD    QQ 邮箱授权码（非登录密码）
    EMAIL_RECIPIENT   收件人邮箱
    EMAIL_SMTP_HOST   SMTP 服务器（默认 smtp.qq.com）
    EMAIL_SMTP_PORT   SMTP 端口（默认 465）
"""

from __future__ import annotations

import csv
import logging
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

# 项目根目录（backend/services/ 的上两级）
ROOT_DIR = Path(__file__).resolve().parents[2]
LOGS_DIR = ROOT_DIR / "logs"


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _is_configured() -> bool:
    """检查邮件配置是否完整。"""
    return all(
        [
            _get_env("EMAIL_SENDER"),
            _get_env("EMAIL_PASSWORD"),
            _get_env("EMAIL_RECIPIENT"),
        ]
    )


def _read_latest_snapshot_records(csv_path: Path) -> List[dict]:
    """
    读取 CSV 中最新时间戳的所有记录。
    返回列表，每个元素为字段名到值的字典。
    """
    records: List[dict] = []
    if not csv_path.is_file():
        return records

    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return records

            # 先读入所有行
            all_rows = list(reader)
            if not all_rows:
                return records

            # 找到最新的时间戳（最后一行非空行的"时间"字段）
            latest_time = ""
            for row in reversed(all_rows):
                t = row.get("时间", "").strip()
                if t:
                    latest_time = t
                    break

            if not latest_time:
                return records

            # 筛选该时间戳的所有行
            for row in all_rows:
                if row.get("时间", "").strip() == latest_time:
                    records.append(row)
    except Exception:
        logging.warning("email_notifier: 读取 CSV 失败", exc_info=True)

    return records


def _filter_alert_records(records: List[dict]) -> List[Tuple[str, str, str]]:
    """
    筛选出「实际交易动作」不是"持仓"和"观望"的标的。
    返回 [(代码, 名称, 实际交易动作), ...]
    """
    alerts: List[Tuple[str, str, str]] = []
    for rec in records:
        action = rec.get("实际交易动作", "").strip()
        if action and action not in ("持仓", "观望"):
            code = rec.get("代码", "").strip()
            name = rec.get("名称", "").strip()
            alerts.append((code, name, action))
    return alerts


def _action_to_letter(action: str) -> str:
    """将实际交易动作映射为单字母缩写。"""
    if "卖" in action:
        return "S"
    if "买" in action:
        return "B"
    return action[0] if action else "?"


def _build_email_body(
    alerts: List[Tuple[str, str, str]], time_str: str
) -> str:
    """构造纯文本邮件正文。"""
    if not alerts:
        return f"{time_str} 无异动。"

    lines: List[str] = []
    for code, name, action in alerts:
        letter = _action_to_letter(action)
        short_code = code[-4:] if len(code) >= 4 else code
        lines.append(f"{letter}{short_code}")

    return "\n".join(lines)


def send_snapshot_alert(
    csv_path: Optional[Path] = None, slot_time: Optional[datetime] = None
) -> bool:
    """
    读取最新 CSV 快照，筛选异动标的，发送邮件通知。

    Args:
        csv_path:  CSV 文件路径，默认按年推断 logs/snapshots_YYYY.csv
        slot_time: 槽位时间，默认取当前时间

    Returns:
        True 表示发送成功（或无配置时静默跳过），False 表示发送失败。
    """
    if not _is_configured():
        logging.info("email_notifier: 邮件配置不完整，跳过发送")
        return True

    now = slot_time or datetime.now(ZoneInfo("Asia/Shanghai"))
    if csv_path is None:
        csv_path = LOGS_DIR / f"snapshots_{now.year}.csv"

    records = _read_latest_snapshot_records(csv_path)
    alerts = _filter_alert_records(records)

    # 从记录中提取最新时间戳并格式化为 YYYYMMDDHH
    latest_time_raw = ""
    if records:
        latest_time_raw = records[0].get("时间", "").strip()

    time_str = ""
    if latest_time_raw:
        try:
            dt = datetime.strptime(latest_time_raw, "%Y-%m-%d %H:%M:%S")
            time_str = dt.strftime("%Y%m%d%H")
        except ValueError:
            time_str = latest_time_raw.replace("-", "").replace(" ", "").replace(":", "")[:10]

    body = _build_email_body(alerts, time_str)
    subject = time_str if time_str else f"[缠论快照] {now.strftime('%Y-%m-%d %H:%M')} 异动标的"

    sender = _get_env("EMAIL_SENDER")
    recipient = _get_env("EMAIL_RECIPIENT")
    smtp_host = _get_env("EMAIL_SMTP_HOST", "smtp.qq.com")
    try:
        smtp_port = int(_get_env("EMAIL_SMTP_PORT", "465"))
    except ValueError:
        logging.error("email_notifier: EMAIL_SMTP_PORT 配置无效，使用默认 465")
        smtp_port = 465
    password = _get_env("EMAIL_PASSWORD")

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        logging.info(
            "email_notifier: 邮件已发送 %s -> %s (异动标的 %d 个)",
            sender, recipient, len(alerts),
        )
        return True
    except Exception:
        logging.exception("email_notifier: 邮件发送失败")
        return False
