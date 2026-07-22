#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#    http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------
"""
Host 侧 IO Snapshot 只读采集器（mindstudio-storage-analysis）。

设计原则：
  - 只读：仅读取 /proc、/sys、iostat、pidstat、df、mountstats 等只读数据源，不修改任何状态。
  - Provider 化：每个数据源独立采集，各自记录 source / status / 时间窗 / 退出码 / 错误。
  - 状态模型替代布尔 available：status ∈ {ok, missing, permission_denied,
    command_failed, parse_failed, empty, unsupported}，失败绝不伪装成成功。
  - 同窗并发：diskstats/iostat/pidstat/mountstats 在同一时间窗并发采集，各自记录真实起止时间。
  - 原子写入：先写 *.tmp，pydantic 校验通过后 os.replace 重命名；父目录不存在或写入失败
    时向 stderr 输出原因并以非零退出码返回。

用法:
    python3 collect_io_snapshot.py --duration 30 --out io_snapshot.json
    python3 collect_io_snapshot.py -d 15 -o io_snapshot.json --pid 12345 --path /data

退出码：
    0  成功，输出文件已写入并通过 schema 校验
    2  参数错误
    1  其它失败（采集本身仍尽量完整，但写入/校验失败）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --- 契约常量 ------------------------------------------------------------

SCHEMA_VERSION = "1.4"  # major.minor；minor 只增可选字段，major 变更需分析器显式适配
SUPPORTED_MAJOR = 1

# Provider 状态：失败语义细分类，绝不只用布尔 available
STATUS_OK = "ok"
STATUS_MISSING = "missing"  # 命令/文件不存在
STATUS_PERMISSION = "permission_denied"  # 无权限
STATUS_CMD_FAILED = "command_failed"  # 命令存在但非零退出
STATUS_PARSE_FAILED = "parse_failed"  # 解析失败
STATUS_EMPTY = "empty"  # 命令成功但无输出
STATUS_UNSUPPORTED = "unsupported"  # 平台/工具不支持（如 Lustre 工具未安装）

# 受限 Literal 集合（用于 pydantic 校验，拒绝非法 status）
ProviderStatus = Literal[
    "ok",
    "missing",
    "permission_denied",
    "command_failed",
    "parse_failed",
    "empty",
    "unsupported",
]

# 伪文件系统，挂载/df 采集时跳过
PSEUDO_FS = {
    "proc",
    "sysfs",
    "devpts",
    "tmpfs",
    "devtmpfs",
    "cgroup",
    "cgroup2",
    "debugfs",
    "tracefs",
    "fusectl",
    "securityfs",
    "mqueue",
    "hugetlbfs",
    "rpc_pipefs",
    "binfmt_misc",
    "pstore",
    "bpf",
    "configfs",
    "autofs",
    "selinuxfs",
    "none",
    "overlay",
    "squashfs",
}

# 物理块设备前缀（仅用于候选筛选，最终是否分区由 /sys/class/block/<n>/partition 决定）。
# 含 dm-/md（device-mapper / soft RAID）逻辑设备（Review P2-4）。
BLOCK_DEV_PREFIX = ("nvme", "sd", "vd", "hd", "xvd", "dm-", "md")


# --- pydantic 契约模型 ---------------------------------------------------
# 与 references/io_snapshot_schema.md 对齐。字段用 default=... 保证缺数据时仍可序列化。


class ProviderResult(BaseModel):
    """单个数据源的采集结果。"""

    model_config = ConfigDict(extra="allow")

    source: str = ""
    status: ProviderStatus = STATUS_MISSING
    started_at: str = ""
    ended_at: str = ""
    exit_code: int | None = None
    stderr: str = ""
    error: str = ""
    raw: str = ""
    parsed: Any = None


class DiskStat(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    reads_completed: float = 0
    reads_merged: float = 0
    sectors_read: float = 0
    time_reading_ms: float = 0
    writes_completed: float = 0
    writes_merged: float = 0
    sectors_written: float = 0
    time_writing_ms: float = 0
    io_in_progress: float = 0
    time_io_ms: float = 0
    weighted_time_io_ms: float = 0


class DiskStatSample(BaseModel):
    model_config = ConfigDict(extra="allow")
    sample_index: int = 0
    timestamp: float = 0.0
    disks: dict[str, DiskStat] = Field(default_factory=dict)


class Availability(BaseModel):
    model_config = ConfigDict(extra="allow")
    missing: list[str] = Field(default_factory=list)
    partial: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class IoSnapshot(BaseModel):
    """顶层 IO Snapshot 契约。分析器与分析器测试均依赖此结构。"""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    collected_at: str
    host: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float = 0.0
    window: dict[str, str] = Field(default_factory=dict)  # {start, end} 全局采集窗口
    target: dict[str, Any] = Field(default_factory=dict)  # {pid, path} 用户指定目标
    mounts: list[dict[str, Any]] = Field(default_factory=list)
    mounts_provider: ProviderResult = Field(
        default_factory=lambda: ProviderResult(source="mounts")
    )
    diskstats_sample: list[DiskStatSample] = Field(default_factory=list)
    block_devices: ProviderResult = Field(
        default_factory=lambda: ProviderResult(source="block_devices")
    )
    iostat: ProviderResult = Field(
        default_factory=lambda: ProviderResult(source="iostat")
    )
    pidstat: ProviderResult = Field(
        default_factory=lambda: ProviderResult(source="pidstat")
    )
    process_io_map: ProviderResult = Field(
        default_factory=lambda: ProviderResult(source="process_io_map")
    )
    memory: ProviderResult = Field(
        default_factory=lambda: ProviderResult(source="memory")
    )
    df: ProviderResult = Field(default_factory=lambda: ProviderResult(source="df"))
    nfs: ProviderResult = Field(default_factory=lambda: ProviderResult(source="nfs"))
    readahead: dict[str, int] = Field(default_factory=dict)
    scheduler: dict[str, str] = Field(default_factory=dict)
    availability: Availability = Field(default_factory=Availability)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if not re.match(r"^\d+\.\d+$", str(v)):
            raise ValueError(f"schema_version 必须是 <major>.<minor> 格式，得到 {v!r}")
        return v


# --- 工具函数 ------------------------------------------------------------


def _now_iso() -> str:
    # 快速 provider（如 process_io_map）可能不足 1 秒；保留微秒避免 start=end
    # 被分析器误判为无效窗口。
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


def _have_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def parse_interval(duration: Any) -> int | None:
    """第八轮 P1-2：把 duration 规范为整数秒；非法返回 None。

    只接受有限正整数（或整数值 float）。拒绝 bool（int 子类）、NaN、Inf、≤0、超大值、字符串。
    """
    if isinstance(duration, bool):
        return None
    if isinstance(duration, int):
        d = duration
    elif isinstance(duration, float):
        if duration != duration or duration in (float("inf"), float("-inf")):
            return None
        if duration != int(duration):
            return None  # 小数秒无意义（iostat/pidstat 只支持整数 interval）
        d = int(duration)
    else:
        return None
    if d < 1 or d > 86400:  # 1s..24h
        return None
    return d


def _temp_name(path: str) -> str:
    """第八轮 P2-3：同目录下唯一的临时文件名（含 PID + 随机串），避免并发碰撞。"""
    import uuid

    d = os.path.dirname(path)
    base = os.path.basename(path)
    return os.path.join(d or ".", f".{base}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _run(cmd: list[str], *, timeout: float | None = None) -> tuple[int, str, str]:
    """运行外部命令，返回 (exit_code, stdout, stderr)。失败不抛异常，由调用方判定。"""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except PermissionError as e:
        return 126, "", f"{cmd[0]}: permission denied: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, "", f"{cmd[0]}: {type(e).__name__}: {e}"


def _read_file(path: str) -> tuple[str, str, int]:
    """读文件，返回 (content, error, status_code_int)。status: 0=ok, 1=missing, 2=perm, 3=other"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(), "", 0
    except FileNotFoundError:
        return "", f"{path}: no such file", 1
    except PermissionError:
        return "", f"{path}: permission denied", 2
    except Exception as e:  # noqa: BLE001
        return "", f"{path}: {type(e).__name__}: {e}", 3


def _classify_cmd(cmd: str, exit_code: int, out: str, err: str) -> str:
    """把命令执行结果映射成 provider 状态。"""
    if exit_code == 127:
        return STATUS_MISSING
    if exit_code == 126:
        return STATUS_PERMISSION
    if exit_code == 124:
        return STATUS_CMD_FAILED
    if exit_code != 0:
        return STATUS_CMD_FAILED
    if not out.strip():
        return STATUS_EMPTY
    return STATUS_OK


# --- 各 Provider 采集 ----------------------------------------------------


def _is_real_block_device(name: str) -> bool:
    """判断是否为物理/逻辑块设备主设备（排除分区）。

    通过 /sys/class/block/<name>/partition 判断：存在该文件且内容非 0 即分区。
    兼容无 /sys 的环境（如容器内只读视图缺失），此时退化为前缀过滤并标记 partial。
    """
    # dm-/md 逻辑设备通常无 partition 文件，且不是分区，直接判为整设备
    # （否则启发式会把 md0/dm-0 误判为分区）。但 md0p1/dm-1p2 是分区，需折叠。
    if name.startswith(("dm-", "md")):
        if re.match(r"^(dm-\d+|md\d+)p\d+$", name):
            return False  # md/dm 的分区
        return True
    part_file = f"/sys/class/block/{name}/partition"
    content, err, status = _read_file(part_file)
    if status == 0:
        # partition 文件存在：值为 0 表示是整盘（极少），>0 表示是分区
        try:
            return int(content.strip()) == 0
        except ValueError:
            return True
    # 无 partition 文件：可能是整盘（nvme0n1 这类没有 partition 文件），或无 /sys
    # 退化为名称启发式：带 p<数字>（nvme0n1p1）或纯数字后缀（sda1）视为分区
    if re.search(r"p\d+$", name) or re.search(r"[a-z]\d+$", name):
        # nvme0n1p1 -> True(是分区) ；sda1 -> True；但 nvme0n1 不会命中
        # 注意 nvme0n1 本身不含字母+数字结尾的分区模式（它是 nvme + 0n1）
        if name.startswith("nvme"):
            # nvme0n1 是整盘，nvme0n1p1 是分区
            return not re.search(r"p\d+$", name)
        return False  # sd/vd/hd 带数字后缀视为分区
    return True


def _list_block_devices() -> list[str]:
    """列出物理块设备主设备名（排除分区）。"""
    content, _, status = _read_file("/proc/diskstats")
    if status != 0:
        return []
    devices = []
    for line in content.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        name = fields[2]
        if not name.startswith(BLOCK_DEV_PREFIX):
            continue
        if _is_real_block_device(name):
            devices.append(name)
    return sorted(set(devices))


def _parse_diskstats(content: str) -> dict[str, DiskStat]:
    """解析 /proc/diskstats 单次快照。"""
    disks: dict[str, DiskStat] = {}
    for line in content.splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        if not name.startswith(BLOCK_DEV_PREFIX):
            continue
        if not _is_real_block_device(name):
            continue
        disks[name] = DiskStat(
            name=name,
            reads_completed=float(f[3]),
            reads_merged=float(f[4]),
            sectors_read=float(f[5]),
            time_reading_ms=float(f[6]),
            writes_completed=float(f[7]),
            writes_merged=float(f[8]),
            sectors_written=float(f[9]),
            time_writing_ms=float(f[10]),
            io_in_progress=float(f[11]),
            time_io_ms=float(f[12]),
            weighted_time_io_ms=float(f[13]),
        )
    return disks


