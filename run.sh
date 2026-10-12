#!/usr/bin/env bash
# run.sh —— m260c 语音助手启动脚本
# 激活 lerobot conda 环境并运行 voice_command_demo.py（前台运行，交互式输出）
#
# 用法:
#   ./run.sh                      # 交互式菜单，选择启动模式
#   ./run.sh --ui --log-file run.log   # 直接透传参数给 voice_command_demo.py
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

CONDA_ENV="lerobot"
MAIN_SCRIPT="voice_command_demo.py"

# —— 定位 conda 并激活环境 ——
CONDA_BASE="${CONDA_BASE:-$HOME/miniconda3}"
if [ ! -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
  CONDA_BASE="$(conda info --base 2>/dev/null || true)"
fi
if [ -z "${CONDA_BASE:-}" ] || [ ! -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
  echo "[错误] 找不到 conda，请设置 CONDA_BASE 指向 miniconda 安装目录" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV"
echo "[启动] conda 环境: $CONDA_ENV ($(python --version 2>&1))"
echo "[启动] 工作目录: $PROJECT_DIR"

# —— 大模型 key（未设置则从 .deepseek_key 读取；都没有则程序走桩模式）——
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f "$PROJECT_DIR/.deepseek_key" ]; then
  export DEEPSEEK_API_KEY="$(cat "$PROJECT_DIR/.deepseek_key")"
fi

run() {
  echo "[启动] python $MAIN_SCRIPT $*"
  exec python "$MAIN_SCRIPT" "$@"
}

# 传了参数就直接透传；无参数进交互菜单
if [ "$#" -gt 0 ]; then
  run "$@"
fi

cat <<'MENU'
请选择启动模式:
  1) 带 HUD 界面运行 (推荐)
  2) 无界面运行
  3) Vosk 轻量识别
  4) 只识别已有录音 wav
  5) 硬件测试（唤醒 -> 录音 -> 回放）
  6) 自定义参数
MENU
read -r -p "输入序号 [1]: " choice
choice="${choice:-1}"
case "$choice" in
  1) run --ui --log-file run.log ;;
  2) run --log-file run.log ;;
  3) run --asr vosk --ui --log-file run.log ;;
  4) read -r -p "WAV 文件路径 [a.wav]: " wav; run --wav "${wav:-a.wav}" ;;
  5) echo "[启动] python voice_interact_test.py"; exec python voice_interact_test.py ;;
  6) read -r -p "请输入参数: " args; run $args ;;
  *) echo "[错误] 无效选择: $choice" >&2; exit 1 ;;
esac
