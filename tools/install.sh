#!/usr/bin/env bash
#
# XG-CLI 原生安装器（macOS / Linux / WSL）
#
# 用法:
#   bash install.sh          # 安装（若已装则升级到最新）
#
# 效果: 把 xg-cli 装进用户级虚拟环境 ~/.xg-cli/venv，
#       并自动将其 bin 目录加入 shell PATH（写入 ~/.bashrc / ~/.zshrc / ~/.profile）。
#       完成后重开终端即可直接执行: xg-cli
#
# 与 Claude Code 类似的思路: 安装器负责把命令所在目录放进 PATH，
# 这样用户 pip 装完就能直接用，不用手动改系统 PATH。

set -euo pipefail

PACKAGE="xg-cli"
INSTALL_ROOT="${XG_CLI_HOME:-$HOME/.xg-cli}"
VENV_DIR="$INSTALL_ROOT/venv"
VENV_BIN="$VENV_DIR/bin"

log()  { printf '=> %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }

# ---- 1) 定位 Python 3.11+ ----
PY=""
for cand in python3 python3.13 python3.12 python3.11; do
    if command -v "$cand" >/dev/null 2>&1; then
        PY="$cand"
        break
    fi
done
if [[ -z "$PY" ]]; then
    printf '!! 未找到 Python 3.11+。请先安装 Python 3（https://www.python.org/）后重试。\n' >&2
    exit 1
fi
PYVER="$("$PY" -c 'import sys,sysconfig; print("%d.%d" % sys.version_info[:2]); print(sys.executable)' 2>/dev/null | head -n1)"
log "使用 Python $PYVER ($(command -v "$PY"))"

# ---- 2) 创建虚拟环境（幂等） ----
if [[ ! -x "$VENV_BIN/python" ]]; then
    log "创建虚拟环境 $VENV_DIR"
    "$PY" -m venv "$VENV_DIR"
fi

# ---- 3) 安装 / 升级 xg-cli ----
MODE="install"
if "$VENV_BIN/pip" show -q "$PACKAGE" >/dev/null 2>&1; then
    MODE="upgrade"
fi
log "$MODE $PACKAGE ..."
"$VENV_BIN/pip" install --upgrade "$PACKAGE"
if [[ ! -x "$VENV_BIN/xg-cli" ]]; then
    printf '!! 安装完成但找不到 xg-cli 命令，请检查上方输出。\n' >&2
    exit 1
fi

# ---- 4) 把 venv/bin 加入 shell PATH（缺失才写） ----
LINE="export PATH=\"$VENV_BIN:\$PATH\""
RC_FILES=()
[[ -f "$HOME/.zshrc" ]]       && RC_FILES+=( "$HOME/.zshrc" )
[[ -f "$HOME/.bashrc" ]]      && RC_FILES+=( "$HOME/.bashrc" )
[[ -f "$HOME/.bash_profile" ]] && RC_FILES+=( "$HOME/.bash_profile" )
if (( ${#RC_FILES[@]} == 0 )); then
    RC_FILES+=( "$HOME/.profile" )
fi

added=0
for rc in "${RC_FILES[@]}"; do
    if grep -qF "$VENV_BIN" "$rc" 2>/dev/null; then
        continue
    fi
    printf '\n# XG-CLI PATH\n%s\n' "$LINE" >> "$rc"
    added=1
done

# ---- 5) 当前会话即时可用 + 校验 ----
export PATH="$VENV_BIN:$PATH"
log "版本: $("$VENV_BIN/xg-cli" --version 2>/dev/null || echo "未知")"

echo
if (( added )); then
    echo "已把 $VENV_BIN 加入 shell 启动配置。"
    echo "请重开终端（或执行 source ~/.bashrc）后，直接运行: xg-cli"
else
    echo "PATH 已包含 $VENV_BIN，可直接运行: xg-cli"
fi