def collect_block_devices(
    duration: float,
) -> tuple[ProviderResult, list[DiskStatSample]]:
    """两次采样 /proc/diskstats，返回 ProviderResult 与两个 DiskStatSample。"""
    started = _now_iso()
    content0, err0, st0 = _read_file("/proc/diskstats")
    ts0 = time.time()
    if st0 != 0:
        status = {
            1: STATUS_MISSING,
            2: STATUS_PERMISSION,
        }.get(st0, STATUS_CMD_FAILED)
        return (
            ProviderResult(
                source="block_devices",
                status=status,
                started_at=started,
                ended_at=_now_iso(),
                error=err0,
            ),
            [],
        )
    disks0 = _parse_diskstats(content0)
    time.sleep(max(0.1, duration))
    content1, err1, st1 = _read_file("/proc/diskstats")
    ts1 = time.time()
    if st1 != 0:
        status = {
            1: STATUS_MISSING,
            2: STATUS_PERMISSION,
        }.get(st1, STATUS_CMD_FAILED)
        return (
            ProviderResult(
                source="block_devices",
                status=status,
                started_at=started,
                ended_at=_now_iso(),
                error=err1,
            ),
            [],
        )
    disks1 = _parse_diskstats(content1)
    samples = [
        DiskStatSample(sample_index=0, timestamp=ts0, disks=disks0),
        DiskStatSample(sample_index=1, timestamp=ts1, disks=disks1),
    ]
    return (
        ProviderResult(
            source="block_devices",
            status=STATUS_OK if disks1 else STATUS_EMPTY,
            started_at=started,
            ended_at=_now_iso(),
            error="",
        ),
        samples,
    )


def collect_iostat(duration: float) -> ProviderResult:
    """iostat 动态采样。优先 JSON 输出（sysstat 12.7+），失败降级到按表头解析的文本路径。

    - JSON 路径：`iostat -o JSON -x -k 1 <count>`（最稳健，不依赖列序）。
    - 文本降级：`iostat -xk 1 <count>`，按表头行建立列名→索引映射，兼容现代/旧列序。
    若 JSON 解析得到空 disks，则继续尝试文本路径（sysstat 12.6.x JSON 字段别名兼容）。
    全窗口聚合（-y 丢弃 boot 首报后保留全部真实区间报告），解析为结构化 per-disk 指标。固定 LC_ALL=C。
    """
    started = _now_iso()
    if not _have_cmd("iostat"):
        return ProviderResult(
            source="iostat",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
        )
    env = {**os.environ, "LC_ALL": "C"}
    parsed: dict[str, Any] | None = None
    source_fmt = ""
    raw_out = ""
    exit_code = 0
    err = ""
    drop_boot = False  # -y 成功时为 False（无 boot 报告）；-y 不支持退回无 -y 时为 True

    # 第七轮 P1-3 / 第八轮 P1-2：duration 必须是有限正整数秒；非法 → command_failed。
    eff = parse_interval(duration)
    if eff is None:
        return ProviderResult(
            source="iostat",
            status=STATUS_CMD_FAILED,
            started_at=started,
            ended_at=_now_iso(),
            error=f"invalid duration {duration!r} (need finite int seconds >=1)",
        )

    def _count(with_y: bool) -> int:
        return eff if with_y else eff + 1

    def _y_unsupported(ec: int, err: str) -> bool:
        """仅当 -y 因"选项不被支持"失败时返回 True（此时无 -y 的首份才是 boot 累计）。
        超时/其它瞬时失败不得误判为不支持（第六轮自检 Defect #2）。
        注意：不使用 'usage:' 关键词——瞬时错误的 stderr 也可能含 usage 行，
        会误判并静默丢 boot（自审第二轮发现的回归）。"""
        if ec == 0:
            return False
        low = (err or "").lower()
        return any(
            k in low for k in ("invalid option", "unrecognized", "illegal option")
        )

    y_failed_ec = 0
    y_failed_err = ""

    # 1) JSON + -y
    ec_j, out_j, err_j = _run_with_env(
        ["iostat", "-y", "-o", "JSON", "-x", "-k", "1", str(_count(True))],
        env=env,
        timeout=duration + 10,
    )
    if ec_j == 0 and out_j.strip().startswith("{"):
        parsed = _parse_iostat_json(out_j, drop_boot=False)
        if parsed is not None and not parsed.get("disks"):
            parsed = None
        source_fmt, raw_out, exit_code, err = "json", out_j, ec_j, err_j
    else:
        y_failed_ec, y_failed_err = ec_j, err_j
    # 2) 文本 + -y（JSON -y 未出结果时尝试）
    if parsed is None:
        ec_t, out_t, err_t = _run_with_env(
            ["iostat", "-y", "-xk", "1", str(_count(True))],
            env=env,
            timeout=duration + 10,
        )
        if ec_t == 0 and out_t.strip():
            parsed = _parse_iostat_text(out_t, drop_boot=False)
            if parsed.get("disks"):
                source_fmt, raw_out, exit_code, err = "text", out_t, ec_t, err_t
            else:
                parsed = None
        else:
            y_failed_ec, y_failed_err = ec_t, err_t
    # 3) -y 全部失败：仅在"选项不被支持"时退回无 -y（drop_boot=True，首份确为 boot）。
    #    超时/瞬时失败不得静默退回并误标 boot-dropped——如实报告 command_failed。
    if parsed is None and _y_unsupported(y_failed_ec, y_failed_err):
        ec_j2, out_j2, err_j2 = _run_with_env(
            ["iostat", "-o", "JSON", "-x", "-k", "1", str(_count(False))],
            env=env,
            timeout=duration + 10,
        )
        if ec_j2 == 0 and out_j2.strip().startswith("{"):
            parsed = _parse_iostat_json(out_j2, drop_boot=True)
            if parsed is not None and not parsed.get("disks"):
                parsed = None
            source_fmt, raw_out, exit_code, err = "json", out_j2, ec_j2, err_j2
            drop_boot = True
        else:
            ec_t2, out_t2, err_t2 = _run_with_env(
                ["iostat", "-xk", "1", str(_count(False))],
                env=env,
                timeout=duration + 10,
            )
            exit_code, err, raw_out = ec_t2, err_t2, out_t2
            if ec_t2 == 0 and out_t2.strip():
                parsed = _parse_iostat_text(out_t2, drop_boot=True)
                source_fmt, drop_boot = "text", True
                if not parsed.get("disks"):
                    parsed = None
    elif parsed is None:
        # -y 失败且非"选项不支持"（如超时）：保留 -y 失败状态，不臆造数据
        exit_code, err, raw_out = y_failed_ec, y_failed_err, ""
    if parsed is None:
        status = _classify_cmd("iostat", exit_code, raw_out, err)
        if status == STATUS_OK:
            status = STATUS_PARSE_FAILED
    else:
        # 第七轮 P2-4：有效输出但无受支持设备 → empty（而非 ok），避免与 R000 矛盾。
        status = STATUS_OK if parsed.get("disks") else STATUS_EMPTY
    parsed_out = parsed if parsed is not None else None
    if parsed_out is not None:
        parsed_out["source_format"] = source_fmt
        # 记录 boot 报告处理方式，便于审计窗口对齐（第六轮 P1-1）
        parsed_out["boot_report"] = "dropped" if drop_boot else "excluded_by_y"
        # 标注设备类型（rotational：HDD=1, SSD/NVMe=0），供 analyzer 按介质选阈值（Review P1-3）
        disks = parsed_out.get("disks") or {}
        for name, metrics in disks.items():
            if isinstance(metrics, dict):
                metrics["device_type"] = _device_type(name)
    return ProviderResult(
        source="iostat",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        exit_code=exit_code,
        stderr=err,
        raw=raw_out,
        parsed=parsed_out,
    )


def _device_type(name: str) -> str:
    """读 /sys/block/<dev>/queue/rotational 判断介质类型（Review P1-3）。

    返回 'hdd' / 'ssd' / 'unknown'（NVMe/SSD/未知统一归 ssd 阈值档；unknown 用保守阈值）。
    nvme* 默认归 ssd（NVMe 几乎不可能 rotational）。
    """
    if name.startswith("nvme"):
        return "ssd"
    content, _, st = _read_file(f"/sys/block/{name}/queue/rotational")
    if st == 0:
        return "hdd" if content.strip() == "1" else "ssd"
    return "unknown"


def _run_with_env(
    cmd: list[str], env: dict[str, str], timeout: float | None = None
) -> tuple[int, str, str]:
    """运行外部命令并指定 env（用于固定 locale）。"""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            errors="replace",
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except PermissionError as exc:
        return 126, "", f"{cmd[0]}: permission denied: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"{cmd[0]}: {type(exc).__name__}: {exc}"


def _parse_iostat_json(text: str, drop_boot: bool = False) -> dict[str, Any] | None:
    """解析 iostat -o JSON 输出。收集 statistics 块并按设备聚合（Review P1-1）。

    JSON schema（sysstat）：{sysstat: {hosts: [{statistics: [{disk: [{disk_name / disk_device, ...}]}]}]}}
    兼容字段别名：设备名 `disk_name` / `disk_device`，利用率 `%util` / `util`，队列深度
    `aqu-sz` / `avgqu-sz`。drop_boot：collector 用了 -y 时为 False（全部为真实区间，全保留）；
    -y 不支持退回无 -y 时为 True（首份是开机累计，丢弃）。禁止按报告数量猜测，由 collector
    显式传入（第六轮 P1-1）。
    返回 {disks: {name: 聚合指标}, reports: N}。聚合见 _aggregate_iostat_samples。
    """
    # DEFECT-4 自审（subagent）：非字符串输入（None/int）→ json.loads 抛 TypeError，提前返回。
    if not isinstance(text, str):
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    # G0-5 自审：json.loads('null')→None / json.loads('[]')→list → doc.get 崩溃。
    if not isinstance(doc, dict):
        return None
    # G0-5-NESTED 自审：逐层 isinstance 守卫，避免嵌套畸形 JSON 崩溃。
    sysstat = doc.get("sysstat")
    if not isinstance(sysstat, dict):
        return None
    hosts = sysstat.get("hosts") or []
    if not isinstance(hosts, list) or not hosts:
        return None
    h0 = hosts[0]
    if not isinstance(h0, dict):
        return None
    stats_blocks = h0.get("statistics") or []
    if not isinstance(stats_blocks, list) or not stats_blocks:
        return None
    if drop_boot and len(stats_blocks) >= 2:
        stats_blocks = stats_blocks[1:]
    per_disk_samples: dict[str, list[dict[str, float]]] = {}
    for block in stats_blocks:
        if not isinstance(block, dict):
            continue
        for d in block.get("disk") or []:
            if not isinstance(d, dict):
                continue
            name = d.get("disk_name") or d.get("disk_device")
            if not isinstance(name, str) or not name.startswith(BLOCK_DEV_PREFIX):
                continue
            per_disk_samples.setdefault(name, []).append(_iostat_disk_from_json(d))
    disks = {
        name: _aggregate_iostat_samples(samples)
        for name, samples in per_disk_samples.items()
    }
    return {"disks": disks, "reports": len(stats_blocks)}


def _iostat_disk_from_json(d: dict[str, Any]) -> dict[str, float]:
    """从 iostat JSON 的单个 disk 对象提取统一字段名。字段缺失则缺省 0。"""
    out: dict[str, float] = {}
    mapping = {
        "r_per_s": "r/s",
        "w_per_s": "w/s",
        "rkB_per_s": "rkB/s",
        "wkB_per_s": "wkB/s",
        "rrqm_per_s": "rrqm/s",
        "wrqm_per_s": "wrqm/s",
        "avgqu_sz": ("aqu-sz", "avgqu-sz"),
        "await": "await",
        "r_await_ms": "r_await",
        "w_await_ms": "w_await",
        "util_percent": ("%util", "util"),
        "avgrq_sz": "rareq-sz",
    }
    for ours, theirs in mapping.items():
        if isinstance(theirs, tuple):
            candidates = theirs
        else:
            candidates = (theirs,)
        for key in candidates:
            val = d.get(key)
            if val is None:
                continue
            try:
                out[ours] = float(val)
                break
            except (TypeError, ValueError):
                continue
    return out


