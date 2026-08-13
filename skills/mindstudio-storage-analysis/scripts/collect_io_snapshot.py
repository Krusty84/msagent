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
  - 原子写入：先写 *.tmp，collector envelope 的 pydantic 校验通过后 os.replace 重命名；父目录不存在或写入失败
    时向 stderr 输出原因并以非零退出码返回。

用法:
    python3 collect_io_snapshot.py --duration 30 --out io_snapshot.json
    python3 collect_io_snapshot.py -d 15 -o io_snapshot.json --pid 12345 --path /data

退出码：
    0  成功，输出文件已写入并通过 collector envelope 校验
    2  参数错误
    1  其它失败（采集本身仍尽量完整，但写入/校验失败）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time  # noqa: F401 - retained for existing callers that patch collector timing
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Preserve the historical import surface while the implementation lives in modules.
from _collection.common import (  # noqa: F401
    Availability,
    DiskStat,
    DiskStatSample,
    IoSnapshot,
    PSEUDO_FS,
    ProviderResult,
    ProviderStatus,
    SCHEMA_VERSION,
    STATUS_CMD_FAILED,
    STATUS_EMPTY,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    STATUS_PERMISSION,
    STATUS_UNSUPPORTED,
    SUPPORTED_MAJOR,
    _COMMAND_DIAGNOSTIC_BYTES,
    _DEFAULT_COMMAND_TIMEOUT_SECONDS,
    _FD_SCAN_BUDGET_SECONDS,
    _IGNORED_BLOCK_DEVICE,
    _MAX_CHILDREN_READ_BYTES,
    _MAX_COMMAND_STDERR_BYTES,
    _MAX_COMMAND_STDOUT_BYTES,
    _MAX_FILE_READ_BYTES,
    _MAX_MOUNT_NAMESPACES_PER_OBSERVATION,
    _MAX_OPEN_FILE_RECORDS,
    _MAX_PROCESS_TREE_PIDS,
    _OUTPUT_LIMIT_EXIT_CODE,
    _PROCESS_MAP_FD_BUDGET_SECONDS,
    _READAHEAD_PROBE_BUDGET_SECONDS,
    _STATIC_PROBE_TIMEOUT_SECONDS,
    _cached_mountinfo_table,
    _classify_cmd,
    _decode_mountinfo_octal,
    _finite_mean,
    _finite_weighted_mean,
    _have_cmd,
    _heuristic_canonical,
    _is_block_device_candidate,
    _mapping_observation_key,
    _merge_mapping_observation,
    _mount_namespace_key,
    _mountinfo_table,
    _now_iso,
    _read_file,
    _resolve_canonical_device,
    _resolve_canonical_device_impl,
    _resolve_path_to_mount,
    _run,
    _run_bounded,
    _run_with_env,
    _strict_finite_float,
    _strict_text_rate,
    _temp_name,
    _to_float,
    parse_interval,
)

from _collection.disk import (  # noqa: F401
    _IOSTAT_AVG_FIELDS,
    _aggregate_iostat_samples,
    _device_type,
    _iostat_disk_from_json,
    _is_real_block_device,
    _list_block_devices,
    _parse_diskstats,
    _parse_iostat_json,
    _parse_iostat_text,
    collect_block_devices,
    collect_iostat,
    collect_readahead_scheduler,
)

from _collection.process import (  # noqa: F401
    _MOUNTINFO_LINE_RE,
    _aggregate_pidstat_samples,
    _children_of,
    _fd_mnt_id,
    _is_data_relevant_path_collector,
    _opened_file_records,
    _parse_pidstat_json,
    _parse_pidstat_text,
    _pid_starttime_ticks,
    _process_tree,
    _read_boot_id,
    collect_pidstat,
    collect_process_io_map,
)

from _collection.filesystem import (  # noqa: F401
    MountIdentity,
    _DATA_OPS,
    _METADATA_OPS,
    _PEROP_SECTION_MARKERS,
    _PROC_IO_COUNTERS,
    _delta_process_io_tree,
    _diff_mount_metrics,
    _is_glusterfs_fuse,
    _new_mount_acc,
    _parse_df,
    _parse_mountstats,
    _parse_rpc_nfs,
    _path_under_mount_collector,
    _read_proc_io_counters,
    _sample_process_io_tree,
    collect_df,
    collect_glusterfs,
    collect_memory,
    collect_mounts,
    collect_nfs,
)


def _resolve_target_path(path: str | None, pid: int | None) -> str | None:
    """Resolve a target path from the selected process's root filesystem view."""
    if not path:
        return path
    normalized = os.path.normpath(path)
    if pid is None:
        return os.path.realpath(normalized) if os.path.exists(normalized) else normalized
    proc_root = f"/proc/{pid}/root"
    candidate = os.path.join(proc_root, normalized.lstrip("/"))
    if not os.path.exists(candidate):
        return normalized
    try:
        root = os.path.realpath(proc_root)
        resolved = os.path.realpath(candidate)
        if os.path.commonpath((root, resolved)) != root:
            return normalized
        relative = os.path.relpath(resolved, root)
    except (OSError, ValueError):
        return normalized
    return "/" if relative == "." else "/" + relative


