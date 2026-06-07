#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run model quantization from Practice YAML (replaces MCP quantization_run)."""

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
from shared import get_lab_calib_dir, get_lab_practice_dir, parse_quant_device, to_quant_type  # noqa: E402


def run_quantization(
    model_type: str,
    model_path: str,
    save_path: str,
    device: str = "npu",
    config_path: Optional[str] = None,
    quant_type: Optional[str] = None,
    trust_remote_code: bool = False,
    debug: bool = False,
    tag: Optional[List[str]] = None,
) -> Dict[str, Any]:
    try:
        from msmodelslim.app.naive_quantization import NaiveQuantizationApplication
        from msmodelslim.core.context import ContextFactory
        from msmodelslim.core.quant_service.proxy import QuantServiceProxy, QuantServiceProxyConfig
        from msmodelslim.infra.dataset_loader.vlm_dataset_loader import VLMDatasetLoader
        from msmodelslim.infra.debug_info_persistence import DebugInfoPersistence
        from msmodelslim.infra.file_dataset_loader import FileDatasetLoader
        from msmodelslim.infra.plugin_practice_dirs import discover_plugin_practice_dirs
        from msmodelslim.infra.yaml_practice_manager import YamlPracticeManager
        from msmodelslim.infra.yaml_quant_config_exporter import YamlQuantConfigExporter
        from msmodelslim.model import PluginModelFactory
        from msmodelslim.utils.config import msmodelslim_config

        custom_practice_dir = msmodelslim_config.env_vars.custom_practice_repo
        custom_practice_path = Path(custom_practice_dir) if custom_practice_dir else None
        plugin_dirs = discover_plugin_practice_dirs()
        practice_manager = YamlPracticeManager(
            official_config_dir=get_lab_practice_dir(),
            custom_config_dir=custom_practice_path,
            third_party_config_dirs=plugin_dirs if plugin_dirs else None,
        )

        dataset_dir = get_lab_calib_dir()
        device_type, device_index = parse_quant_device(device)
        debug_info_persistence = DebugInfoPersistence(save_dir=save_path) if debug else None

        app = NaiveQuantizationApplication(
            practice_manager=practice_manager,
            quant_service=QuantServiceProxy(
                QuantServiceProxyConfig(apiversion="proxy"),
                FileDatasetLoader(dataset_dir),
                VLMDatasetLoader(dataset_dir),
                context_factory=ContextFactory(enable_debug=debug),
                debug_info_persistence=debug_info_persistence,
            ),
            model_factory=PluginModelFactory(),
            quant_config_export_infra=YamlQuantConfigExporter(),
        )

        app.quant(
            model_type=model_type,
            model_path=model_path,
            save_path=save_path,
            device_type=device_type,
            device_index=device_index,
            quant_type=to_quant_type(quant_type),
            config_path=config_path,
            trust_remote_code=trust_remote_code,
            tag=tag,
        )
        return {"ok": True, "message": "quantization finished"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run quantization from Practice YAML.")
    parser.add_argument("--model-type", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--save-path", required=True)
    parser.add_argument("--device", default="npu")
    parser.add_argument("--config-path", default=None)
    parser.add_argument("--quant-type", default=None)
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--tag", default=None, help="JSON array of tag strings")
    args = parser.parse_args()

    ensure_msmodelslim()
    result = run_quantization(
        model_type=args.model_type,
        model_path=args.model_path,
        save_path=args.save_path,
        device=args.device,
        config_path=args.config_path,
        quant_type=args.quant_type,
        trust_remote_code=args.trust_remote_code,
        debug=args.debug,
        tag=parse_optional_json(args.tag),
    )
    return emit_result(result)


if __name__ == "__main__":
    sys.exit(main())