# iostat 聚合字段：基础名取**算术平均**；await 按 IO 数（r/s+w/s）加权；util 额外保留 max/p95。
_IOSTAT_AVG_FIELDS = (
    "r_per_s",
    "w_per_s",
    "rkB_per_s",
    "wkB_per_s",
    "rrqm_per_s",
    "wrqm_per_s",
    "avgqu_sz",
    "avgrq_sz",
)


def _aggregate_iostat_samples(samples: list[dict[str, float]]) -> dict[str, Any]:
    """把同一设备的多次采样聚合为窗口统计（Review P1-1）。

    - 速率/队列/IO 大小：算术平均（基础字段名保持不变，下游兼容）。
    - r_await/w_await：按 r/s、w/s 加权平均（避免小 IO 样本被等权放大）。
    - util_percent：平均；另给 util_max / util_p95 / sample_count 供 analyzer 判"持续饱和"。
    """
    n = len(samples)
    out: dict[str, Any] = {"sample_count": n}

    def _col(key: str) -> list[float]:
        return [float(s.get(key, 0) or 0) for s in samples]

    for key in _IOSTAT_AVG_FIELDS:
        vals = _col(key)
        out[key] = round(sum(vals) / n, 4) if vals else 0.0

    # 加权 await（按 r/s / w/s 加权；权重为 0 时退化为算术平均）
    for await_key, weight_key in (("r_await_ms", "r_per_s"), ("w_await_ms", "w_per_s")):
        aw = _col(await_key)
        wt = _col(weight_key)
        num = sum(a * w for a, w in zip(aw, wt))
        den = sum(wt)
        out[await_key] = (
            round(num / den, 4) if den > 0 else (round(sum(aw) / n, 4) if aw else 0.0)
        )
    # 综合 await：算术平均（r/s、w/s 已分别加权）
    raw_await = _col("await")
    out["await"] = round(sum(raw_await) / n, 4) if raw_await else 0.0

    util = sorted(_col("util_percent"))
    if util:
        out["util_percent"] = round(sum(util) / n, 4)
        out["util_max"] = round(util[-1], 4)
        # p95：线性插值（n=1 时等于该值）
        rank = 0.95 * (len(util) - 1)
        lo = int(rank)
        hi = min(lo + 1, len(util) - 1)
        frac = rank - lo
        out["util_p95"] = round(util[lo] + (util[hi] - util[lo]) * frac, 4)
    else:
        out["util_percent"] = 0.0
        out["util_max"] = 0.0
        out["util_p95"] = 0.0
    return out


def _parse_iostat_text(text: str, drop_boot: bool = False) -> dict[str, Any]:
    """解析 iostat -xk 文本输出。按表头建立列名→索引，收集报告并按设备聚合（Review P1-1）。

    drop_boot：collector 用了 -y 时为 False（全保留）；-y 不支持时为 True（丢弃首份 boot 累计）。
    禁止按报告数量猜测，由 collector 显式传入（第六轮 P1-1）。
    返回 {disks: {name: 聚合指标}, reports: N}。聚合见 _aggregate_iostat_samples。
    """
    blocks = re.split(r"\n\s*\n", text.strip())
    col_map = {
        "r_per_s": ("r/s",),
        "w_per_s": ("w/s",),
        "rkB_per_s": ("rkB/s",),
        "wkB_per_s": ("wkB/s",),
        "rrqm_per_s": ("rrqm/s",),
        "wrqm_per_s": ("wrqm/s",),
        "avgqu_sz": ("aqu-sz", "avgqu-sz"),
        "await": ("await",),
        "r_await_ms": ("r_await",),
        "w_await_ms": ("w_await",),
        "util_percent": ("%util",),
        "avgrq_sz": ("rareq-sz", "avgrq-sz"),
    }
    # 先按报告收集每设备的采样（保留报告顺序），再决定是否丢弃首报
    report_samples: list[dict[str, dict[str, float]]] = []
    for blk in blocks:
        lines = [ln for ln in blk.splitlines() if ln.strip()]
        if not lines or not any("Device" in ln for ln in lines):
            continue
        header = next((ln.split() for ln in lines if "Device" in ln), [])
        rows = [ln for ln in lines if "Device" not in ln]
        if not header:
            continue
        idx = {col: header.index(col) for col in header}
        per_dev: dict[str, dict[str, float]] = {}
        for row in rows:
            f = row.split()
            if len(f) < 2:
                continue
            name = f[0]
            if not name.startswith(BLOCK_DEV_PREFIX):
                continue
            m: dict[str, float] = {}
            for ours, aliases in col_map.items():
                for theirs in aliases:
                    if theirs in idx and idx[theirs] < len(f):
                        try:
                            m[ours] = float(f[idx[theirs]])
                            break
                        except (ValueError, IndexError):
                            pass
            if m:
                per_dev[name] = m
        if per_dev:
            report_samples.append(per_dev)
    # Review P1-1：仅在 collector 显式 drop_boot=True（-y 不支持的兼容路径）时丢弃首份 boot 报告。
    if drop_boot and len(report_samples) >= 2:
        report_samples = report_samples[1:]
    per_disk_samples: dict[str, list[dict[str, float]]] = {}
    for rep in report_samples:
        for name, m in rep.items():
            per_disk_samples.setdefault(name, []).append(m)
    disks = {
        name: _aggregate_iostat_samples(samples)
        for name, samples in per_disk_samples.items()
    }
    return {"disks": disks, "reports": max(1, len(report_samples))}


def collect_pidstat(duration: float) -> ProviderResult:
    """pidstat -d 动态采样。优先 JSON（sysstat 12.7.7+），失败降级到按表头解析的文本路径。

    文本路径处理时间列（12/24h、AM/PM）、Average 行、iodelay 列。固定 LC_ALL=C。
    """
    started = _now_iso()
    if not _have_cmd("pidstat"):
        return ProviderResult(
            source="pidstat",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
        )
    # 第七轮 P1-3 / 第八轮 P1-2：duration 必须是有限正整数秒；非法 → command_failed。
    eff = parse_interval(duration)
    if eff is None:
        return ProviderResult(
            source="pidstat",
            status=STATUS_CMD_FAILED,
            started_at=started,
            ended_at=_now_iso(),
            error=f"invalid duration {duration!r} (need finite int seconds >=1)",
        )
    count = eff  # pidstat count = duration（不加 1），与请求窗口对齐。
    env = {**os.environ, "LC_ALL": "C"}
    parsed: dict[str, Any] | None = None
    source_fmt = ""
    raw_out = ""
    exit_code = 0
    err = ""
    # 优先 JSON
    ec_j, out_j, err_j = _run_with_env(
        ["pidstat", "-d", "-o", "JSON", "1", str(count)], env=env, timeout=duration + 10
    )
    if ec_j == 0 and out_j.strip().startswith("{"):
        parsed = _parse_pidstat_json(out_j)
        if parsed is not None:
            source_fmt = "json"
            raw_out, exit_code, err = out_j, ec_j, err_j
    if parsed is None:
        ec_t, out_t, err_t = _run_with_env(
            ["pidstat", "-d", "1", str(count)], env=env, timeout=duration + 10
        )
        exit_code, err, raw_out = ec_t, err_t, out_t
        status = _classify_cmd("pidstat", ec_t, out_t, err_t)
        if status == STATUS_OK:
            parsed = _parse_pidstat_text(out_t)
            source_fmt = "text"
    if parsed is None:
        status = _classify_cmd("pidstat", exit_code, raw_out, err)
        if status == STATUS_OK:
            status = STATUS_PARSE_FAILED
    else:
        status = STATUS_OK
    parsed_out = parsed if parsed is not None else None
    if parsed_out is not None:
        parsed_out["source_format"] = source_fmt
    return ProviderResult(
        source="pidstat",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        exit_code=exit_code,
        stderr=err,
        raw=raw_out,
        parsed=parsed_out,
    )


def _parse_pidstat_json(text: str) -> dict[str, Any] | None:
    """解析 pidstat -o JSON 输出。收集**全部** task snapshots 并按 PID 聚合（Review P1-1）。

    Modern schema uses `statistics[].io` with `PID`/`UID`/`cmd`; older builds may
    use `statistics[].task` with lowercase keys. Accept both explicitly.
    每个 PID 的速率字段取窗口平均（避免仅最后瞬时决定 R400 活跃判定）。
    """
    # DEFECT-4 自审（subagent）：非字符串输入 → 提前返回。
    if not isinstance(text, str):
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    # G0-5 自审：json.loads('null')→None / json.loads('[]')→list → doc.get 崩溃。
    if not isinstance(doc, dict):
        return None
    # G0-5-NESTED 自审：逐层 isinstance 守卫。
    sysstat = doc.get("sysstat")
    if not isinstance(sysstat, dict):
        return None
    hosts = sysstat.get("hosts") or []
    if not isinstance(hosts, list) or not hosts:
        return None
    h0 = hosts[0]
    if not isinstance(h0, dict):
        return None
    stats = h0.get("statistics") or []
    if not isinstance(stats, list) or not stats:
        return None
    per_pid: dict[int, list[dict[str, Any]]] = {}
    recognized = False
    for snap in stats:
        if not isinstance(snap, dict):
            continue
        tasks = None
        for key in ("io", "task"):
            if key in snap:
                recognized = True
                tasks = snap.get(key)
                break
        if tasks is None:
            continue
        if not isinstance(tasks, list):
            return None
        for t in tasks:
            if not isinstance(t, dict):
                continue
            try:
                pid = int(t.get("PID", t.get("pid")))
            except (TypeError, ValueError):
                continue
            per_pid.setdefault(pid, []).append(
                {
                    "uid": t.get("UID", t.get("uid", "")),
                    "kB_rd/s": t.get("kB_rd/s", 0),
                    "kB_wr/s": t.get("kB_wr/s", 0),
                    "kB_ccwr/s": t.get("kB_ccwr/s", 0),
                    "command": t.get("cmd", t.get("command", t.get("Command", ""))),
                }
            )
    if not recognized:
        return None
    procs = [_aggregate_pidstat_samples(pid, snaps) for pid, snaps in per_pid.items()]
    return {"processes": procs, "reports": len(stats)}