def collect(duration: float, pid: int | None, path: str | None) -> IoSnapshot:
    """并发采集所有 provider，组装 IoSnapshot。"""
    requested_path = path
    path = _resolve_target_path(path, pid)
    window_start = _now_iso()

    # 并发采集动态指标（同窗）
    dynamic: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=7) as ex:
        futures = {
            ex.submit(collect_block_devices, duration): "block",
            ex.submit(collect_iostat, duration): "iostat",
            ex.submit(collect_pidstat, duration): "pidstat",
            ex.submit(collect_nfs, duration, pid): "nfs",
            ex.submit(collect_glusterfs, duration, pid, path): "glusterfs",
            ex.submit(collect_process_io_map, pid, path, duration): "pmap",
            ex.submit(collect_mounts, pid): "mounts",
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
                    "glusterfs": "glusterfs",
                    "pmap": "process_io_map",
                    "mounts": "mounts",
                }[key]
                crashed = ProviderResult(
                    source=source,
                    status=STATUS_CMD_FAILED,
                    started_at=window_start,
                    ended_at=_now_iso(),
                    error=f"collector crashed: {type(e).__name__}: {e}",
                )
                dynamic[key] = (crashed, []) if key == "block" else crashed

    # Close the causal window as soon as concurrent dynamic evidence completes.
    # df/memory/readahead are static context and must not stretch R100-R400 windows.
    window_end = _now_iso()

    # 静态/快速采集（顺序，开销小）。单个 provider 的实现异常也必须隔离。
    def _safe_provider(source: str, func, *args) -> ProviderResult:
        try:
            result = func(*args)
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(
                source=source,
                status=STATUS_CMD_FAILED,
                started_at=window_end,
                ended_at=window_end,
                error=f"collector crashed: {type(exc).__name__}: {exc}",
            )
        if isinstance(result, ProviderResult):
            return result
        return ProviderResult(
            source=source,
            status=STATUS_CMD_FAILED,
            started_at=window_end,
            ended_at=window_end,
            error="collector returned an invalid result",
        )

    df_pr = _safe_provider("df", collect_df, pid)
    mem_pr = _safe_provider("memory", collect_memory)
    try:
        readahead, scheduler, partial = collect_readahead_scheduler()
        if not (isinstance(readahead, dict) and isinstance(scheduler, dict) and isinstance(partial, list)):
            raise TypeError("collector returned an invalid result")
    except Exception as exc:  # noqa: BLE001
        readahead, scheduler = {}, {}
        partial = [f"readahead_scheduler: collector crashed: {type(exc).__name__}: {exc}"]

    # 拆解动态结果
    block_result = dynamic.get("block")
    if isinstance(block_result, tuple) and len(block_result) == 2:
        block_pr, diskstats_samples = block_result
    else:
        block_pr, diskstats_samples = None, []
    if not isinstance(block_pr, ProviderResult) or not isinstance(diskstats_samples, list):
        block_pr = ProviderResult(
            source="block_devices",
            status=STATUS_CMD_FAILED,
            error="collector returned an invalid result",
        )
        diskstats_samples = []
    iostat_pr = dynamic.get("iostat", ProviderResult(source="iostat"))
    pidstat_pr = dynamic.get("pidstat", ProviderResult(source="pidstat"))
    nfs_pr = dynamic.get("nfs", ProviderResult(source="nfs"))
    glusterfs_pr = dynamic.get("glusterfs", ProviderResult(source="glusterfs"))
    pmap_pr = dynamic.get("pmap", ProviderResult(source="process_io_map"))
    mounts_pr = dynamic.get("mounts")
    if not isinstance(mounts_pr, ProviderResult):
        mounts_pr = ProviderResult(
            source="mounts",
            status=STATUS_CMD_FAILED,
            error="collector returned an invalid result",
        )

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
        "glusterfs": glusterfs_pr,
        "mounts": mounts_pr,
    }
    for name, pr in all_providers.items():
        if not isinstance(pr, ProviderResult):
            availability.errors.append(f"{name}: collector crashed")
            continue
        if pr.status == STATUS_MISSING:
            availability.missing.append(name)
        elif pr.status in (STATUS_PERMISSION, STATUS_CMD_FAILED, STATUS_PARSE_FAILED):
            availability.errors.append(f"{name}: {pr.status}" + (f" ({pr.error})" if pr.error else ""))
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
        target={
            "pid": pid,
            "path": path,
            **({"requested_path": requested_path} if requested_path is not None and requested_path != path else {}),
        },
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
        nfs=nfs_pr if isinstance(nfs_pr, ProviderResult) else ProviderResult(source="nfs", status=STATUS_CMD_FAILED),
        glusterfs=glusterfs_pr
        if isinstance(glusterfs_pr, ProviderResult)
        else ProviderResult(source="glusterfs", status=STATUS_CMD_FAILED),
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
    # 临时名含 PID 和随机串，避免并发任务写同一输出时碰撞。
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
        # 任何失败路径都清理残留 .tmp；replace 成功后临时文件已不存在。
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
    parser.add_argument("-o", "--out", default="", help="输出 JSON 文件路径；省略则输出到 stdout")
    parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="目标进程 PID（用于建立 PID→设备映射，R400）",
    )
    parser.add_argument("--path", default=None, help="目标数据集/挂载点路径（用于建立映射）")
    args = parser.parse_args(argv)

    # type=int 已在 argparse 层拒绝 float/NaN/Inf；这里再校验范围。
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

    snapshot = collect(eff, args.pid, args.path)

    if args.out:
        rc = write_snapshot(snapshot, args.out)
        if rc == 0:
            print(f"Snapshot 已写入: {args.out}", file=sys.stderr)
        return rc
    else:
        print(json.dumps(json.loads(snapshot.model_dump_json()), ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    sys.exit(main())
