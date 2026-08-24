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
"""Collector contracts, bounded command execution, and shared mount helpers."""

from __future__ import annotations

import math
import os
import re
import shutil
import signal
import subprocess
import time
import threading
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = "1.5"  # major.minor；minor 只增可选字段，major 变更需分析器显式适配


SUPPORTED_MAJOR = 1


STATUS_OK = "ok"


STATUS_MISSING = "missing"  # 命令/文件不存在


STATUS_PERMISSION = "permission_denied"  # 无权限


STATUS_CMD_FAILED = "command_failed"  # 命令存在但非零退出


STATUS_PARSE_FAILED = "parse_failed"  # 解析失败


STATUS_EMPTY = "empty"  # 命令成功但无输出或无受支持采集对象


STATUS_UNSUPPORTED = "unsupported"  # 平台/工具不支持（如 Lustre 工具未安装）


ProviderStatus = Literal[
    "ok",
    "missing",
    "permission_denied",
    "command_failed",
    "parse_failed",
    "empty",
    "unsupported",
]


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


_IGNORED_BLOCK_DEVICE = re.compile(r"^(?:loop|ram|zram)\d+$")


_MAX_PROCESS_TREE_PIDS = 256


_MAX_CHILDREN_READ_BYTES = 64 * 1024


_MAX_MOUNT_NAMESPACES_PER_OBSERVATION = 16


_MAX_OPEN_FILE_RECORDS = 256


_FD_SCAN_BUDGET_SECONDS = 1.0


_PROCESS_MAP_FD_BUDGET_SECONDS = 5.0


_STATIC_PROBE_TIMEOUT_SECONDS = 5.0


_READAHEAD_PROBE_BUDGET_SECONDS = 5.0


_DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0


_MAX_COMMAND_STDOUT_BYTES = 8 * 1024 * 1024


_MAX_COMMAND_STDERR_BYTES = 256 * 1024


_COMMAND_DIAGNOSTIC_BYTES = 64 * 1024


_MAX_FILE_READ_BYTES = 2 * 1024 * 1024


_OUTPUT_LIMIT_EXIT_CODE = 125


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
    mounts_provider: ProviderResult = Field(default_factory=lambda: ProviderResult(source="mounts"))
    diskstats_sample: list[DiskStatSample] = Field(default_factory=list)
    block_devices: ProviderResult = Field(default_factory=lambda: ProviderResult(source="block_devices"))
    iostat: ProviderResult = Field(default_factory=lambda: ProviderResult(source="iostat"))
    pidstat: ProviderResult = Field(default_factory=lambda: ProviderResult(source="pidstat"))
    process_io_map: ProviderResult = Field(default_factory=lambda: ProviderResult(source="process_io_map"))
    memory: ProviderResult = Field(default_factory=lambda: ProviderResult(source="memory"))
    df: ProviderResult = Field(default_factory=lambda: ProviderResult(source="df"))
    nfs: ProviderResult = Field(default_factory=lambda: ProviderResult(source="nfs"))
    glusterfs: ProviderResult = Field(default_factory=lambda: ProviderResult(source="glusterfs"))
    readahead: dict[str, int] = Field(default_factory=dict)
    scheduler: dict[str, str] = Field(default_factory=dict)
    availability: Availability = Field(default_factory=Availability)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if not re.match(r"^\d+\.\d+$", str(v)):
            raise ValueError(f"schema_version 必须是 <major>.<minor> 格式，得到 {v!r}")
        return v


def _now_iso() -> str:
    # 快速 provider（如 process_io_map）可能不足 1 秒；保留微秒避免 start=end
    # 被分析器误判为无效窗口。
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