def _aggregate_pidstat_samples(pid: int, snaps: list[dict[str, Any]]) -> dict[str, Any]:
    """把同一 PID 的多份 pidstat 快照聚合为窗口平均（Review P1-1）。"""
    n = len(snaps)
    first = snaps[0]
    return {
        "pid": pid,
        "uid": str(first.get("uid", "")),
        "kbr_per_s": round(sum(_to_float(s.get("kB_rd/s")) for s in snaps) / n, 4),
        "kbw_per_s": round(sum(_to_float(s.get("kB_wr/s")) for s in snaps) / n, 4),
        "kbccwd_per_s": round(sum(_to_float(s.get("kB_ccwr/s")) for s in snaps) / n, 4),
        "command": str(first.get("command", "")),
        "sample_count": n,
    }


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_pidstat_text(text: str) -> dict[str, Any] | None:
    """解析 pidstat -d 文本输出，按表头索引，处理时间列/AM-PM/Average 行/iodelay。

    真实输出形如：
      12:00:00   UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay  Command
      12:00:01  1000 1234 2048.00 0.00 0.00 0 python
      Average:  1000 1234 2048.00 0.00 0.00 0 python
    收集**全部**非 Average 报告并按 PID 聚合（窗口平均，Review P1-1）。
    """
    blocks = re.split(r"\n\s*\n", text.strip())
    samples: list[list[str]] = []  # 每份报告的数据行
    last_header: list[str] = []
    for blk in blocks:
        lines = [ln for ln in blk.splitlines() if ln.strip()]
        if not lines:
            continue
        header_line = next((ln for ln in lines if "UID" in ln and "PID" in ln), None)
        if header_line:
            last_header = header_line.split()
            rows = [ln for ln in lines if ln is not header_line and "UID" not in ln]
            samples.append(rows)
    procs: list[dict[str, Any]] = []
    if not last_header:
        return None
    idx = {col: i for i, col in enumerate(last_header)}

    def col_of(*names: str) -> int | None:
        for n in names:
            if n in idx:
                return idx[n]
        return None

    pid_i = col_of("PID")
    uid_i = col_of("UID")
    kbr_i = col_of("kB_rd/s")
    kbw_i = col_of("kB_wr/s")
    kbcc_i = col_of("kB_ccwr/s")
    command_i = col_of("Command", "cmd")
    if pid_i is None:
        return None

    # 按 PID 收集所有快照
    per_pid: dict[int, list[dict[str, Any]]] = {}
    for rows in samples:
        for row in rows:
            f = row.split()
            if len(f) <= pid_i or f[0].lower().startswith("average"):
                continue
            try:
                pid = int(f[pid_i])
            except (ValueError, IndexError):
                continue
            cmd_start = command_i if command_i is not None else len(f)
            per_pid.setdefault(pid, []).append(
                {
                    "uid": f[uid_i] if uid_i is not None and uid_i < len(f) else "",
                    "kbr_per_s": _safe_float(f, kbr_i),
                    "kbw_per_s": _safe_float(f, kbw_i),
                    "kbccwd_per_s": _safe_float(f, kbcc_i),
                    "command": " ".join(f[cmd_start:]) if cmd_start < len(f) else "",
                }
            )

    for pid, snaps in per_pid.items():
        n = len(snaps)
        first = snaps[0]
        procs.append(
            {
                "pid": pid,
                "uid": first.get("uid", ""),
                "kbr_per_s": round(sum(s["kbr_per_s"] for s in snaps) / n, 4),
                "kbw_per_s": round(sum(s["kbw_per_s"] for s in snaps) / n, 4),
                "kbccwd_per_s": round(sum(s["kbccwd_per_s"] for s in snaps) / n, 4),
                "command": first.get("command", ""),
                "sample_count": n,
            }
        )
    return {"processes": procs}


def _safe_float(fields: list[str], idx: int | None) -> float:
    if idx is None or idx >= len(fields):
        return 0.0
    try:
        return float(fields[idx])
    except (ValueError, IndexError):
        return 0.0


def _children_of(pid: int) -> list[int]:
    """读 /proc/<pid>/task/<tid>/children 获取直接子进程（内核需 CONFIG_PROC_CHILDREN）。

    不可用时返回空，由调用方退化。
    """
    try:
        with open(f"/proc/{pid}/task/{pid}/children", encoding="utf-8") as f:
            return [int(x) for x in f.read().split() if x.isdigit()]
    except (OSError, ValueError):
        return []


def _process_tree(root: int) -> list[dict[str, Any]]:
    """从 root 递归收集进程树（BFS），返回 [{pid, role}]（role: root/descendant）。

    优先用 /proc/<pid>/task/<pid>/children；不可用时退化扫 /proc/*/stat 的 ppid。
    """
    tree: list[dict[str, Any]] = [{"pid": root, "role": "root"}]
    seen: set[int] = {root}
    queue: list[int] = [root]
    # 始终构建 ppid 映射并与 children 接口取并集。只在 children 为空时退化会
    # 漏掉由其他线程创建、未出现在 leader children 文件中的 worker。
    ppid_map: dict[int, list[int]] = {}
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        proc_entries = []
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
            rp = stat.rfind(")")
            if rp < 0:
                continue
            ppid = int(stat[rp + 2 :].split()[1])
            ppid_map.setdefault(ppid, []).append(int(entry))
        except (OSError, ValueError, IndexError):
            continue
    while queue:
        cur = queue.pop(0)
        kids = sorted(set(_children_of(cur)) | set(ppid_map.get(cur, [])))
        for k in kids:
            if k not in seen:
                seen.add(k)
                tree.append({"pid": k, "role": "descendant"})
                queue.append(k)
    return tree


def _opened_files(pid: int) -> tuple[set[str], bool]:
    """读 /proc/<pid>/fd/* 的符号链接 + cwd，返回 (路径集合, 是否被权限拒绝)。

    Review P2-2：旧实现吞掉 PermissionError，导致全部被拒时仍报 empty 而非 permission_denied。
    """
    opened: set[str] = set()
    denied = False
    fd_dir = f"/proc/{pid}/fd"
    try:
        for fd in os.listdir(fd_dir):
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
                if target.startswith("/"):  # 跳过 socket/pipe/anon_inode
                    opened.add(target)
            except PermissionError:
                denied = True
                continue
            except OSError:
                continue
    except PermissionError:
        denied = True
    except OSError:
        pass
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        if cwd.startswith("/"):
            opened.add(cwd)
    except PermissionError:
        denied = True
    except OSError:
        pass
    return opened, denied


def _fd_mnt_id(pid: int, fd: str) -> int | None:
    """读取 fdinfo.mnt_id，用于在 overmount/旧 FD 场景精确绑定 mountinfo。"""
    try:
        with open(f"/proc/{pid}/fdinfo/{fd}", encoding="utf-8") as info:
            for line in info:
                if line.startswith("mnt_id:"):
                    return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


def _opened_file_records(pid: int) -> tuple[list[dict[str, Any]], bool]:
    """返回带 fd/mnt_id 的打开路径；保留成功项并单独标记部分权限失败。"""
    records: list[dict[str, Any]] = []
    denied = False
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except PermissionError:
        return [], True
    except OSError:
        fds = []
    for fd in fds:
        try:
            target = os.readlink(f"{fd_dir}/{fd}")
            if target.startswith("/"):
                records.append(
                    {
                        "path": target,
                        "fd": int(fd) if fd.isdigit() else fd,
                        "mnt_id": _fd_mnt_id(pid, fd),
                        "path_source": "fd",
                    }
                )
        except PermissionError:
            denied = True
        except OSError:
            continue
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        if cwd.startswith("/"):
            records.append(
                {"path": cwd, "fd": None, "mnt_id": None, "path_source": "cwd"}
            )
    except PermissionError:
        denied = True
    except OSError:
        pass
    return records, denied


def _pids_for_path(path: str | None) -> tuple[list[int], int]:
    """反查访问 path 的 PID：扫描所有进程的 /proc/<pid>/fd readlink + cwd，按 path 前缀匹配。

    Review P1-6：旧实现只检查 cwd，训练进程通常 cwd 不在数据集目录 → 漏报。
    """
    if not path:
        return [], 0
    prefix = path.rstrip("/") + "/"
    pids: list[int] = []
    denied_count = 0
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return [], 0
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        hit = False
        opened, denied = _opened_files(pid)
        if denied:
            denied_count += 1
        for target in opened:
            if target == path or target.startswith(prefix):
                hit = True
                break
        if not hit:
            # cwd 也算
            try:
                cwd = os.readlink(f"/proc/{entry}/cwd")
                if cwd == path or cwd.startswith(prefix):
                    hit = True
            except OSError:
                pass
        if hit:
            pids.append(pid)
    return pids, denied_count


# /proc/<pid>/mountinfo：mount_id parent_id major:minor root mount_point opts ... - fstype source super_opts
_MOUNTINFO_LINE_RE = re.compile(r"^\d+\s+\d+\s+\d+:\d+\s+\S+\s+(\S+)\s+")


def _mountinfo_table(pid: int | None = None) -> list[dict[str, Any]]:
    """读目标 PID（默认 self）的 mountinfo，返回最长前缀匹配表。

    天然支持路径含空格/Unicode（字段拆分而非外部命令）。每项含
    {mount_point, source, fstype, major_minor}（major_minor 用于设备拓扑归一）。
    """
    proc_id = str(pid) if pid is not None else "self"
    content, _, st = _read_file(f"/proc/{proc_id}/mountinfo")
    if st != 0:
        return []
    entries: list[dict[str, Any]] = []
    for line in content.splitlines():
        f = line.split()
        # 格式：mount_id parent_id major:minor root mount_point options ... - fstype source ...
        if len(f) < 10:
            continue
        # mount_point/source 中空格等特殊字符以八进制转义（\040=空格），需还原。
        mount_point = _decode_mountinfo_octal(f[4])
        major_minor = f[2] if ":" in f[2] else ""
        try:
            sep = f.index("-")
            fstype = f[sep + 1]
            source = _decode_mountinfo_octal(f[sep + 2]) if len(f) > sep + 2 else ""
        except ValueError:
            fstype, source = "", ""
        entries.append(
            {
                "mount_id": int(f[0]) if f[0].isdigit() else None,
                "mount_point": mount_point,
                "source": source,
                "fstype": fstype,
                "major_minor": major_minor,
            }
        )
    # 长路径优先（最长前缀匹配）
    entries.sort(key=lambda e: len(e["mount_point"]), reverse=True)
    return entries


def _decode_mountinfo_octal(token: str) -> str:
    r"""还原 procfs 八进制及 findmnt --raw 的 \xHH 转义。"""
    decoded = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), token)
    return re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), decoded)


# 设备名归一缓存（major:minor → canonical 结果），跨 mapping 复用。
_CANONICAL_DEVICE_CACHE: dict[str, dict[str, Any]] = {}


def _resolve_canonical_device(major_minor: str, source: str) -> dict[str, Any]:
    """把挂载源归一到 iostat 报告的"整盘/逻辑主设备"名（Review P1-2 设备拓扑归一）。

    策略（与 R100 iostat 设备名可 join）：
      - major:minor → /sys/dev/block/<mm> realpath → 父目录判断；分区折叠到整盘
        （/dev/sda1 → sda），整盘/dm/md 取 sysfs 叶子 basename（dm-0/md0/nvme0n1）。
      - dm/md 保留为独立 canonical（iostat 在 dm-0/md0 上报告饱和），另记录 backing_devices。
    无 /sys（如容器只读视图缺失）时退化为名称启发式并标注 device_resolution="heuristic"。

    返回 {canonical_device, major_minor, backing_devices, device_resolution}。
    """
    cache_key = f"{major_minor}|{source}"
    if cache_key in _CANONICAL_DEVICE_CACHE:
        return _CANONICAL_DEVICE_CACHE[cache_key]
    result = _resolve_canonical_device_impl(major_minor, source)
    _CANONICAL_DEVICE_CACHE[cache_key] = result
    return result


