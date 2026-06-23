#!/usr/bin/env python3
"""
Mock Ascend NPU Profiling Data Generator for msagent Model Testing.

Generates synthetic profiling data files matching the Ascend PyTorch Profiler
output format. Supports single-card, multi-card, and cluster scenarios with
configurable bottleneck injection for targeted test cases.

Usage:
    # Generate a single-card profiling dataset
    python generate_mock_data.py single --rank 0 --out mock_data/single_rank/

    # Generate an 8-rank cluster dataset
    python generate_mock_data.py cluster --ranks 8 --out mock_data/cluster_8/

    # Generate with specific bottleneck (for testing skill responses)
    python generate_mock_data.py cluster --ranks 8 --bottleneck lane_degradation \
        --degraded-link "3->7" --out mock_data/cluster_lane_degrade/

    # Generate all test scenarios
    python generate_mock_data.py all --out mock_data/
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any


# ─── constants ───────────────────────────────────────────────────────────────

NPU_COUNT_PER_SERVER = 8
HCCS_LANE_COUNT_FULL = 7
AI_CORE_COUNT_910B = 40
AI_CORE_FREQ_MHZ = 1800
DDR_BANDWIDTH_GB_S = 204.8

# bandwidth anchors from ascend-communication-analysis SKILL.md
REF_BANDWIDTH = {
    "SDMA_memcpy":      19.0,   # GB/s, > 16 MB
    "SDMA_inline_reduce": 17.0, # GB/s, > 16 MB
    "RDMA":             21.0,   # GB/s, > 1 MB
}

# ─── helpers ─────────────────────────────────────────────────────────────────

def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def random_step_time(base_ms: float = 500.0, jitter_pct: float = 5.0) -> float:
    jitter = base_ms * (jitter_pct / 100.0)
    return base_ms + random.uniform(-jitter, jitter)


def random_bandwidth(ref_bw: float, degradation_pct: float = 0.0) -> float:
    noise = random.uniform(-0.05, 0.05) * ref_bw
    degraded = ref_bw * (1.0 - degradation_pct / 100.0)
    return max(0.1, degraded + noise)


# ─── CSV generators ──────────────────────────────────────────────────────────

def generate_step_trace_time_csv(
    path: Path,
    *,
    num_steps: int = 10,
    compute_ms: float = 300.0,
    communication_ms: float = 100.0,
    communication_overlap_ms: float = 80.0,
    free_ms: float = 20.0,
    wait_ms: float = 10.0,
):
    """
    Generate step_trace_time.csv.

    Key columns: Step, Compute Time(ms), Communication Time(ms),
    Communication Overlap Time(ms), Free Time(ms), Wait Time(ms)
    """
    rows = []
    for step in range(num_steps):
        rows.append({
            "Step": step,
            "Compute Time(ms)": round(random_step_time(compute_ms), 3),
            "Communication Time(ms)": round(random_step_time(communication_ms), 3),
            "Communication Overlap Time(ms)": round(random_step_time(communication_overlap_ms), 3),
            "Free Time(ms)": round(random_step_time(free_ms), 3),
            "Wait Time(ms)": round(random_step_time(wait_ms), 3),
            "Total Step Time(ms)": round(
                random_step_time(compute_ms + communication_ms + free_ms), 3
            ),
        })
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def generate_op_statistic_csv(path: Path):
    """
    Generate op_statistic.csv with a realistic compute operator distribution.
    """
    operators = [
        ("MatMul",          120.0,  50,  2.40,  "AI_CORE"),
        ("FlashAttention",  80.0,   30,  2.67,  "AI_CORE"),
        ("Add",             40.0,   200, 0.20,  "AI_CORE"),
        ("LayerNorm",       25.0,   48,  0.52,  "AI_CORE"),
        ("GELU",            18.0,   24,  0.75,  "AI_CORE"),
        ("TransData",       15.0,   60,  0.25,  "AI_CORE"),
        ("Transpose",       12.0,   40,  0.30,  "AI_CORE"),
        ("Cast",            8.0,    80,  0.10,  "AI_CORE"),
        ("Softmax",         7.0,    24,  0.29,  "AI_CORE"),
        ("BatchMatMul",     35.0,   16,  2.19,  "AI_CORE"),
        ("ReduceSum",       5.0,    30,  0.17,  "AI_CORE"),
        ("AICPU_CustomOp",  15.0,   10,  1.50,  "AI_CPU"),
    ]
    rows = []
    for name, total_ms, count, avg_ms, core in operators:
        rows.append({
            "Operator Name": name,
            "Total Duration(ms)": total_ms,
            "Count": count,
            "Average Duration(ms)": avg_ms,
            "Max Duration(ms)": round(avg_ms * random.uniform(1.1, 2.0), 3),
            "Accelerator Core": core,
        })
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def generate_kernel_details_csv(path: Path, num_kernels: int = 100):
    """
    Generate kernel_details.csv.
    """
    kernels = []
    for i in range(num_kernels):
        kernels.append({
            "Kernel Name": f"kernel_{i}",
            "Total Duration(us)": round(random.uniform(10, 5000), 2),
            "Count": random.randint(1, 50),
            "Average Duration(us)": round(random.uniform(10, 500), 2),
            "Max Duration(us)": round(random.uniform(100, 10000), 2),
            "Stream ID": random.randint(0, 7),
            "Task Type": random.choice(["AI_CORE", "AI_VECTOR", "AI_CPU", "MIX_AIC", "MIX_AIV"]),
        })
    fieldnames = list(kernels[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kernels)


def generate_api_statistic_csv(path: Path):
    """Generate api_statistic.csv with CANN API statistics."""
    apis = [
        ("aclrtLaunchKernel",       80.0,  500, 0.16),
        ("aclrtSynchronizeStream",  25.0,  10,  2.50),
        ("aclrtMemcpy",             12.0,  40,  0.30),
        ("aclrtMalloc",             8.0,   30,  0.27),
        ("aclrtFree",               5.0,   30,  0.17),
        ("aclrtEnqueue",            15.0,  60,  0.25),
        ("aclrtDequeue",            10.0,  60,  0.17),
    ]
    rows = []
    for name, total_ms, count, avg_ms in apis:
        rows.append({
            "API Name": name,
            "Total Duration(ms)": total_ms,
            "Count": count,
            "Average Duration(ms)": avg_ms,
            "Max Duration(ms)": round(avg_ms * random.uniform(2.0, 5.0), 3),
        })
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ─── JSON generators ─────────────────────────────────────────────────────────

def generate_profiler_info_json(path: Path, rank_id: int, profiler_level: str = "level1"):
    """Generate profiler_info_{rank_id}.json"""
    data = {
        "rank_id": rank_id,
        "profiler_level": profiler_level,
        "profiling_start_time": "2026-06-22T10:00:00",
        "profiling_end_time": "2026-06-22T10:05:00",
        "num_steps": 10,
        "device_type": "Ascend910B",
        "device_count": NPU_COUNT_PER_SERVER,
        "world_size": 8,
        "framework": "PyTorch",
        "framework_version": "2.1.0",
        "cann_version": "8.1.RC1",
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def generate_profiler_metadata_json(path: Path):
    """Generate profiler_metadata.json with parallel strategy info."""
    data = {
        "world_size": 8,
        "tensor_model_parallel_size": 4,
        "pipeline_model_parallel_size": 1,
        "data_parallel_size": 2,
        "parallel_group_info": {
            "tp": [[0, 1, 2, 3], [4, 5, 6, 7]],
            "dp": [[0, 4], [1, 5], [2, 6], [3, 7]],
        },
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def generate_communication_json(
    path: Path,
    *,
    degraded_links: dict[str, int] | None = None,
):
    """
    Generate communication.json with collective op details.

    degraded_links: {"3->7": 3, "7->3": 5} means NPU3→NPU7 has 3 lanes, etc.
    """
    if degraded_links is None:
        degraded_links = {}

    ops = [
        {"op_name": "hcom_allReduce__123_0_1",   "op_type": "allReduce",    "group": "dp",   "elapsed_ms": 45.0, "transit_size_mb": 256, "transport": "RDMA"},
        {"op_name": "hcom_allReduce__123_0_1",   "op_type": "allReduce",    "group": "dp",   "elapsed_ms": 48.0, "transit_size_mb": 256, "transport": "RDMA"},
        {"op_name": "hcom_allGather__318_0_1",   "op_type": "allGather",    "group": "tp",   "elapsed_ms": 12.0, "transit_size_mb": 64,  "transport": "SDMA"},
        {"op_name": "hcom_reduceScatter__456_0_1","op_type": "reduceScatter","group": "tp",  "elapsed_ms": 8.0,  "transit_size_mb": 32,  "transport": "SDMA"},
        {"op_name": "hcom_allReduce__789_0_1",   "op_type": "allReduce",    "group": "dp",   "elapsed_ms": 50.0, "transit_size_mb": 128, "transport": "RDMA"},
        {"op_name": "hcom_broadcast__111_0_1",   "op_type": "broadcast",    "group": "tp",   "elapsed_ms": 3.0,  "transit_size_mb": 4,   "transport": "SDMA"},
    ]

    for op in ops:
        # apply lane degradation effects
        transport = op["transport"]
        ref_bw = REF_BANDWIDTH["RDMA"] if transport == "RDMA" else REF_BANDWIDTH["SDMA_memcpy"]
        bw = random_bandwidth(ref_bw)
        op["bandwidth_gb_s"] = round(bw, 3)
        op["wait_time_ms"] = round(random.uniform(0, op["elapsed_ms"] * 0.3), 3)
        op["transit_time_ms"] = round(op["transit_size_mb"] / max(bw, 0.001), 3)

    with open(path, "w") as f:
        json.dump(ops, f, indent=2)


def generate_communication_matrix_json(
    path: Path,
    num_ranks: int = 8,
    *,
    degraded_links: dict[str, int] | None = None,
):
    """
    Generate communication_matrix.json with rank-pair link details.

    degraded_links maps "src->dst" to current_lane_count.
    """
    if degraded_links is None:
        degraded_links = {}

    entries = []
    for src in range(num_ranks):
        for dst in range(num_ranks):
            if src == dst:
                continue
            key = f"{src}->{dst}"
            current_lanes = degraded_links.get(key, HCCS_LANE_COUNT_FULL)
            lane_ratio = current_lanes / HCCS_LANE_COUNT_FULL

            # determine transport type
            same_server = (src // 4) == (dst // 4)
            transport = "SDMA" if same_server else "RDMA"
            ref_bw = REF_BANDWIDTH["SDMA_memcpy"] if transport == "SDMA" else REF_BANDWIDTH["RDMA"]
            effective_bw = ref_bw * lane_ratio

            entries.append({
                "src_rank": src,
                "dst_rank": dst,
                "transport_type": transport,
                "lane_count": current_lanes,
                "expected_lane_count": HCCS_LANE_COUNT_FULL,
                "bandwidth_gb_s": round(random_bandwidth(effective_bw, degradation_pct=0), 3),
                "transit_size_mb": random.choice([16, 32, 64, 128, 256]),
                "transit_time_ms": round(random.uniform(0.5, 20.0), 3),
            })

    with open(path, "w") as f:
        json.dump(entries, f, indent=2)


def generate_trace_view_json(path: Path, num_ranks: int = 8):
    """Generate a minimal trace_view.json with timeline events."""
    events = []
    tid = 0
    for rank in range(num_ranks):
        # compute stream
        compute_tid = tid
        tid += 1
        events.append({
            "name": "process_name", "ph": "M",
            "pid": rank, "tid": compute_tid,
            "args": {"name": f"Rank {rank} Compute Stream"},
        })
        # communication stream
        comm_tid = tid
        tid += 1
        events.append({
            "name": "process_name", "ph": "M",
            "pid": rank, "tid": comm_tid,
            "args": {"name": f"Rank {rank} HCCL Stream"},
        })
        # sample kernel events
        for step in range(10):
            t0 = step * 500_000  # microsecond base
            # compute kernel
            events.append({
                "name": "MatMul", "ph": "X", "cat": "kernel",
                "pid": rank, "tid": compute_tid,
                "ts": t0, "dur": random.randint(50_000, 150_000),
                "args": {"step": step},
            })
            # communication op
            events.append({
                "name": "allReduce", "ph": "X", "cat": "communication",
                "pid": rank, "tid": comm_tid,
                "ts": t0 + 100_000, "dur": random.randint(20_000, 80_000),
                "args": {"step": step},
            })

    with open(path, "w") as f:
        json.dump({"traceEvents": events}, f, indent=2)


# ─── SQLite DB generators ────────────────────────────────────────────────────

def generate_rank_db(
    path: Path,
    rank_id: int,
    *,
    num_steps: int = 10,
    degraded_links: dict[str, int] | None = None,
):
    """
    Generate a rank-level ascend_pytorch_profiler_{rank_id}.db
    with the key tables: COMMUNICATION_OP, COMMUNICATION_TASK_INFO, StepTraceTime.
    """
    if degraded_links is None:
        degraded_links = {}

    conn = sqlite3.connect(str(path))

    # --- COMMUNICATION_OP (large ops) ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS COMMUNICATION_OP (
            id INTEGER PRIMARY KEY,
            step INTEGER,
            op_name TEXT,
            group_name TEXT,
            elapsed_time REAL,
            wait_time REAL,
            transit_time REAL,
            transit_size REAL,
            bandwidth REAL,
            link_type TEXT,
            retry INTEGER DEFAULT 0,
            relay INTEGER DEFAULT 0
        )
    """)

    op_types = [
        ("hcom_allReduce__123_0_1", "group_dp_0", 45.0, 256.0, "RDMA"),
        ("hcom_allGather__318_0_1", "group_tp_0", 12.0, 64.0, "SDMA"),
        ("hcom_reduceScatter__456_0_1", "group_tp_0", 8.0, 32.0, "SDMA"),
        ("hcom_allReduce__789_0_1", "group_dp_1", 50.0, 128.0, "RDMA"),
    ]

    for step in range(num_steps):
        for op_name, group, base_elapsed, size_mb, transport in op_types:
            ref_bw = REF_BANDWIDTH["RDMA"] if transport == "RDMA" else REF_BANDWIDTH["SDMA_memcpy"]
            lane_ratio = 1.0
            for link_key, lanes in degraded_links.items():
                src_str, dst_str = link_key.split("->")
                if int(src_str) == rank_id:
                    lane_ratio = min(lane_ratio, lanes / HCCS_LANE_COUNT_FULL)

            effective_bw = ref_bw * lane_ratio
            elapsed = base_elapsed + random.uniform(-base_elapsed * 0.1, base_elapsed * 0.1)
            wait = elapsed * random.uniform(0.05, 0.25)
            transit = size_mb / effective_bw * 1000  # ms
            bw = size_mb / (transit / 1000) / 1000  # GB/s

            conn.execute(
                """INSERT INTO COMMUNICATION_OP
                   (step, op_name, group_name, elapsed_time, wait_time,
                    transit_time, transit_size, bandwidth, link_type, retry, relay)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (step, op_name, group, elapsed, wait, transit, size_mb * 1024 * 1024,
                 round(bw, 3), transport, 0, 0),
            )

    # --- COMMUNICATION_TASK_INFO (small tasks) ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS COMMUNICATION_TASK_INFO (
            id INTEGER PRIMARY KEY,
            task_name TEXT,
            task_type TEXT,
            src_rank INTEGER,
            dst_rank INTEGER,
            elapsed_time REAL,
            transit_size REAL,
            link_type TEXT
        )
    """)

    small_tasks = [
        ("Notify Wait", "Notify", None, None, 2.0, 0, None),
        ("Notify Record", "Notify", None, None, 0.5, 0, None),
        ("Memcpy", "Memcpy", None, None, 5.0, 1048576, "SDMA"),
        ("Reduce Inline", "Reduce", None, None, 3.0, 524288, "SDMA"),
    ]
    for step in range(num_steps):
        for name, ttype, src, dst, elapsed, size, link in small_tasks:
            conn.execute(
                """INSERT INTO COMMUNICATION_TASK_INFO
                   (task_name, task_type, src_rank, dst_rank, elapsed_time, transit_size, link_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, ttype, src, dst, elapsed, size, link),
            )

    # --- StepTraceTime ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS StepTraceTime (
            step INTEGER PRIMARY KEY,
            Compute_Time REAL,
            Communication_Time REAL,
            Communication_Overlap_Time REAL,
            Free_Time REAL,
            Wait_Time REAL,
            Total_Step_Time REAL
        )
    """)
    for step in range(num_steps):
        conn.execute(
            """INSERT INTO StepTraceTime
               (step, Compute_Time, Communication_Time, Communication_Overlap_Time,
                Free_Time, Wait_Time, Total_Step_Time)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (step,
             random_step_time(300),
             random_step_time(100),
             random_step_time(80),
             random_step_time(20),
             random_step_time(10),
             random_step_time(430)),
        )

    conn.commit()
    conn.close()


def generate_cluster_analysis_db(
    path: Path,
    num_ranks: int = 8,
    *,
    bottleneck: str = "none",
    degraded_links: dict[str, int] | None = None,
):
    """
    Generate a cluster_analysis.db with ClusterCommunicationTime,
    ClusterCommunicationBandwidth, ClusterCommunicationMatrix, and
    CommunicationGroupMapping tables.
    """
    if degraded_links is None:
        degraded_links = {}

    conn = sqlite3.connect(str(path))

    # --- CommunicationGroupMapping ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS CommunicationGroupMapping (
            group_id TEXT,
            group_name TEXT,
            pg_name TEXT,
            type TEXT,
            rank_set TEXT
        )
    """)
    groups = [
        ("0", "group_dp_0", "dp0", "DATA_PARALLEL", "0,1,2,3"),
        ("1", "group_dp_1", "dp1", "DATA_PARALLEL", "4,5,6,7"),
        ("2", "group_tp_0", "tp0", "TENSOR_MODEL_PARALLEL", "0,1,2,3"),
        ("3", "group_tp_1", "tp1", "TENSOR_MODEL_PARALLEL", "4,5,6,7"),
    ]
    for gid, gname, pg, gtype, rset in groups:
        conn.execute(
            "INSERT INTO CommunicationGroupMapping VALUES (?, ?, ?, ?, ?)",
            (gid, gname, pg, gtype, rset),
        )

    # --- ClusterCommunicationTime ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ClusterCommunicationTime (
            id INTEGER PRIMARY KEY,
            step INTEGER,
            hccl_op_name TEXT,
            group_name TEXT,
            rank_id INTEGER,
            start_timestamp REAL,
            elapsed_time REAL,
            wait_time REAL,
            transit_time REAL
        )
    """)

    op_defs = [
        ("hcom_allReduce__123_0_1", "group_dp_0", 45.0),
        ("hcom_allGather__318_0_1", "group_tp_0", 12.0),
        ("hcom_reduceScatter__456_0_1", "group_tp_0", 8.0),
    ]

    # inject Total Op Info aggregate row
    conn.execute(
        """INSERT INTO ClusterCommunicationTime
           (step, hccl_op_name, group_name, rank_id, start_timestamp, elapsed_time, wait_time, transit_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (0, "Total Op Info", "", 0, 1000.0, 2000.0, 200.0, 1800.0),
    )

    for step in range(5):
        t0 = step * 5000.0 + 1000.0
        for op_name, group, base_elapsed in op_defs:
            for rank in range(num_ranks):
                # inject bottleneck-specific behavior
                elapsed = base_elapsed + random.uniform(-base_elapsed * 0.1, base_elapsed * 0.1)
                wait = elapsed * random.uniform(0.05, 0.20)
                transit = elapsed - wait

                if bottleneck == "slow_rank" and rank == 7:
                    elapsed *= 1.5
                    wait *= 2.0
                elif bottleneck == "wait_heavy" and rank == 3:
                    wait = elapsed * 0.7
                    transit = elapsed * 0.3

                start = t0 + rank * random.uniform(0, 50)
                conn.execute(
                    """INSERT INTO ClusterCommunicationTime
                       (step, hccl_op_name, group_name, rank_id, start_timestamp,
                        elapsed_time, wait_time, transit_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (step, op_name, group, rank, start, elapsed, wait, transit),
                )

    # --- ClusterCommunicationBandwidth ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ClusterCommunicationBandwidth (
            id INTEGER PRIMARY KEY,
            step INTEGER,
            hccl_op_name TEXT,
            group_name TEXT,
            rank_id INTEGER,
            band_type TEXT,
            transit_size REAL,
            transit_time REAL,
            bandwidth REAL,
            large_packet_ratio REAL
        )
    """)
    for step in range(5):
        for op_name, group, base_elapsed in op_defs:
            for rank in range(num_ranks):
                transport = "RDMA" if "dp" in group else "SDMA"
                ref_bw = REF_BANDWIDTH["RDMA"] if transport == "RDMA" else REF_BANDWIDTH["SDMA_memcpy"]
                size = random.choice([16, 32, 64, 128, 256])

                # apply lane degradation
                lane_ratio = 1.0
                for link_key, lanes in degraded_links.items():
                    src_str, dst_str = link_key.split("->")
                    if int(src_str) == rank:
                        lane_ratio = min(lane_ratio, lanes / HCCS_LANE_COUNT_FULL)

                effective_bw = ref_bw * lane_ratio
                bw = random_bandwidth(effective_bw)

                conn.execute(
                    """INSERT INTO ClusterCommunicationBandwidth
                       (step, hccl_op_name, group_name, rank_id, band_type,
                        transit_size, transit_time, bandwidth, large_packet_ratio)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (step, op_name, group, rank, transport,
                     size * 1024 * 1024, size / max(bw, 0.001), round(bw, 3),
                     random.uniform(0.8, 1.0)),
                )

    # --- ClusterCommunicationMatrix ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ClusterCommunicationMatrix (
            id INTEGER PRIMARY KEY,
            group_name TEXT,
            hccl_op_name TEXT,
            src_rank INTEGER,
            dst_rank INTEGER,
            transport_type TEXT,
            transit_size REAL,
            transit_time REAL,
            bandwidth REAL
        )
    """)
    for step in range(5):
        for op_name, group, _base in op_defs:
            for src in range(num_ranks):
                for dst in range(num_ranks):
                    if src == dst:
                        continue
                    same_server = (src // 4) == (dst // 4)
                    transport = "SDMA" if same_server else "RDMA"
                    ref_bw = REF_BANDWIDTH["SDMA_memcpy"] if transport == "SDMA" else REF_BANDWIDTH["RDMA"]

                    link_key = f"{src}->{dst}"
                    current_lanes = degraded_links.get(link_key, HCCS_LANE_COUNT_FULL)
                    lane_ratio = current_lanes / HCCS_LANE_COUNT_FULL
                    effective_bw = ref_bw * lane_ratio

                    size = random.choice([16, 32, 64, 128])
                    bw = random_bandwidth(effective_bw)

                    conn.execute(
                        """INSERT INTO ClusterCommunicationMatrix
                           (group_name, hccl_op_name, src_rank, dst_rank, transport_type,
                            transit_size, transit_time, bandwidth)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (group, op_name, src, dst, transport,
                         size * 1024 * 1024, size / max(bw, 0.001), round(bw, 3)),
                    )

    conn.commit()
    conn.close()


# ─── bottleneck injectors ────────────────────────────────────────────────────

class BottleneckConfig:
    """Configuration for a specific bottleneck scenario."""

    def __init__(
        self,
        name: str,
        description: str,
        step_trace_overrides: dict | None = None,
        degraded_links: dict[str, int] | None = None,
        cluster_bottleneck: str = "none",
        comm_overlap_ratio: float | None = None,
    ):
        self.name = name
        self.description = description
        self.step_trace_overrides = step_trace_overrides or {}
        self.degraded_links = degraded_links or {}
        self.cluster_bottleneck = cluster_bottleneck
        self.comm_overlap_ratio = comm_overlap_ratio


BOTTLENECK_SCENARIOS = {
    # ─── communication overlap scenarios ───
    "good_overlap": BottleneckConfig(
        name="good_overlap",
        description="Communication well-hidden behind compute (84% overlap)",
        step_trace_overrides={
            "communication_ms": 100.0,
            "communication_overlap_ms": 84.0,  # 84% overlap
        },
        comm_overlap_ratio=0.84,
    ),
    "poor_overlap_no_compute": BottleneckConfig(
        name="poor_overlap_no_compute",
        description="No compute to overlap with during sync phase",
        step_trace_overrides={
            "communication_ms": 120.0,
            "communication_overlap_ms": 10.0,  # < 10% overlap
            "free_ms": 15.0,
        },
        comm_overlap_ratio=0.08,
    ),
    "poor_overlap_contention": BottleneckConfig(
        name="poor_overlap_contention",
        description="Compute-communication bandwidth contention (overlap degrades both)",
        step_trace_overrides={
            "communication_ms": 150.0,
            "communication_overlap_ms": 50.0,  # partial overlap
            "compute_ms": 350.0,  # elevated due to contention
        },
        comm_overlap_ratio=0.33,
    ),

    # ─── lane degradation scenario ───
    "lane_degradation": BottleneckConfig(
        name="lane_degradation",
        description="NPU3→NPU7 link degraded from 7 to 3 lanes",
        degraded_links={"3->7": 3, "3->6": 5},
        cluster_bottleneck="none",
    ),

    # ─── wait-caused communication slow ───
    "wait_caused": BottleneckConfig(
        name="wait_caused",
        description="Rank 3 waiting for slow Rank 7",
        cluster_bottleneck="wait_heavy",
    ),

    # ─── slow rank ───
    "slow_rank": BottleneckConfig(
        name="slow_rank",
        description="Rank 7 is consistently 50% slower than peers",
        cluster_bottleneck="slow_rank",
    ),

    # ─── host bound ───
    "host_bound": BottleneckConfig(
        name="host_bound",
        description="Device Free time > 20%, host dispatch bottleneck",
        step_trace_overrides={
            "free_ms": 100.0,  # 20%+ of step
            "compute_ms": 280.0,
            "communication_ms": 60.0,
        },
    ),

    # ─── clean baseline ───
    "clean": BottleneckConfig(
        name="clean",
        description="No bottlenecks, normal performance profile",
    ),
}


# ─── main generator ──────────────────────────────────────────────────────────

def generate_single_rank(
    out_dir: Path,
    rank_id: int = 0,
    bottleneck: str = "clean",
):
    """Generate a single rank profiling directory."""
    bc = BOTTLENECK_SCENARIOS.get(bottleneck, BOTTLENECK_SCENARIOS["clean"])

    rank_dir = ensure_dir(out_dir / f"worker1_20260622100000_ascend_pt")
    output_dir = ensure_dir(rank_dir / "ASCEND_PROFILER_OUTPUT")

    # profiler info
    generate_profiler_info_json(rank_dir / f"profiler_info_{rank_id}.json", rank_id)
    generate_profiler_metadata_json(rank_dir / "profiler_metadata.json")

    # CSV files
    st_overrides = bc.step_trace_overrides
    generate_step_trace_time_csv(
        output_dir / "step_trace_time.csv",
        compute_ms=st_overrides.get("compute_ms", 300.0),
        communication_ms=st_overrides.get("communication_ms", 100.0),
        communication_overlap_ms=st_overrides.get("communication_overlap_ms", 80.0),
        free_ms=st_overrides.get("free_ms", 20.0),
        wait_ms=st_overrides.get("wait_ms", 10.0),
    )
    generate_op_statistic_csv(output_dir / "op_statistic.csv")
    generate_kernel_details_csv(output_dir / "kernel_details.csv")
    generate_api_statistic_csv(output_dir / "api_statistic.csv")

    # JSON files
    generate_communication_json(
        output_dir / "communication.json",
        degraded_links=bc.degraded_links,
    )
    generate_communication_matrix_json(
        output_dir / "communication_matrix.json",
        degraded_links=bc.degraded_links,
    )
    generate_trace_view_json(output_dir / "trace_view.json")

    # DB file
    generate_rank_db(
        output_dir / f"ascend_pytorch_profiler_{rank_id}.db",
        rank_id=rank_id,
        degraded_links=bc.degraded_links,
    )

    print(f"  Generated single rank data: {rank_dir}")


def generate_cluster(
    out_dir: Path,
    num_ranks: int = 8,
    bottleneck: str = "clean",
):
    """Generate a multi-rank cluster profiling dataset."""
    bc = BOTTLENECK_SCENARIOS.get(bottleneck, BOTTLENECK_SCENARIOS["clean"])
    cluster_dir = ensure_dir(out_dir / "cluster_data")

    # generate per-rank directories
    for rank in range(num_ranks):
        rank_dir = ensure_dir(
            cluster_dir / f"worker{rank}_20260622100000_ascend_pt"
        )
        output_dir = ensure_dir(rank_dir / "ASCEND_PROFILER_OUTPUT")

        generate_profiler_info_json(rank_dir / f"profiler_info_{rank}.json", rank)
        if rank == 0:
            generate_profiler_metadata_json(rank_dir / "profiler_metadata.json")

        rank_bc = bc
        step_trace_kwargs = {
            "compute_ms": rank_bc.step_trace_overrides.get("compute_ms", 300.0),
            "communication_ms": rank_bc.step_trace_overrides.get("communication_ms", 100.0),
            "communication_overlap_ms": rank_bc.step_trace_overrides.get("communication_overlap_ms", 80.0),
            "free_ms": rank_bc.step_trace_overrides.get("free_ms", 20.0),
            "wait_ms": rank_bc.step_trace_overrides.get("wait_ms", 10.0),
        }

        # if slow_rank bottleneck, make rank 7 worse
        if bottleneck == "slow_rank" and rank == 7:
            step_trace_kwargs["compute_ms"] *= 1.5
            step_trace_kwargs["free_ms"] *= 2.0
        if bottleneck == "host_bound" and rank == 0:
            step_trace_kwargs["free_ms"] *= 3.0

        generate_step_trace_time_csv(output_dir / "step_trace_time.csv", **step_trace_kwargs)
        generate_op_statistic_csv(output_dir / "op_statistic.csv")
        generate_kernel_details_csv(output_dir / "kernel_details.csv")
        generate_api_statistic_csv(output_dir / "api_statistic.csv")
        generate_communication_json(
            output_dir / "communication.json",
            degraded_links=bc.degraded_links,
        )
        generate_communication_matrix_json(
            output_dir / "communication_matrix.json",
            num_ranks=num_ranks,
            degraded_links=bc.degraded_links,
        )
        generate_rank_db(
            output_dir / f"ascend_pytorch_profiler_{rank}.db",
            rank_id=rank,
            degraded_links=bc.degraded_links,
        )

    # generate cluster analysis output
    cluster_output_dir = ensure_dir(cluster_dir / "cluster_analysis_output")
    generate_cluster_analysis_db(
        cluster_output_dir / "cluster_analysis.db",
        num_ranks=num_ranks,
        bottleneck=bc.cluster_bottleneck,
        degraded_links=bc.degraded_links,
    )

    print(f"  Generated {num_ranks}-rank cluster data: {cluster_dir}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate mock Ascend NPU profiling data")
    sub = parser.add_subparsers(dest="command")

    single_p = sub.add_parser("single", help="Generate single-rank profiling data")
    single_p.add_argument("--rank", type=int, default=0)
    single_p.add_argument("--bottleneck", choices=list(BOTTLENECK_SCENARIOS.keys()), default="clean")
    single_p.add_argument("--out", default="mock_data/single_rank/")

    cluster_p = sub.add_parser("cluster", help="Generate multi-rank cluster profiling data")
    cluster_p.add_argument("--ranks", type=int, default=8)
    cluster_p.add_argument("--bottleneck", choices=list(BOTTLENECK_SCENARIOS.keys()), default="clean")
    cluster_p.add_argument("--out", default="mock_data/cluster/")

    all_p = sub.add_parser("all", help="Generate all test scenarios")
    all_p.add_argument("--out", default="mock_data/")

    args = parser.parse_args()

    if args.command == "single":
        out = Path(args.out)
        generate_single_rank(out, rank_id=args.rank, bottleneck=args.bottleneck)

    elif args.command == "cluster":
        out = Path(args.out)
        bc = BOTTLENECK_SCENARIOS.get(args.bottleneck, BOTTLENECK_SCENARIOS["clean"])
        print(f"Scenario: {bc.name} — {bc.description}")
        generate_cluster(out, num_ranks=args.ranks, bottleneck=args.bottleneck)

    elif args.command == "all":
        base = Path(args.out)
        print("Generating all test scenarios...\n")

        # single rank scenarios
        print("Single-rank scenarios:")
        for key in ["clean", "good_overlap", "poor_overlap_no_compute", "host_bound"]:
            bc = BOTTLENECK_SCENARIOS[key]
            print(f"  [{key}] {bc.description}")
            generate_single_rank(base / key, bottleneck=key)

        # cluster scenarios
        print("\nCluster scenarios:")
        for key in ["clean", "lane_degradation", "wait_caused", "slow_rank"]:
            bc = BOTTLENECK_SCENARIOS[key]
            print(f"  [{key}] {bc.description}")
            generate_cluster(base / key, num_ranks=8, bottleneck=key)

        print(f"\nAll mock data generated under: {base.resolve()}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