def _have_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def parse_interval(duration: Any) -> int | None:
    """把 duration 规范为整数秒；非法返回 None。

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
    """生成同目录下含 PID 和随机串的唯一临时文件名。"""
    import uuid

    d = os.path.dirname(path)
    base = os.path.basename(path)
    return os.path.join(d or ".", f".{base}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _run_bounded(cmd: list[str], *, timeout: float | None, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a command with bounded pipe capture so long samples cannot exhaust memory."""
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except PermissionError as e:
        return 126, "", f"{cmd[0]}: permission denied: {e}"
    except Exception as e:  # noqa: BLE001
        return 1, "", f"{cmd[0]}: {type(e).__name__}: {e}"

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": _MAX_COMMAND_STDOUT_BYTES, "stderr": _MAX_COMMAND_STDERR_BYTES}
    lock = threading.Lock()
    output_exceeded: list[str] = []

    def _drain(name: str, stream: Any) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            with lock:
                if output_exceeded:
                    continue
                buffer = captured[name]
                if len(buffer) + len(chunk) <= limits[name]:
                    buffer.extend(chunk)
                    continue
                diagnostic_limit = min(_COMMAND_DIAGNOSTIC_BYTES, limits[name])
                del buffer[diagnostic_limit:]
                remaining = diagnostic_limit - len(buffer)
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
                output_exceeded.append(name)

    stdout_thread = threading.Thread(target=_drain, args=("stdout", process.stdout), daemon=True)
    stderr_thread = threading.Thread(target=_drain, args=("stderr", process.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def _stop_process(force: bool = False) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    deadline = time.monotonic() + timeout if timeout is not None else None
    stop_reason: str | None = None
    stop_started: float | None = None
    while process.poll() is None:
        with lock:
            exceeded_stream = output_exceeded[0] if output_exceeded else None
        now = time.monotonic()
        if stop_reason is None and exceeded_stream is not None:
            stop_reason = f"{exceeded_stream} output exceeded its byte budget"
            stop_started = now
            _stop_process()
        elif stop_reason is None and deadline is not None and now >= deadline:
            stop_reason = f"timed out after {timeout:g}s"
            stop_started = now
            _stop_process()
        elif stop_started is not None and now - stop_started >= 1.0:
            _stop_process(force=True)
        time.sleep(0.02)

    stdout_thread.join()
    stderr_thread.join()
    process.stdout.close()
    process.stderr.close()
    with lock:
        exceeded_stream = output_exceeded[0] if output_exceeded else None
    if exceeded_stream is not None and stop_reason is None:
        stop_reason = f"{exceeded_stream} output exceeded its byte budget"

    stdout = captured["stdout"].decode("utf-8", errors="replace")
    stderr = captured["stderr"].decode("utf-8", errors="replace")
    if exceeded_stream is not None:
        marker = "\n[truncated: command output budget exceeded]"
        if exceeded_stream == "stdout":
            stdout += marker
        else:
            stderr += marker
        return (
            _OUTPUT_LIMIT_EXIT_CODE,
            stdout,
            f"{cmd[0]}: {stop_reason}; process terminated",
        )
    if stop_reason is not None:
        return 124, stdout, f"{cmd[0]}: {stop_reason}"
    return process.returncode, stdout, stderr


def _run(cmd: list[str], *, timeout: float | None = _DEFAULT_COMMAND_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """运行外部命令，返回有界 (exit_code, stdout, stderr)。"""
    return _run_bounded(cmd, timeout=timeout)


def _read_file(path: str) -> tuple[str, str, int]:
    """读文件，返回 (content, error, status_code_int)。status: 0=ok, 1=missing, 2=perm, 3=other"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read(_MAX_FILE_READ_BYTES + 1)
            if len(content) > _MAX_FILE_READ_BYTES:
                return "", f"{path}: exceeds {_MAX_FILE_READ_BYTES} byte read budget", 3
            return content, "", 0
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


def _is_block_device_candidate(name: Any) -> bool:
    """Accept kernel block-device names while excluding explicit pseudo devices."""
    return bool(isinstance(name, str) and name and "/" not in name and not _IGNORED_BLOCK_DEVICE.fullmatch(name))


def _run_with_env(
    cmd: list[str],
    env: dict[str, str],
    timeout: float | None = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """运行固定 locale 的外部命令，并使用相同的有界捕获。"""
    return _run_bounded(cmd, env=env, timeout=timeout)


def _strict_finite_float(value: Any) -> float | None:
    """Accept only scalar finite numeric values from untrusted command JSON."""
    if isinstance(value, bool) or isinstance(value, (list, dict, tuple, set)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _finite_mean(values: list[float]) -> float | None:
    """Return an overflow-safe mean of finite values."""
    if not values or any(not math.isfinite(value) for value in values):
        return None
    count = len(values)
    try:
        result = math.fsum(value / count for value in values)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _finite_weighted_mean(values: list[float], weights: list[float]) -> float | None:
    """Return an overflow-safe weighted mean with non-negative finite weights."""
    if len(values) != len(weights) or not values:
        return None
    if any(not math.isfinite(value) for value in values) or any(
        not math.isfinite(weight) or weight < 0 for weight in weights
    ):
        return None
    scale = max(weights)
    if scale == 0:
        return _finite_mean(values)
    scaled = [weight / scale for weight in weights]
    try:
        denominator = math.fsum(scaled)
        result = math.fsum(value * (weight / denominator) for value, weight in zip(values, scaled))
    except (OverflowError, ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) else None


def _to_float(v: Any) -> float:
    return _strict_finite_float(v) or 0.0


def _strict_text_rate(fields: list[str], idx: int | None) -> float | None:
    """Allow an absent auxiliary column, but reject a missing/invalid present token."""
    if idx is None:
        return 0.0
    if idx >= len(fields):
        return None
    return _strict_finite_float(fields[idx])


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


def _resolve_canonical_device(
    major_minor: str,
    source: str,
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把挂载源归一到 iostat 报告的整盘或逻辑主设备名。

    策略（与 R100 iostat 设备名可 join）：
      - major:minor → /sys/dev/block/<mm> realpath → 父目录判断；分区折叠到整盘
        （/dev/sda1 → sda），整盘/dm/md 取 sysfs 叶子 basename（dm-0/md0/nvme0n1）。
      - dm/md 保留为独立 canonical（iostat 在 dm-0/md0 上报告饱和），另记录 backing_devices。
    无 /sys（如容器只读视图缺失）时退化为名称启发式并标注 device_resolution="heuristic"。

    返回 {canonical_device, major_minor, backing_devices, device_resolution}。
    """
    observation_cache = cache if cache is not None else {}
    cache_key = f"{major_minor}|{source}"
    if cache_key in observation_cache:
        return observation_cache[cache_key]
    result = _resolve_canonical_device_impl(major_minor, source)
    observation_cache[cache_key] = result
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
    if not _is_block_device_candidate(canonical):
        canonical = leaf if _is_block_device_candidate(leaf) else source
    backing: list[str] = []
    # dm/md backing 链（slaves 列出底层物理盘，仅记录不折叠）
    slaves_dir = f"/sys/dev/block/{major_minor}/slaves"
    try:
        for s in os.listdir(slaves_dir):
            if _is_block_device_candidate(s):
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
        r"^/dev/((?:nvme\d+n\d+|mmcblk\d+|rbd\d+|nbd\d+|"
        r"sd[a-z]+|vd[a-z]+|hd[a-z]+|xvd[a-z]+))(?:p?\d+)?$",
        src,
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


def _cached_mountinfo_table(
    pid: int | None = None,
    cache: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], str, bool]:
    """Cache mountinfo within one observation for PIDs sharing a namespace."""
    observation_cache = cache if cache is not None else {}
    key = _mount_namespace_key(pid)
    if key not in observation_cache:
        if len(observation_cache) >= _MAX_MOUNT_NAMESPACES_PER_OBSERVATION:
            return [], key, True
        table = _mountinfo_table(pid)
        # 若目标与采集器明确处于同一 namespace，可安全退化到 self 视图。
        if not table and pid is not None and key == _mount_namespace_key(None):
            table = _mountinfo_table(None)
        observation_cache[key] = table
    return observation_cache[key], key, False


def _resolve_path_to_mount(
    path: str,
    pid: int | None = None,
    mnt_id: int | None = None,
    mountinfo_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """在目标 PID 的 mount namespace 中把路径解析到挂载点。

    findmnt 必须用 -T/--target 才能查找包含路径的挂载；把 path 当
    source 传入，对普通文件路径无效。
    """
    table, namespace, _budget_exhausted = _cached_mountinfo_table(pid, mountinfo_cache)
    if mnt_id is not None:
        exact = next((entry for entry in table if entry.get("mount_id") == mnt_id), None)
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
        # fdinfo supplied an exact identity. Falling back to a path prefix could
        # bind a stale/replaced mount to this FD and must not certify the mapping.
        return None
    # mountinfo 最长前缀匹配
    norm = path.rstrip("/") or "/"
    for e in table:
        mp = e["mount_point"]
        if norm == mp or norm.startswith(mp.rstrip("/") + "/") or (mp == "/" and norm.startswith("/")):
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
        exit_code, out, _ = _run(["findmnt", "-n", "-r", "-T", path, "-o", "TARGET,SOURCE,FSTYPE"])
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


def _merge_mapping_observation(previous: dict[str, Any], current: dict[str, Any], fallback_end: str) -> dict[str, Any]:
    """Merge a second observation of the same PID/path/mount mapping."""
    merged = dict(previous)
    merged["last_seen"] = current.get("last_seen") or fallback_end
    try:
        previous_count = int(previous.get("observation_count") or 1)
        current_count = int(current.get("observation_count") or 1)
    except (TypeError, ValueError, OverflowError):
        previous_count = current_count = 1
    merged["observation_count"] = max(1, previous_count) + max(1, current_count)
    return merged


def _mapping_observation_key(mapping: dict[str, Any]) -> tuple[Any, ...]:
    """Return the stable identity that must match before observations are merged."""
    backing = mapping.get("backing_devices")
    backing_key = tuple(sorted(str(item) for item in backing)) if isinstance(backing, list) else ()
    return (
        mapping.get("pid"),
        mapping.get("boot_id"),
        mapping.get("pid_starttime_ticks"),
        mapping.get("path"),
        mapping.get("mount_namespace"),
        mapping.get("mount_id"),
        mapping.get("mount_point"),
        mapping.get("source"),
        mapping.get("fstype"),
        mapping.get("major_minor"),
        mapping.get("canonical_device"),
        backing_key,
        mapping.get("device_resolution"),
        mapping.get("mount_binding"),
    )