def _resolve_canonical_device_impl(major_minor: str, source: str) -> dict[str, Any]:
    base = {
        "canonical_device": source,
        "major_minor": major_minor,
        "backing_devices": [],
        "device_resolution": "unknown",
    }
    if not major_minor or ":" not in major_minor:
        # 无 major:minor，直接走启发式
        return _heuristic_canonical(source, base)
    sys_path = f"/sys/dev/block/{major_minor}"
    try:
        real = os.path.realpath(sys_path)
    except OSError:
        real = ""
    if not real or not os.path.exists(real):
        return _heuristic_canonical(source, base)
    leaf = os.path.basename(real)
    parent = os.path.basename(os.path.dirname(real))
    # 判断是否分区：sysfs 中分区的父目录即整盘（sda1 的父是 sda）。
    # 用 /sys/class/block/<leaf>/partition 存在性确认（>0 即分区）。
    part_file = f"/sys/class/block/{leaf}/partition"
    _, _, pst = _read_file(part_file)
    is_partition = pst == 0
    canonical = parent if is_partition else leaf
    if not canonical or not canonical.startswith(BLOCK_DEV_PREFIX):
        canonical = leaf if leaf.startswith(BLOCK_DEV_PREFIX) else source
    backing: list[str] = []
    # dm/md backing 链（slaves 列出底层物理盘，仅记录不折叠）
    slaves_dir = f"/sys/dev/block/{major_minor}/slaves"
    try:
        for s in os.listdir(slaves_dir):
            if s.startswith(BLOCK_DEV_PREFIX):
                backing.append(s)
    except OSError:
        pass
    base.update(
        {
            "canonical_device": canonical,
            "major_minor": major_minor,
            "backing_devices": sorted(set(backing)),
            "device_resolution": "sysfs",
        }
    )
    return base


def _heuristic_canonical(source: str, base: dict[str, Any]) -> dict[str, Any]:
    """无 /sys 时的设备名启发式归一（标注 heuristic，置信度影响在 analyzer 处理）。"""
    src = source
    # /dev/sda1 → sda；/dev/nvme0n1p2 → nvme0n1
    m = re.match(
        r"^/dev/((?:nvme\d+n\d+|sd[a-z]+|vd[a-z]+|hd[a-z]+|xvd[a-z]+))p?\d*$", src
    )
    if m:
        base.update({"canonical_device": m.group(1), "device_resolution": "heuristic"})
        return base
    # /dev/dm-0 → dm-0；/dev/mapper/vg-data → dm-?（无法仅靠名定号，保留 source 标注）
    m = re.match(r"^/dev/(dm-\d+)$", src)
    if m:
        base.update({"canonical_device": m.group(1), "device_resolution": "heuristic"})
        return base
    if src.startswith("/dev/mapper/"):
        base.update(
            {
                "canonical_device": src,
                "device_resolution": "heuristic-unresolved-mapper",
            }
        )
        return base
    m = re.match(r"^/dev/(md\d+)$", src)
    if m:
        base.update({"canonical_device": m.group(1), "device_resolution": "heuristic"})
        return base
    # 去掉 /dev/ 前缀作为兜底
    base.update(
        {
            "canonical_device": src.removeprefix("/dev/"),
            "device_resolution": "heuristic",
        }
    )
    return base


_MOUNTINFO_TABLE_CACHE: dict[str, list[dict[str, Any]]] | None = None


def _mount_namespace_key(pid: int | None = None) -> str:
    """返回 mount namespace + process root 身份，避免同 namespace 的 chroot 串表。"""
    proc_id = str(pid) if pid is not None else "self"
    try:
        namespace = os.readlink(f"/proc/{proc_id}/ns/mnt")
    except OSError:
        return f"pid:{proc_id}"
    try:
        root_stat = os.stat(f"/proc/{proc_id}/root")
        root_key = f"{root_stat.st_dev}:{root_stat.st_ino}"
    except OSError:
        # root 身份未知时不得与其他 PID 共用 cache。
        root_key = f"pid:{proc_id}"
    return f"{namespace}|root:{root_key}"


def _cached_mountinfo_table(pid: int | None = None) -> tuple[list[dict[str, Any]], str]:
    """按 mount namespace 缓存 mountinfo；同 namespace 的多个 PID 可安全复用。"""
    global _MOUNTINFO_TABLE_CACHE
    if not isinstance(_MOUNTINFO_TABLE_CACHE, dict):
        _MOUNTINFO_TABLE_CACHE = {}
    key = _mount_namespace_key(pid)
    if key not in _MOUNTINFO_TABLE_CACHE:
        table = _mountinfo_table(pid)
        # 若目标与采集器明确处于同一 namespace，可安全退化到 self 视图。
        if not table and pid is not None and key == _mount_namespace_key(None):
            table = _mountinfo_table(None)
        _MOUNTINFO_TABLE_CACHE[key] = table
    return _MOUNTINFO_TABLE_CACHE[key], key


def _resolve_path_to_mount(
    path: str, pid: int | None = None, mnt_id: int | None = None
) -> dict[str, Any] | None:
    """在目标 PID 的 mount namespace 中把路径解析到挂载点。

    Review P1-6：findmnt 必须用 -T/--target 才能查找包含路径的挂载；旧实现把 path 当
    source 传入，对普通文件路径无效。
    """
    table, namespace = _cached_mountinfo_table(pid)
    if mnt_id is not None:
        exact = next(
            (entry for entry in table if entry.get("mount_id") == mnt_id), None
        )
        if exact is not None:
            return {
                "mount_id": exact.get("mount_id"),
                "mount_point": exact["mount_point"],
                "source": exact["source"],
                "fstype": exact["fstype"],
                "major_minor": exact.get("major_minor", ""),
                "mount_namespace": namespace,
                "mount_binding": "fdinfo_mnt_id",
            }
    # mountinfo 最长前缀匹配
    norm = path.rstrip("/") or "/"
    for e in table:
        mp = e["mount_point"]
        if (
            norm == mp
            or norm.startswith(mp.rstrip("/") + "/")
            or (mp == "/" and norm.startswith("/"))
        ):
            return {
                "mount_point": mp,
                "source": e["source"],
                "fstype": e["fstype"],
                "major_minor": e.get("major_minor", ""),
                "mount_namespace": namespace,
                "mount_id": e.get("mount_id"),
                "mount_binding": "path_prefix",
            }
    # findmnt 只能观察调用者 namespace。仅 self 或已确认与 self 同 namespace 时可退化。
    same_as_self = pid is None or namespace == _mount_namespace_key(None)
    if same_as_self and _have_cmd("findmnt"):
        exit_code, out, _ = _run(
            ["findmnt", "-n", "-r", "-T", path, "-o", "TARGET,SOURCE,FSTYPE"]
        )
        if exit_code == 0:
            parts = out.strip().split()
            if len(parts) >= 3:
                return {
                    "mount_point": _decode_mountinfo_octal(parts[0]),
                    "source": _decode_mountinfo_octal(parts[1]),
                    "fstype": parts[2],
                    "mount_namespace": namespace,
                }
    return None


def collect_process_io_map(
    pid: int | None, path: str | None, duration: float = 0.0
) -> ProviderResult:
    """建立 PID → path → mount → device 映射（用于 R400）。

    Review P1-6 改进：
      - --pid 扩展为进程树（root + 后代，记录 pid_tree 与 role）。
      - --path 反查扫描 /proc/<pid>/fd readlink + cwd（不再只看 cwd）。
      - 路径→挂载用 mountinfo 最长前缀匹配（首选，支持空格/Unicode），findmnt -T 交叉校验。
      - 每个 PID 截取去重后的 FD（上限 256/进程），截断记 partial。
      - 每条映射标注 path_relevant（数据相关 vs 共享库/日志）。
    无 pid/path 时返回 unsupported（不臆造映射）。访问其它用户进程受限时标 permission_denied。
    """
    started = _now_iso()
    if not pid and not path:
        return ProviderResult(
            source="process_io_map",
            status=STATUS_UNSUPPORTED,
            started_at=started,
            ended_at=_now_iso(),
        )
    if pid and not os.path.isdir(f"/proc/{pid}"):
        return ProviderResult(
            source="process_io_map",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
            error=f"pid {pid} does not exist or is no longer visible in /proc",
        )

    pid_tree: list[dict[str, Any]] = []
    if pid:
        pid_tree = _process_tree(pid)
    path_scan_denied = 0
    if path and not pid:
        path_pids, path_scan_denied = _pids_for_path(path)
        for p in path_pids:
            pid_tree.append({"pid": p, "role": "root"})
    pids = [e["pid"] for e in pid_tree]

    mapping: list[dict[str, Any]] = []
    errors: list[str] = []
    partial: list[str] = []
    denied_pids: list[int] = []
    mountinfo_failed_pids: list[int] = []
    fd_cap = 256
    for entry in pid_tree:
        p = entry["pid"]
        opened, denied = _opened_file_records(p)
        if denied and not opened:
            denied_pids.append(p)
            errors.append(f"pid {p}: permission denied reading /proc/{p}/fd")
            continue  # 该 PID 无法读取 FD，跳过（不臆造映射）
        if denied:
            partial.append(f"pid {p}: 部分 FD/cwd 无权限，保留已成功读取的映射")
        # 过滤为常规文件路径并去重；截断保护
        targets = [record for record in opened if record["path"].startswith("/")]
        # 数据/target 相关路径优先，避免大量系统库 FD 按字典序占满 cap。
        target_prefix = (path.rstrip("/") + "/") if path else ""
        targets = sorted(
            targets,
            key=lambda record: (
                not (
                    (
                        path
                        and (
                            record["path"] == path
                            or record["path"].startswith(target_prefix)
                        )
                    )
                    or _is_data_relevant_path_collector(record["path"])
                ),
                record["path"],
            ),
        )
        deduped: dict[tuple[Any, Any], dict[str, Any]] = {}
        for record in targets:
            deduped[(record["path"], record.get("mnt_id"))] = record
        targets = list(deduped.values())
        if len(targets) > fd_cap:
            partial.append(f"pid {p}: {len(targets)} FD targets > {fd_cap}，截断")
            targets = targets[:fd_cap]
        mount_table, mount_namespace = _cached_mountinfo_table(p)
        if targets and not mount_table:
            mountinfo_failed_pids.append(p)
            errors.append(f"pid {p}: 无法读取目标 mount namespace 的 mountinfo")
            partial.append(
                f"pid {p}: mount namespace {mount_namespace} 不可解析，已跳过路径→设备确认"
            )
            continue
        for record in targets:
            t = record["path"]
            mi = _resolve_path_to_mount(t, pid=p, mnt_id=record.get("mnt_id"))
            if not mi:
                continue
            # 设备拓扑归一（Review P1-2）：把挂载源 major:minor 解析成 iostat 整盘名
            cand = _resolve_canonical_device(mi.get("major_minor", ""), mi["source"])
            if cand.get("device_resolution") == "heuristic-unresolved-mapper" and (
                "device resolution: heuristic (no /sys)" not in partial
            ):
                partial.append("device resolution: heuristic (no /sys)")
            mapping.append(
                {
                    "pid": p,
                    "role": entry.get("role", "root"),
                    "path": t,
                    "mount_point": mi["mount_point"],
                    "source": mi["source"],
                    "fstype": mi["fstype"],
                    "canonical_device": cand["canonical_device"],
                    "major_minor": cand.get("major_minor", ""),
                    "backing_devices": cand.get("backing_devices", []),
                    "device_resolution": cand.get("device_resolution", "unknown"),
                    "mount_namespace": mi.get("mount_namespace", mount_namespace),
                    "mount_id": mi.get("mount_id"),
                    "mount_binding": mi.get("mount_binding", "path_prefix"),
                    "fd": record.get("fd"),
                    "path_source": record.get("path_source"),
                    "path_relevant": _is_data_relevant_path_collector(t, path),
                    "first_seen": started,
                    "last_seen": _now_iso(),
                }
            )
    observation_samples = 1
    if duration > 0:
        # 与 iostat/pidstat 同窗做 t0/t1 两次轻量观察，捕获快速 open/read/close
        # workload；合并时保留每条路径首次/末次观测时间。
        time.sleep(max(0.1, duration))
        second = collect_process_io_map(pid, path, 0.0)
        observation_samples = 2
        second_parsed = second.parsed if isinstance(second.parsed, dict) else {}
        merged: dict[tuple[Any, Any, Any], dict[str, Any]] = {
            (item.get("pid"), item.get("path"), item.get("mount_id")): item
            for item in mapping
        }
        for item in second_parsed.get("mappings", []) or []:
            if not isinstance(item, dict):
                continue
            key = (item.get("pid"), item.get("path"), item.get("mount_id"))
            if key in merged:
                merged[key]["last_seen"] = item.get("last_seen") or second.ended_at
            else:
                merged[key] = item
        mapping = list(merged.values())
        if second.error:
            errors.append(second.error)
        partial.extend(second_parsed.get("partial", []) or [])
        denied_pids.extend(
            entry.get("pid")
            for entry in second_parsed.get("pid_tree", []) or []
            if isinstance(entry, dict)
            and second.status == STATUS_PERMISSION
            and isinstance(entry.get("pid"), int)
        )
    # Review P2-2：全部目标 PID 被拒 → permission_denied；部分成功 → ok + partial 记 denied
    all_denied = bool(denied_pids) and not mapping and len(denied_pids) == len(pids)
    if path_scan_denied:
        partial.append(f"--path 扫描中 {path_scan_denied} 个 PID 无权限读取")
    if not pids and path_scan_denied:
        status = STATUS_PERMISSION
        errors.append("--path 扫描受 /proc 权限限制，不能确认无访问进程")
    elif all_denied:
        status = STATUS_PERMISSION
    elif mountinfo_failed_pids and not mapping:
        status = STATUS_PERMISSION
    elif mapping:
        status = STATUS_OK
        if denied_pids:
            partial.append(f"permission denied PIDs: {denied_pids}")
    elif denied_pids:
        status = STATUS_PERMISSION
    else:
        status = STATUS_EMPTY
    return ProviderResult(
        source="process_io_map",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        error="; ".join(errors),
        parsed={
            "mappings": mapping,
            "pid_count": len(pids),
            "pid_tree": pid_tree,
            "partial": partial,
            "denied_pid_count": len(denied_pids),
            "observation_samples": observation_samples,
            "mountinfo_failed_pids": sorted(set(mountinfo_failed_pids)),
        },
    )


