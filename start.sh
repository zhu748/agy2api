#!/bin/bash
# 启动 AGY2API（安全加固：不再自动同步远端代码）
#
# 说明：旧版本启动时会执行 git fetch --all + git reset --hard 强制对齐远端，
# 属于供应链风险（远端被接管即等于下次启动执行任意代码），已移除。
# 如需更新：先人工核对远端仓库可信后执行 git pull --ff-only，再运行本脚本。

uv sync
source .venv/bin/activate
python web.py
