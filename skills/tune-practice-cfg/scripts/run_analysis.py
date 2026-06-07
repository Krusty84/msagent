#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run sensitive-layer analysis (replaces MCP analysis_run)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Bootstrap: add shared library to sys.path before any cross-skill imports
# ---------------------------------------------------------------------------
_common_dir = Path(__file__).resolve().parents[2] / "msmodelslim-tools-common" / "scripts"
if str(_common_dir) not in sys.path:
    sys.path.insert(0, str(_common_dir))

from script_utils import emit_result, ensure_msmodelslim, parse_optional_json
from shared import get_lab_calib_dir, parse_quant_device, to_analysis_metrics  # noqa: E402


def run_analysis(
    model_type: str,
    model_path: str,
    result_yaml_path: str,
    pattern: Optional[List[str]] = None,
    metrics: str = "kurtosis",
    calib_dataset: str = "boolq.jsonl",
    topk: int = 15,
    device: str = "npu",
    trust_remote_code: bool = False,
) -> Dict[str, Any]:
    try:
        from msmodelslim.app.analysis import LayerAnalysisApplication
        from msmodelslim.core.analysis_service import PipelineAnalysisService
        from msmodelslim.core.context import ContextFactory
        from msmodelslim.infra.analysis_pipeline_loader import AnalysisPipelineLoader
        from msmodelslim.infra.file_dataset_loader import FileDatasetLoader
        from msmodelslim.infra.yaml_analysis_result_displayer import YamlAnalysisResultDisplayer
        from msmodelslim.model import PluginModelFactory

        output_path = Path(result_yaml_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result_displayer = YamlAnalysisResultDisplayer(output_path=output_path)
        analysis_app = LayerAnalysisApplication(
            analysis_service=PipelineAnalysisService(
                FileDatasetLoader(get_lab_calib_dir()),
                context_factory=ContextFactory(),
                pipeline_loader=AnalysisPipelineLoader(),
            ),
            model_factory=PluginModelFactory(),
            result_manager=result_displayer,
        )
        device_type, device_indices = parse_quant_device(device)
        analysis_app.analyze(
            model_type=model_type,
            model_path=model_path,
            patterns=pattern or ["*"],
            device=device_type,
            device_indices=device_indices,
            metrics=to_analysis_metrics(metrics),
            calib_dataset=calib_dataset,
            topk=topk,
            trust_remote_code=trust_remote_code,
        )
        return {
            "ok": True,
            "message": "analysis finished",
            "result_yaml_path": str(output_path),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run sensitive-layer analysis.")
    parser.add_argument("--model-type", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--result-yaml-path", required=True)
    parser.add_argument("--pattern", default=None, help="JSON array of layer patterns")
    parser.add_argument("--metrics", default="kurtosis")
    parser.add_argument("--calib-dataset", default="boolq.jsonl")
    parser.add_argument("--topk", type=int, default=15)
    parser.add_argument("--device", default="npu")
    parser.add_argument("--trust-remote-code", action="store_true")
    args = parser.parse_args()

    ensure_msmodelslim()
    result = run_analysis(
        model_type=args.model_type,
        model_path=args.model_path,
        result_yaml_path=args.result_yaml_path,
        pattern=parse_optional_json(args.pattern),
        metrics=args.metrics,
        calib_dataset=args.calib_dataset,
        topk=args.topk,
        device=args.device,
        trust_remote_code=args.trust_remote_code,
    )
    return emit_result(result)


if __name__ == "__main__":
    sys.exit(main())
