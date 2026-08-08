#!/usr/bin/env bash
# 一键启动虚荣视觉工作台；可变数据始终放在包外工作目录。
set -euo pipefail
product_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$product_dir"
export VISION_LAB_DATA_DIR="${VISION_LAB_DATA_DIR:-$product_dir/data}"
exec .venv/bin/python -m labeler.server