# 与 analyzer._is_data_relevant_path 同义（collector 侧用于给 mapping 标注）。
def _is_data_relevant_path_collector(
    path: str | None, target_path: str | None = None
) -> bool:
    if not path:
        return False
    normalized = os.path.normpath(path)
    low = normalized.lower()
    if low.endswith((".so", ".so.", ".pyc", ".pyo", ".pyd")):
        return False
    if "/site-packages/" in low or "/dist-packages/" in low:
        return False

    if isinstance(target_path, str):
        target = os.path.normpath(target_path)
        if target not in ("", "/", "."):
            return normalized == target or normalized.startswith(
                target.rstrip("/") + "/"
            )

    prefixes = (
        "/usr",
        "/lib",
        "/lib64",
        "/opt",
        "/var/log",
        "/var/lib",
        "/proc",
        "/sys",
        "/dev",
        "/boot",
        "/etc",
        "/run",
        "/snap",
        "/conda",
        "/python",
        "/bin",
        "/sbin",
    )
    if any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in prefixes
    ):
        return False
    return True


def collect_mounts(pid: int | None = None) -> ProviderResult:
    """目标 PID（默认 self）视角的 mounts，排除伪文件系统。"""
    started = _now_iso()
    proc_id = str(pid) if pid is not None else "self"
    content, err, status_int = _read_file(f"/proc/{proc_id}/mounts")
    if status_int == 1:
        return ProviderResult(
            source="mounts",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    if status_int == 2:
        return ProviderResult(
            source="mounts",
            status=STATUS_PERMISSION,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    if status_int != 0:
        return ProviderResult(
            source="mounts",
            status=STATUS_CMD_FAILED,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    mounts: list[dict[str, Any]] = []
    for line in content.splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        fstype = f[2]
        if fstype in PSEUDO_FS:
            continue
        mounts.append(
            {
                "device": _decode_mountinfo_octal(f[0]),
                "mount_point": _decode_mountinfo_octal(f[1]),
                "fstype": fstype,
                "options": _decode_mountinfo_octal(f[3]),
            }
        )
    status = STATUS_OK if mounts else STATUS_EMPTY
    return ProviderResult(
        source="mounts",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        parsed=mounts,
        raw=content,
    )


def collect_df(pid: int | None = None) -> ProviderResult:
    """df -hP（空间）+ df -iP（inode）分开采集并按挂载点合并。"""
    started = _now_iso()
    if pid is not None and _mount_namespace_key(pid) != _mount_namespace_key(None):
        return ProviderResult(
            source="df",
            status=STATUS_UNSUPPORTED,
            started_at=started,
            ended_at=_now_iso(),
            error="target PID mount namespace/root differs; self df output would be misleading",
        )
    if not _have_cmd("df"):
        return ProviderResult(
            source="df", status=STATUS_MISSING, started_at=started, ended_at=_now_iso()
        )
    # 失联 NFS 上 statfs 可能长期阻塞；每个 df 必须有界返回。
    ec1, out1, err1 = _run(["df", "-hP"], timeout=15)
    ec2, out2, err2 = _run(["df", "-iP"], timeout=15)
    if ec1 != 0 or ec2 != 0:
        return ProviderResult(
            source="df",
            status=STATUS_CMD_FAILED,
            started_at=started,
            ended_at=_now_iso(),
            exit_code=ec1 if ec1 != 0 else ec2,
            stderr=err1 + err2,
            error="one or more df commands failed",
            raw=out1 + "\n--- inodes ---\n" + out2,
            parsed={
                "filesystems": _parse_df(out1, inode=False)
                + _parse_df(out2, inode=True),
                "partial": [
                    message
                    for code, message in ((ec1, err1), (ec2, err2))
                    if code != 0 and message
                ],
            },
        )
    space = _parse_df(out1, inode=False)
    inodes = _parse_df(out2, inode=True)
    # 按 mounted_on 合并
    by_mp: dict[str, dict[str, Any]] = {}
    for row in space:
        by_mp[row["mounted_on"]] = row
    for row in inodes:
        mp = row["mounted_on"]
        if mp in by_mp:
            by_mp[mp]["inodes"] = row.get("inodes")
            by_mp[mp]["iuse_percent"] = row.get("iuse_percent")
        else:
            by_mp[mp] = row
    filesystems = list(by_mp.values())
    status = STATUS_OK if filesystems else STATUS_EMPTY
    return ProviderResult(
        source="df",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        exit_code=ec1,
        stderr=(err1 + err2) if (ec1 or ec2) else "",
        raw=out1 + "\n--- inodes ---\n" + out2,
        parsed={
            "filesystems": filesystems,
            "partial": [
                message
                for code, message in ((ec1, err1), (ec2, err2))
                if code != 0 and message
            ],
        },
    )


def _parse_df(text: str, *, inode: bool) -> list[dict[str, Any]]:
    """解析 df -P 输出。inode=True 时解析 -iP。"""
    rows: list[dict[str, Any]] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for line in lines[1:]:
        f = line.split()
        if len(f) < 6:
            continue
        src = f[0]
        if src in PSEUDO_FS:
            continue
        # POSIX -P 固定前 5 列，挂载点是剩余整段；不能只取最后一个 token。
        mp = " ".join(f[5:])
        if inode:
            rows.append(
                {
                    "filesystem": src,
                    "inodes": f[1],
                    "iused": f[2],
                    "iavail": f[3],
                    "iuse_percent": f[4],
                    "mounted_on": mp,
                }
            )
        else:
            rows.append(
                {
                    "filesystem": src,
                    "size": f[1],
                    "used": f[2],
                    "avail": f[3],
                    "use_percent": f[4],
                    "mounted_on": mp,
                }
            )
    return rows


def _parse_rpc_nfs(content: str) -> dict[str, float]:
    """解析 /proc/net/rpc/nfs 的 rpc 行（客户端累计：calls / retrans）。

    形如：rpc <calls> <retrans> <authrefrefresh> ... 。返回 {calls, retrans}。
    """
    out = {"calls": 0.0, "retrans": 0.0}
    for line in content.splitlines():
        f = line.split()
        if len(f) >= 3 and f[0] == "rpc":
            try:
                out["calls"] = float(f[1])
                out["retrans"] = float(f[2])
            except ValueError:
                pass
            break
    return out


def collect_nfs(duration: float, pid: int | None = None) -> ProviderResult:
    """NFS 客户端统计，**窗内两次采样求差**（mountstats/rpc 均为累计值）。

    数据源：
      - /proc/self/mountstats：per-mount per-op 累计统计（RTT/execute/ops/transmissions），
        两次采样差值得到本次 workload 窗口的 avg_rtt_ms/avg_execute_ms/retrans/ops。
      - /proc/net/rpc/nfs：客户端累计 calls/retrans，两次采样差值。
      - nfsiostat：可选单次补充（原始文本，非窗内）。
    无 NFS 挂载或文件不存在时标 unsupported/missing，绝不输出"正常"。
    """
    started = _now_iso()
    # 先看是否有 NFS 挂载
    mounts_pr = collect_mounts(pid)
    if mounts_pr.status not in (STATUS_OK, STATUS_EMPTY):
        return ProviderResult(
            source="nfs",
            status=mounts_pr.status,
            started_at=started,
            ended_at=_now_iso(),
            error=f"mount discovery failed: {mounts_pr.error or mounts_pr.status}",
        )
    has_nfs = False
    if isinstance(mounts_pr.parsed, list):
        has_nfs = any(
            str(m.get("fstype", "")).startswith("nfs") for m in mounts_pr.parsed
        )
    if not has_nfs:
        return ProviderResult(
            source="nfs",
            status=STATUS_UNSUPPORTED,
            started_at=started,
            ended_at=_now_iso(),
            error="no nfs mount found",
        )

    # t0 采样
    proc_id = str(pid) if pid is not None else "self"
    mountstats_path = f"/proc/{proc_id}/mountstats"
    rpc_path = f"/proc/{proc_id}/net/rpc/nfs"
    ms0_content, ms0_err, ms0_st = _read_file(mountstats_path)
    rpc0_content, rpc0_err, rpc0_st = _read_file(rpc_path)
    ms0 = _parse_mountstats(ms0_content) if ms0_st == 0 else {}
    rpc0 = (
        _parse_rpc_nfs(rpc0_content) if rpc0_st == 0 else {"calls": 0.0, "retrans": 0.0}
    )

    time.sleep(max(0.1, duration))

    # t1 采样
    ms1_content, ms1_err, ms1_st = _read_file(mountstats_path)
    rpc1_content, rpc1_err, rpc1_st = _read_file(rpc_path)
    ms1 = _parse_mountstats(ms1_content) if ms1_st == 0 else {}
    rpc1 = (
        _parse_rpc_nfs(rpc1_content) if rpc1_st == 0 else {"calls": 0.0, "retrans": 0.0}
    )

    parsed: dict[str, Any] = {}
    raw_parts: list[str] = []
    nfs_status = STATUS_OK

    mount_metrics: list[dict[str, Any]] = []
    if ms0_st == 0 and ms1_st == 0:
        mount_metrics = _diff_mount_metrics(ms0, ms1)
        parsed["mount_metrics"] = mount_metrics
        raw_parts.append(f"--- {mountstats_path} (t1) ---\n" + ms1_content)
    else:
        parsed["mountstats_error"] = (
            f"t0(status={ms0_st}): {ms0_err or 'unavailable'}; "
            f"t1(status={ms1_st}): {ms1_err or 'unavailable'}"
        )
        nfs_status = STATUS_PERMISSION if 2 in (ms0_st, ms1_st) else STATUS_MISSING

    if rpc0_st == 0 and rpc1_st == 0:
        parsed["client_calls_delta"] = round(max(0.0, rpc1["calls"] - rpc0["calls"]), 3)
        parsed["client_retrans_delta"] = round(
            max(0.0, rpc1["retrans"] - rpc0["retrans"]), 3
        )
    else:
        parsed["client_stats_error"] = (
            f"t0(status={rpc0_st}): {rpc0_err or 'unavailable'}; "
            f"t1(status={rpc1_st}): {rpc1_err or 'unavailable'}"
        )

    # nfsiostat 可选补充（不强依赖，单次快照）
    if pid is None and _have_cmd("nfsiostat"):
        ec, out, err = _run(["nfsiostat"], timeout=15)
        if ec == 0 and out.strip():
            raw_parts.append("--- nfsiostat ---\n" + out)
            parsed["nfsiostat_raw"] = out
        else:
            parsed["nfsiostat_error"] = err or f"exit {ec}"

    if not mount_metrics and nfs_status == STATUS_OK:
        nfs_status = STATUS_EMPTY if rpc0_st != 0 else STATUS_OK

    return ProviderResult(
        source="nfs",
        status=nfs_status,
        started_at=started,
        ended_at=_now_iso(),
        raw="\n".join(raw_parts),
        parsed={
            **parsed,
            "target_pid": pid,
            "mount_namespace": _mount_namespace_key(pid),
        },
        error=str(parsed.get("mountstats_error") or ""),
    )


# per-op 段起始标记：内核实际输出 "per-op statistics"（无冒号），部分文档/旧版用 "per-op:"。
# Review P1-1：旧实现只认 "per-op:"，导致真实 NFS 环境漏解析全部 per-op 数据。
_PEROP_SECTION_MARKERS = ("per-op", "per-op:")
# 远程元数据相关操作（R300）：高延迟反映 open/stat/lookup/readdir 的元数据/远程访问开销。
_METADATA_OPS = (
    "GETATTR",
    "SETATTR",
    "LOOKUP",
    "READLINK",
    "READDIR",
    "READDIRPLUS",
    "ACCESS",
    "FSSTAT",
    "FSINFO",
    "PATHCONF",
    "CREATE",
    "MKDIR",
    "SYMLINK",
    "MKNOD",
    "REMOVE",
    "RMDIR",
    "RENAME",
    "LINK",
    "OPEN",
    "OPEN_CONFIRM",
    "OPEN_NOATTR",
    "CLOSE",
    "LOCK",
    "LOCKT",
    "LOCKU",
    "DELEGRETURN",
    "LAYOUTGET",
    "LAYOUTRETURN",
)
# 数据读写操作（R200 带宽/重传相关）。
_DATA_OPS = ("READ", "WRITE", "READDATA", "WRITEDATA")


def _new_mount_acc(mp: str, source: str, fstype: str) -> dict[str, Any]:
    return {
        "mount_point": mp,
        "source": source,
        "fstype": fstype,
        # 全量累计（所有 op 求和）
        "ops": 0,
        "transmissions": 0,
        "major_timeouts": 0,
        "sum_rtt_ms": 0.0,
        "sum_execute_ms": 0.0,
        "bytes_read": 0,
        "bytes_write": 0,
        # 元数据 op 子集累计（R300 远程访问耗时证据）
        "metadata_ops": 0,
        "metadata_sum_rtt_ms": 0.0,
        "metadata_sum_execute_ms": 0.0,
        # 数据 op 子集累计（R200 带宽/重传证据）
        "data_ops": 0,
        "data_transmissions": 0,
        "data_sum_rtt_ms": 0.0,
        "data_sum_execute_ms": 0.0,
    }


MountIdentity = tuple[str, str, str]


def _parse_mountstats(text: str) -> dict[MountIdentity, dict[str, Any]]:
    """解析 /proc/self/mountstats，按 (source, mount_point, fstype) 保存累计统计。

    头行格式（关键字位置固定，见 prometheus/procfs parseMount）：
        device <src> mounted on <mp> with fstype <type> statvers=<ver>
        字段：[0]device [1]src [2]mounted [3]on [4]mp [5]with [6]fstype [7]type [8]statvers
    per-op 段起始：内核实际输出 "per-op statistics"（无冒号），兼容 "per-op:"/"per-op"。
    per-op 数据行（>=9 列）：
        <OP>: <requests> <transmissions> <major_timeouts> <bytes_sent> <bytes_recv>
              <queue_ms> <rtt_ms> <execute_ms> [<errors>]
        RTT/execute 为**累计毫秒**（statvers 1.1，现代内核）；重传 = transmissions - requests。
    保留全量 + 元数据子集（GETATTR/LOOKUP/READDIR/...）+ 数据子集（READ/WRITE）三类累计。
    累计值不反映本次 workload 窗口，需配合两次采样差值（见 _diff_mount_metrics）。
    """
    mounts: dict[MountIdentity, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    in_per_op = False
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            in_per_op = False
            continue
        # 设备头行（关键字位置校验，避免误匹配数据行）
        if (
            len(fields) >= 8
            and fields[0] == "device"
            and fields[2] == "mounted"
            and fields[3] == "on"
            and fields[5] == "with"
            and fields[6] == "fstype"
        ):
            fstype = fields[7]
            if not (fstype == "nfs" or fstype.startswith("nfs")):
                current = None
                in_per_op = False
                continue
            mp = _decode_mountinfo_octal(fields[4])
            source = _decode_mountinfo_octal(fields[1])
            identity = (source, mp, fstype)
            current = mounts.setdefault(identity, _new_mount_acc(mp, source, fstype))
            in_per_op = False
            continue
        if current is None:
            continue
        # per-op 段起始标记（兼容内核 "per-op statistics" / "per-op:" / "per-op"）
        if fields[0] in _PEROP_SECTION_MARKERS or (
            len(fields) >= 2 and fields[0] == "per-op" and fields[1] == "statistics"
        ):
            in_per_op = True
            continue
        if fields[0] == "bytes:" and len(fields) >= 6:
            try:
                current["bytes_read"] = int(fields[1])
                current["bytes_write"] = int(fields[2])
            except ValueError:
                pass
            continue
        if in_per_op and fields[0].endswith(":"):
            # per-op 数据行：OP: req tx major bs br queue rtt exec [err]
            nums: list[float] = []
            ok = True
            for tok in fields[1:]:
                try:
                    nums.append(float(tok))
                except ValueError:
                    ok = False
                    break
            if not ok or len(nums) < 8:
                continue
            op = fields[0].upper().rstrip(":")
            req, tx, major, _bs, _br, _queue, rtt, execute = nums[:8]
            current["ops"] += req
            current["transmissions"] += tx
            current["major_timeouts"] += major
            current["sum_rtt_ms"] += rtt
            current["sum_execute_ms"] += execute
            if op in _METADATA_OPS:
                current["metadata_ops"] += req
                current["metadata_sum_rtt_ms"] += rtt
                current["metadata_sum_execute_ms"] += execute
            elif op in _DATA_OPS:
                current["data_ops"] += req
                current["data_transmissions"] += tx
                current["data_sum_rtt_ms"] += rtt
                current["data_sum_execute_ms"] += execute
    return mounts


def _diff_mount_metrics(
    prev: dict[MountIdentity, dict], cur: dict[MountIdentity, dict]
) -> list[dict[str, Any]]:
    """对两次累计 mountstats 求窗内差值，返回 per-mount 窗内性能指标列表。

    单次调用（prev 为空）时退化为累计值（仅 mount_point/source/fstype 可信，性能值标注 cumulative）。
    """
    out: list[dict[str, Any]] = []
    for identity, cur_m in cur.items():
        prev_m = prev.get(identity, {})
        same_identity = bool(prev_m) and (
            str(prev_m.get("source") or "") == str(cur_m.get("source") or "")
            and str(prev_m.get("fstype") or "") == str(cur_m.get("fstype") or "")
        )
        if not same_identity:
            prev_m = {}

        counter_keys = (
            "ops",
            "transmissions",
            "major_timeouts",
            "sum_rtt_ms",
            "sum_execute_ms",
            "metadata_ops",
            "metadata_sum_rtt_ms",
            "metadata_sum_execute_ms",
            "data_ops",
            "data_transmissions",
            "data_sum_rtt_ms",
            "data_sum_execute_ms",
            "bytes_read",
            "bytes_write",
        )
        counter_reset = False
        if prev_m:
            try:
                counter_reset = any(
                    float(cur_m.get(key, 0)) < float(prev_m.get(key, 0))
                    for key in counter_keys
                )
            except (TypeError, ValueError):
                counter_reset = True
        windowed = (
            "counter_reset" if counter_reset else ("delta" if prev_m else "cumulative")
        )

        def d(key):
            if counter_reset:
                return 0.0
            try:
                return float(cur_m.get(key, 0)) - float(prev_m.get(key, 0))
            except (TypeError, ValueError):
                return 0.0

        ops = d("ops")
        tx = d("transmissions")
        retrans = max(0.0, tx - ops)
        sum_rtt = d("sum_rtt_ms")
        sum_exec = d("sum_execute_ms")
        avg_rtt = (sum_rtt / ops) if ops > 0 else 0.0
        avg_exec = (sum_exec / ops) if ops > 0 else 0.0
        # 元数据子集（GETATTR/LOOKUP/READDIR/...）—— R300 远程访问耗时证据
        m_ops = d("metadata_ops")
        m_rtt = d("metadata_sum_rtt_ms")
        m_exec = d("metadata_sum_execute_ms")
        avg_meta_rtt = (m_rtt / m_ops) if m_ops > 0 else 0.0
        avg_meta_exec = (m_exec / m_ops) if m_ops > 0 else 0.0
        # 数据子集（READ/WRITE）—— R200 带宽/重传证据
        d_ops = d("data_ops")
        d_tx = d("data_transmissions")
        d_retrans = max(0.0, d_tx - d_ops)
        d_rtt = d("data_sum_rtt_ms")
        d_exec = d("data_sum_execute_ms")
        avg_data_rtt = (d_rtt / d_ops) if d_ops > 0 else 0.0
        avg_data_exec = (d_exec / d_ops) if d_ops > 0 else 0.0
        out.append(
            {
                "mount_point": cur_m.get("mount_point"),
                "source": cur_m.get("source"),
                "fstype": cur_m.get("fstype"),
                "windowing": windowed,
                "ops": round(ops, 3),
                "transmissions": round(tx, 3),
                "retrans": round(retrans, 3),
                "retrans_ratio": round(retrans / ops, 6) if ops > 0 else 0.0,
                "major_timeouts": round(d("major_timeouts"), 3),
                "sum_rtt_ms": round(sum_rtt, 3),
                "sum_execute_ms": round(sum_exec, 3),
                "avg_rtt_ms": round(avg_rtt, 3),
                "avg_execute_ms": round(avg_exec, 3),
                # 元数据子集
                "metadata_ops": round(m_ops, 3),
                "metadata_sum_rtt_ms": round(m_rtt, 3),
                "avg_metadata_rtt_ms": round(avg_meta_rtt, 3),
                "avg_metadata_execute_ms": round(avg_meta_exec, 3),
                # 数据子集
                "data_ops": round(d_ops, 3),
                "data_transmissions": round(d_tx, 3),
                "data_retrans": round(d_retrans, 3),
                "data_retrans_ratio": (
                    round(d_retrans / d_ops, 6) if d_ops > 0 else 0.0
                ),
                "avg_data_rtt_ms": round(avg_data_rtt, 3),
                "avg_data_execute_ms": round(avg_data_exec, 3),
                "bytes_read_delta": round(d("bytes_read"), 3),
                "bytes_write_delta": round(d("bytes_write"), 3),
            }
        )
    return out


def collect_memory() -> ProviderResult:
    """/proc/meminfo。"""
    started = _now_iso()
    content, err, st = _read_file("/proc/meminfo")
    if st == 1:
        return ProviderResult(
            source="memory",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    if st == 2:
        return ProviderResult(
            source="memory",
            status=STATUS_PERMISSION,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    parsed: dict[str, float] = {}
    for line in content.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            parsed[m.group(1).lower()] = float(m.group(2))
    status = STATUS_OK if parsed else STATUS_PARSE_FAILED
    return ProviderResult(
        source="memory",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        raw=content,
        parsed=parsed,
    )


def collect_readahead_scheduler() -> tuple[dict[str, int], dict[str, str], list[str]]:
    """readahead（blockdev --getra）与 IO 调度器（/sys/block/<dev>/queue/scheduler）。

    需 root/读权限，失败收集到 partial 列表。仅对物理主设备采集（已用 _is_real_block_device 过滤）。
    readahead 优先读 blockdev；blockdev 不可用/失败时降级读 sysfs `read_ahead_kb` 并换算为 512B sector。
    """
    readahead: dict[str, int] = {}
    scheduler: dict[str, str] = {}
    partial: list[str] = []
    devices = _list_block_devices()

    def _read_sysfs_readahead(dev: str) -> int | None:
        content, _, st = _read_file(f"/sys/block/{dev}/queue/read_ahead_kb")
        if st != 0:
            return None
        try:
            # sysfs 单位为 KiB；对齐 schema 的 512B sector 语义。
            return int(float(content.strip())) * 2
        except ValueError:
            return None

    have_blockdev = _have_cmd("blockdev")
    for dev in devices:
        blockdev_err = ""
        if have_blockdev:
            ec, out, _ = _run(["blockdev", "--getra", f"/dev/{dev}"])
            if ec == 0:
                try:
                    readahead[f"/dev/{dev}"] = int(out.strip())
                    continue
                except ValueError:
                    blockdev_err = "parse failed"
            else:
                blockdev_err = f"exit {ec}"
        sysfs_value = _read_sysfs_readahead(dev)
        if sysfs_value is not None:
            readahead[f"/dev/{dev}"] = sysfs_value
        else:
            if blockdev_err:
                partial.append(
                    f"readahead {dev}: blockdev {blockdev_err}; sysfs fallback unavailable"
                )
            else:
                partial.append(f"readahead {dev}: sysfs unavailable")
    for dev in devices:
        sch_file = f"/sys/block/{dev}/queue/scheduler"
        content, _, st = _read_file(sch_file)
        if st == 0:
            scheduler[f"/dev/{dev}"] = content.strip()
        # 无权限/不存在则跳过（不报 partial，scheduler 仅为辅助）
    return readahead, scheduler, partial


# --- 主流程 --------------------------------------------------------------


def collect(duration: float, pid: int | None, path: str | None) -> IoSnapshot:
    """并发采集所有 provider，组装 IoSnapshot。"""
    window_start = _now_iso()

    # 并发采集动态指标（同窗）
    dynamic: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(collect_block_devices, duration): "block",
            ex.submit(collect_iostat, duration): "iostat",
            ex.submit(collect_pidstat, duration): "pidstat",
            ex.submit(collect_nfs, duration, pid): "nfs",
            ex.submit(collect_process_io_map, pid, path, duration): "pmap",
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                dynamic[key] = fut.result()
            except Exception as e:  # noqa: BLE001
                source = {
                    "block": "block_devices",
                    "iostat": "iostat",
                    "pidstat": "pidstat",
                    "nfs": "nfs",
                    "pmap": "process_io_map",
                }[key]
                crashed = ProviderResult(
                    source=source,
                    status=STATUS_CMD_FAILED,
                    started_at=window_start,
                    ended_at=_now_iso(),
                    error=f"collector crashed: {type(e).__name__}: {e}",
                )
                dynamic[key] = (crashed, []) if key == "block" else crashed

    # 静态/快速采集（顺序，开销小）。单个 provider 的实现异常也必须隔离。
    def _safe_provider(source: str, func, *args) -> ProviderResult:
        started_at = _now_iso()
        try:
            result = func(*args)
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(
                source=source,
                status=STATUS_CMD_FAILED,
                started_at=started_at,
                ended_at=_now_iso(),
                error=f"collector crashed: {type(exc).__name__}: {exc}",
            )
        if isinstance(result, ProviderResult):
            return result
        return ProviderResult(
            source=source,
            status=STATUS_CMD_FAILED,
            started_at=started_at,
            ended_at=_now_iso(),
            error="collector returned an invalid result",
        )

    mounts_pr = _safe_provider("mounts", collect_mounts, pid)
    df_pr = _safe_provider("df", collect_df, pid)
    mem_pr = _safe_provider("memory", collect_memory)
    try:
        readahead, scheduler, partial = collect_readahead_scheduler()
        if not (
            isinstance(readahead, dict)
            and isinstance(scheduler, dict)
            and isinstance(partial, list)
        ):
            raise TypeError("collector returned an invalid result")
    except Exception as exc:  # noqa: BLE001
        readahead, scheduler = {}, {}
        partial = [
            f"readahead_scheduler: collector crashed: {type(exc).__name__}: {exc}"
        ]

    window_end = _now_iso()

    # 拆解动态结果
    block_result = dynamic.get("block")
    if isinstance(block_result, tuple) and len(block_result) == 2:
        block_pr, diskstats_samples = block_result
    else:
        block_pr, diskstats_samples = None, []
    if not isinstance(block_pr, ProviderResult) or not isinstance(
        diskstats_samples, list
    ):
        block_pr = ProviderResult(
            source="block_devices",
            status=STATUS_CMD_FAILED,
            error="collector returned an invalid result",
        )
        diskstats_samples = []
    iostat_pr = dynamic.get("iostat", ProviderResult(source="iostat"))
    pidstat_pr = dynamic.get("pidstat", ProviderResult(source="pidstat"))
    nfs_pr = dynamic.get("nfs", ProviderResult(source="nfs"))
    pmap_pr = dynamic.get("pmap", ProviderResult(source="process_io_map"))

    # availability 汇总
    availability = Availability(partial=partial)
    all_providers = {
        "block_devices": block_pr,
        "iostat": iostat_pr,
        "pidstat": pidstat_pr,
        "process_io_map": pmap_pr,
        "memory": mem_pr,
        "df": df_pr,
        "nfs": nfs_pr,
        "mounts": mounts_pr,
    }
    for name, pr in all_providers.items():
        if not isinstance(pr, ProviderResult):
            availability.errors.append(f"{name}: collector crashed")
            continue
        if pr.status == STATUS_MISSING:
            availability.missing.append(name)
        elif pr.status in (STATUS_PERMISSION, STATUS_CMD_FAILED, STATUS_PARSE_FAILED):
            availability.errors.append(
                f"{name}: {pr.status}" + (f" ({pr.error})" if pr.error else "")
            )
        elif pr.status == STATUS_EMPTY:
            availability.partial.append(f"{name}: empty")
        elif pr.status == STATUS_UNSUPPORTED:
            availability.partial.append(f"{name}: unsupported")

    # host 信息
    host: dict[str, Any] = {}
    try:
        import platform

        host = {
            "hostname": platform.node(),
            "kernel": platform.release(),
            "platform": platform.platform(),
        }
    except Exception:  # noqa: BLE001
        host = {}

    snapshot = IoSnapshot(
        schema_version=SCHEMA_VERSION,
        collected_at=window_start,
        host=host,
        duration_seconds=duration,
        window={"start": window_start, "end": window_end},
        target={"pid": pid, "path": path},
        mounts=mounts_pr.parsed if isinstance(mounts_pr.parsed, list) else [],
        mounts_provider=mounts_pr,
        diskstats_sample=diskstats_samples,
        block_devices=block_pr,
        iostat=iostat_pr
        if isinstance(iostat_pr, ProviderResult)
        else ProviderResult(source="iostat", status=STATUS_CMD_FAILED),
        pidstat=pidstat_pr
        if isinstance(pidstat_pr, ProviderResult)
        else ProviderResult(source="pidstat", status=STATUS_CMD_FAILED),
        process_io_map=pmap_pr
        if isinstance(pmap_pr, ProviderResult)
        else ProviderResult(source="process_io_map", status=STATUS_CMD_FAILED),
        memory=mem_pr,
        df=df_pr,
        nfs=nfs_pr
        if isinstance(nfs_pr, ProviderResult)
        else ProviderResult(source="nfs", status=STATUS_CMD_FAILED),
        readahead=readahead,
        scheduler=scheduler,
        availability=availability,
    )
    return snapshot


def write_snapshot(snapshot: IoSnapshot, out_path: str) -> int:
    """原子写入：先写 .tmp，pydantic 校验通过后 os.replace。失败返回非零。

    返回 0 成功，非零失败（已向 stderr 输出原因）。
    """
    # 序列化（pydantic 默认序列化自身模型）
    try:
        data = json.loads(snapshot.model_dump_json())
    except Exception as e:  # noqa: BLE001
        print(f"错误: 序列化 Snapshot 失败: {e}", file=sys.stderr)
        return 1

    out = Path(out_path)
    parent = out.parent
    # 第八轮 P2-3：临时名含 PID+随机串，避免并发任务写同一输出时碰撞。
    tmp = Path(_temp_name(str(out)))
    success = False
    try:
        if str(parent) and not parent.exists():
            print(f"错误: 输出目录不存在: {parent}", file=sys.stderr)
            return 1
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 重新校验：读回再过一次 pydantic
        with open(tmp, encoding="utf-8") as f:
            IoSnapshot.model_validate(json.load(f))
        os.replace(tmp, out)
        success = True
    except PermissionError as e:
        print(f"错误: 无写入权限: {out}: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"错误: 写入失败: {out}: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - schema 校验失败等
        print(f"错误: Snapshot 校验失败: {e}", file=sys.stderr)
        return 1
    finally:
        # 第七轮 P2-4：任何失败路径都清理残留 .tmp（replace 成功后 tmp 已不存在）。
        if not success:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Host 侧 IO Snapshot 只读采集器（mindstudio-storage-analysis）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=int,
        default=30,
        help="采样时长（整数秒），默认 30，建议 >= 10（动态指标同窗并发采集）",
    )
    parser.add_argument(
        "-o", "--out", default="", help="输出 JSON 文件路径；省略则输出到 stdout"
    )
    parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="目标进程 PID（用于建立 PID→设备映射，R400）",
    )
    parser.add_argument(
        "--path", default=None, help="目标数据集/挂载点路径（用于建立映射）"
    )
    args = parser.parse_args(argv)

    # 第八轮 P1-2：type=int 已在 argparse 层拒绝 float/NaN/Inf；再校验范围。
    eff = parse_interval(args.duration)
    if eff is None:
        print(
            f"错误: --duration 非法（需 1..86400 的整数秒）: {args.duration!r}",
            file=sys.stderr,
        )
        return 2
    if args.pid is not None and args.pid <= 0:
        print(f"错误: --pid 必须是正整数: {args.pid!r}", file=sys.stderr)
        return 2
    if args.path:
        if not os.path.isabs(args.path):
            print(f"错误: --path 必须是绝对路径: {args.path!r}", file=sys.stderr)
            return 2
        args.path = os.path.normpath(args.path)
        if args.pid is None and os.path.exists(args.path):
            args.path = os.path.realpath(args.path)

    snapshot = collect(eff, args.pid, args.path)

    if args.out:
        rc = write_snapshot(snapshot, args.out)
        if rc == 0:
            print(f"Snapshot 已写入: {args.out}", file=sys.stderr)
        return rc
    else:
        print(
            json.dumps(
                json.loads(snapshot.model_dump_json()), ensure_ascii=False, indent=2
            )
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
