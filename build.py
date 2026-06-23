#!/usr/bin/python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# This file is part of the MindStudio project.
# Copyright (c) 2025 Huawei Technologies Co.,Ltd.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#          http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------
import argparse
import logging
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class BuildManager:
    """
    统一构建管理：编译 → 归档。

    用法:
        python build.py                             完整构建
        python build.py --version/-v <version>      指定构建版本号
        python build.py --extra/-e KEY=VALUE        指定额外构建选项，可多次使用
        python build.py test                        单元测试（待补）

    参数说明:
        - 参数: command    : 构建动作: 为空时为全构建, test 为运行单元测试。
        - 参数: --version  : 构建版本号，不传时默认 1.0.0。
        - 参数: --extra    : 额外构建选项，格式为 KEY=VALUE，可多次指定。
    """

    def __init__(self):
        self.project_root = Path(__file__).resolve().parent
        ap = argparse.ArgumentParser(description='Build the project and optionally run tests.')
        ap.add_argument(
            'command',
            nargs='*',
            default=[],
            help='Build action: omit for full build, "test" to run unit tests',
        )
        ap.add_argument(
            '-v',
            '--version',
            type=str,
            default='1.0.0',
            help='Build version (default: 1.0.0)',
        )
        ap.add_argument(
            '-e',
            '--extra',
            metavar='KEY=VALUE',
            action='append',
            default=[],
            help='Extra build options in KEY=VALUE format, can be specified multiple times',
        )
        self.args = ap.parse_args()

    def _execute_command(self, cmd, timeout_seconds=36000, cwd=None, env=None):
        logging.info("Running: %s", " ".join(cmd))
        subprocess.run(cmd, timeout=timeout_seconds, check=True, cwd=cwd, env=env)

    def _get_extra_options(self):
        opts = {}
        for opt in self.args.extra:
            key, _, val = opt.partition('=')
            opts[key] = val
        return opts

    def _archive_artifacts(self, src_dir, pattern):
        artifacts_dir = self.project_root / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)
        for src in Path(src_dir).glob(pattern):
            shutil.copy2(src, artifacts_dir)
            logging.info("Archived %s -> %s", src, artifacts_dir / src.name)

    def _build_whl(self):
        dist_dir = self.project_root / "dist"
        # 清理旧版本 whl
        shutil.rmtree(dist_dir, ignore_errors=True)

        env = os.environ.copy()
        env["WHL_VERSION"] = self.args.version
        self._execute_command(["bash", "scripts/build_whl.sh"], cwd=self.project_root, env=env)

        self._archive_artifacts(dist_dir, "*.whl")

    def run(self):
        os.chdir(self.project_root)

        if self.args.command and 'test' in self.args.command:
            # -------------------- 单元测试 --------------------
            # 后续补充测试逻辑
            logging.info("Tests are not yet implemented.")
        else:
            # -------------------- 产品构建 --------------------
            logging.info("WHL_VERSION: %s", self.args.version)
            extra_options = self._get_extra_options()
            for key, val in extra_options.items():
                logging.info("--extra: %s = %s", key, val)
            self._build_whl()


if __name__ == "__main__":
    try:
        BuildManager().run()
    except Exception:
        logging.error("Unexpected error: %s", traceback.format_exc())
        sys.exit(1)
