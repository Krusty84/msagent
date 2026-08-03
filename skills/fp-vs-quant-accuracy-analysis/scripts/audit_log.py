#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
审计日志工具。

提供 JSONL 格式的审计日志写入能力，供 Skill 全流程各阶段调用。
每条日志记录包含：时间戳、阶段、事件类型、关键数据。

用法：
  from audit_log import AuditLogger
  logger = AuditLogger("/workdir/audit.jsonl")
  logger.log("collect_dump", {"side": "fp", "dump_path": "...", "step": [0]})
  logger.log("convert_rotation", {"input": "...", "output": "...", "shape": [4096, 4096]})

JSONL 格式（每行一个 JSON 对象）：
  {"timestamp": "2026-08-03T10:00:00+08:00", "stage": "collect_dump", "event": "...", "data": {...}}
"""

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional


class AuditLogger:
    """审计日志写入器（JSONL 格式，线程安全靠 GIL 保证单写）。"""

    def __init__(self, log_path: Optional[str]):
        """
        初始化审计日志。

        Args:
            log_path: 日志文件路径。若为 None 则不写入（no-op），方便脚本在未指定 --audit-log 时正常运行。
        """
        self.log_path = log_path
        if log_path:
            os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)

    def log(self, stage: str, data: dict, event: str = "ok"):
        """
        写入一条审计日志。

        Args:
            stage: 阶段名称（如 "collect_dump" / "convert_rotation" / "gen_config" / "compare" / "locate"）
            data: 关键数据（路径、shape、规则数等）
            event: 事件类型（"ok" / "warn" / "error"）
        """
        if not self.log_path:
            return
        cst_tz = timezone(timedelta(hours=8), name="CST")
        entry = {
            "timestamp": datetime.now(cst_tz).isoformat(),
            "stage": stage,
            "event": event,
            "data": data,
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def add_audit_arg(parser):
    """为 argparse 添加 --audit-log 参数（所有脚本复用）。"""
    parser.add_argument(
        "--audit-log",
        default=None,
        help="审计日志 JSONL 文件路径（可选）。若提供，关键事件会追加写入该文件",
    )


def get_logger(args) -> AuditLogger:
    """从 argparse 解析结果构造 AuditLogger。"""
    return AuditLogger(getattr(args, "audit_log", None))
