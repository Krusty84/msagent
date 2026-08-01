#!/usr/bin/env python3
"""Tests for the deterministic, offline HTML report renderer."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import analyze_io_snapshot as analyzer  # noqa: E402
import render_io_report as renderer  # noqa: E402


class TestHtmlReport(unittest.TestCase):
    def setUp(self):
        fixture_dir = SKILL_ROOT / "evals" / "fixtures"
        self.snapshot_path = fixture_dir / "rc-r400-conflict.json"
        self.agent_path = fixture_dir / "report-agent.json"
        self.snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        self.findings = analyzer.analyze_all(self.snapshot)
        self.agent = renderer._normalize_agent_report(
            json.loads(self.agent_path.read_text(encoding="utf-8"))
        )
        self.template = (SKILL_ROOT / "assets" / "io_report_template.html").read_text(
            encoding="utf-8"
        )

    def _report(self, *, agent=None, targets=None, msprof=None, title="诊断报告"):
        return renderer.render_report(
            snapshot=self.snapshot,
            findings=self.findings,
            agent=agent,
            targets=targets,
            msprof=msprof,
            template_text=self.template,
            title=title,
            artifacts=[
                {
                    "label": "IO Snapshot",
                    "name": "io_snapshot.json",
                    "size": 123,
                    "sha256": "a" * 64,
                }
            ],
        )

    def test_renders_findings_metrics_and_agent_content(self):
        report = self._report(agent=self.agent)
        self.assertIn("R100", report)
        self.assertIn("R400", report)
        self.assertIn("sda", report)
        self.assertIn("PID 100", report)
        self.assertIn("先做单任务与多任务对照", report)
        self.assertIn("执行前必须确认", report)
        self.assertIn("sha256", report)

    def test_optional_target_and_msprof_artifacts_are_visualized(self):
        targets = {
            "recommendation": {
                "pid": 100,
                "path": "/data/train",
                "confidence": "high",
                "requires_confirmation": False,
                "reasons": ["命令行明确指定数据路径"],
            },
            "process_candidates": [
                {
                    "pid": 100,
                    "command": "python",
                    "cmdline": "python train.py --data /data/train",
                    "score": 95,
                    "reasons": ["训练进程候选明确"],
                    "path_candidates": [
                        {
                            "path": "/data/train",
                            "score": 100,
                            "role": "dataset",
                            "mount": {"fstype": "nfs4"},
                        }
                    ],
                }
            ],
        }
        msprof = {
            "diagnostic_proxies": {
                "op_summary_task_gap_proxy_percent": 12.5,
                "mte2_ratio_by_column": {
                    "MTE2 Ratio": {
                        "sample_count": 3,
                        "min": 0.1,
                        "arithmetic_mean": 0.2,
                        "max": 0.3,
                    }
                },
            },
            "provenance": {
                "device_id": 0,
                "task_count": 3,
                "limitations": [
                    "exported task gaps are not device idle time"
                ],
            },
        }
        report = self._report(targets=targets, msprof=msprof)
        self.assertIn("/data/train", report)
        self.assertIn("nfs4", report)
        self.assertIn("12.50%", report)
        self.assertIn("导出任务之间的间隔不等于 NPU 空闲时间", report)

    def test_user_and_agent_text_is_html_escaped(self):
        agent = renderer._normalize_agent_report(
            {
                "summary": "<script>alert('summary')</script>",
                "recommendations": ["<img src=x onerror=alert(1)>"],
            }
        )
        report = self._report(agent=agent, title="<b>unsafe</b>")
        self.assertNotIn("<script>alert", report)
        self.assertNotIn("<img src=x", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("&lt;b&gt;unsafe&lt;/b&gt;", report)

    def test_output_has_no_runtime_frontend_dependency(self):
        report = self._report(agent=self.agent)
        self.assertNotIn('<script src="', report)
        self.assertNotIn('<link rel="stylesheet"', report)
        self.assertNotIn("https://", report)
        self.assertIn("Content-Security-Policy", report)

    def test_rejects_invalid_agent_priority(self):
        with self.assertRaisesRegex(ValueError, "priority"):
            renderer._normalize_agent_report(
                {
                    "summary": "test",
                    "recommendations": [
                        {"priority": "urgent", "title": "x", "detail": "y"}
                    ],
                }
            )

    def test_cli_writes_a_single_html_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            findings_path = root / "findings.json"
            output_path = root / "report.html"
            findings_path.write_text(
                json.dumps(self.findings, ensure_ascii=False), encoding="utf-8"
            )
            result = renderer.main(
                [
                    "--snapshot",
                    str(self.snapshot_path),
                    "--findings",
                    str(findings_path),
                    "--agent-report",
                    str(self.agent_path),
                    "--output",
                    str(output_path),
                ]
            )
            self.assertEqual(result, 0)
            self.assertTrue(output_path.is_file())
            self.assertIn("<!doctype html>", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
