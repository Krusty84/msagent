#!/usr/bin/env python3
"""Unit tests for bounded, read-only workload target discovery."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import discover_io_target as discovery  # noqa: E402


def _write_fake_process(
    proc_root: Path,
    pid: int,
    argv: list[str],
    *,
    cwd: str,
    ppid: int = 1,
    open_files: list[str] | None = None,
    mount_point: str = "/",
    source: str = "overlay",
    fstype: str = "overlay",
) -> None:
    proc_dir = proc_root / str(pid)
    (proc_dir / "fd").mkdir(parents=True)
    (proc_dir / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    (proc_dir / "stat").write_text(
        f"{pid} (python) S {ppid} 0 0 0 0 0 0\n", encoding="utf-8"
    )
    os.symlink(cwd, proc_dir / "cwd")
    (proc_dir / "mountinfo").write_text(
        f"36 25 0:31 / {mount_point} rw,relatime - {fstype} {source} rw\n",
        encoding="utf-8",
    )
    for index, target in enumerate(open_files or [], start=3):
        os.symlink(target, proc_dir / "fd" / str(index))


class TestTargetDiscovery(unittest.TestCase):
    def test_sanitizes_common_command_line_secrets(self):
        text = discovery.sanitize_cmdline(
            [
                "python",
                "train.py",
                "--token",
                "token-value",
                "--password=password-value",
                "https://user:pass@example.invalid/data",
            ]
        )
        self.assertNotIn("token-value", text)
        self.assertNotIn("password-value", text)
        self.assertNotIn("user:pass", text)
        self.assertIn("<redacted>", text)

    def test_extracts_explicit_and_standalone_paths(self):
        paths = discovery.extract_cmdline_paths(
            [
                "python",
                "main.py",
                "--data-root",
                "datasets/train",
                "--config=configs/train.yaml",
                "/mnt/extra/shard.bin",
            ],
            "/workspace",
        )
        keyed = {(item["path"], item["role"]) for item in paths}
        self.assertIn(("/workspace/datasets/train", "dataset"), keyed)
        self.assertIn(("/workspace/configs/train.yaml", "config"), keyed)
        self.assertIn(("/mnt/extra/shard.bin", "unknown"), keyed)

    def test_unique_process_and_dataset_are_recommended(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            proc_root = Path(temp_dir) / "proc"
            proc_root.mkdir()
            _write_fake_process(
                proc_root,
                101,
                ["python", "main.py", "--data-dir", "/mnt/data"],
                cwd="/workspace",
                open_files=["/mnt/data/shard-0001.bin", "/mnt/data/shard-0002.bin"],
                mount_point="/mnt/data",
                source="server:/dataset",
                fstype="nfs4",
            )
            payload = discovery.discover_targets(
                proc_root=proc_root, time_budget_seconds=10
            )
        self.assertEqual(len(payload["process_candidates"]), 1)
        candidate = payload["process_candidates"][0]
        self.assertEqual(candidate["pid"], 101)
        self.assertEqual(candidate["path_candidates"][0]["path"], "/mnt/data")
        self.assertEqual(candidate["path_candidates"][0]["mount"]["fstype"], "nfs4")
        self.assertEqual(payload["recommendation"]["pid"], 101)
        self.assertEqual(payload["recommendation"]["path"], "/mnt/data")
        self.assertFalse(payload["recommendation"]["requires_confirmation"])

    def test_tied_process_candidates_require_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            proc_root = Path(temp_dir) / "proc"
            proc_root.mkdir()
            _write_fake_process(
                proc_root,
                201,
                ["python", "train.py", "--data", "/mnt/data-a"],
                cwd="/workspace/a",
            )
            _write_fake_process(
                proc_root,
                202,
                ["python", "train.py", "--data", "/mnt/data-b"],
                cwd="/workspace/b",
            )
            payload = discovery.discover_targets(
                proc_root=proc_root, time_budget_seconds=10
            )
        self.assertEqual(len(payload["process_candidates"]), 2)
        self.assertIsNone(payload["recommendation"]["pid"])
        self.assertIsNone(payload["recommendation"]["path"])
        self.assertTrue(payload["recommendation"]["requires_confirmation"])

    def test_path_hint_outweighs_ancestor_working_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            proc_root = Path(temp_dir) / "proc"
            proc_root.mkdir()
            _write_fake_process(
                proc_root,
                301,
                ["python", "train.py"],
                cwd="/workspace/project",
            )
            payload = discovery.discover_targets(
                proc_root=proc_root,
                pid=301,
                path_hint="/workspace/project/data/train",
                time_budget_seconds=10,
            )
        self.assertEqual(
            payload["process_candidates"][0]["path_candidates"][0]["path"],
            "/workspace/project/data/train",
        )
        self.assertEqual(
            payload["recommendation"]["path"], "/workspace/project/data/train"
        )
        self.assertFalse(payload["recommendation"]["requires_confirmation"])

    def test_working_directory_alone_is_not_treated_as_dataset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            proc_root = Path(temp_dir) / "proc"
            proc_root.mkdir()
            _write_fake_process(
                proc_root,
                401,
                ["python", "train.py"],
                cwd="/workspace/project",
            )
            payload = discovery.discover_targets(
                proc_root=proc_root,
                pid=401,
                time_budget_seconds=10,
            )
        self.assertEqual(payload["recommendation"]["pid"], 401)
        self.assertIsNone(payload["recommendation"]["path"])
        self.assertTrue(payload["recommendation"]["requires_confirmation"])

    def test_path_hint_selects_matching_main_process_over_workers_and_other_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            proc_root = Path(temp_dir) / "proc"
            proc_root.mkdir()
            target_argv = ["python", "train.py", "--data-root", "/mnt/wanted"]
            _write_fake_process(proc_root, 501, target_argv, cwd="/workspace")
            _write_fake_process(
                proc_root,
                502,
                target_argv,
                cwd="/workspace",
                ppid=501,
            )
            _write_fake_process(
                proc_root,
                601,
                ["python", "train.py", "--data-root", "/mnt/other"],
                cwd="/workspace",
            )
            payload = discovery.discover_targets(
                proc_root=proc_root,
                path_hint="/mnt/wanted",
                time_budget_seconds=10,
            )

        self.assertEqual(payload["recommendation"]["pid"], 501)
        self.assertEqual(payload["recommendation"]["path"], "/mnt/wanted")
        self.assertFalse(payload["recommendation"]["requires_confirmation"])
        other = next(
            item for item in payload["process_candidates"] if item["pid"] == 601
        )
        self.assertNotIn(
            "/mnt/wanted", {item["path"] for item in other["path_candidates"]}
        )

    def test_prefers_torchrun_parent_over_rank_commands_for_same_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            proc_root = Path(temp_dir) / "proc"
            proc_root.mkdir()
            _write_fake_process(
                proc_root,
                701,
                ["python", "torchrun", "--data-file", "/mnt/train/data.bin"],
                cwd="/workspace",
            )
            for pid in (702, 703):
                _write_fake_process(
                    proc_root,
                    pid,
                    [
                        "python",
                        "distributed_io_train.py",
                        "--data-file",
                        "/mnt/train/data.bin",
                    ],
                    cwd="/workspace",
                    ppid=701,
                )
            payload = discovery.discover_targets(
                proc_root=proc_root,
                process_pattern="distributed_io_train.py",
                path_hint="/mnt/train/data.bin",
                time_budget_seconds=10,
            )

        self.assertEqual(payload["recommendation"]["pid"], 701)
        self.assertEqual(
            payload["recommendation"]["path"], "/mnt/train/data.bin"
        )
        self.assertFalse(payload["recommendation"]["requires_confirmation"])


if __name__ == "__main__":
    unittest.main()
