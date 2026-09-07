#!/usr/bin/env bash
# ============================================================
# ccfg-monitor 安装脚本
#
# 将本仓库安装为可直接使用的命令行工具：
#   - 核心文件（ccfg + scripts/ + skills/ + .codex-plugin/ + README）
#     安装到 $PREFIX，默认 $HOME/.local/share/ccfg-monitor
#   - 在 $BIN_DIR 中创建 ccfg 软链接，默认 $HOME/.local/bin
#     （$HOME/.local/bin 通常已在 PATH 中）
#
# 用法：
#   ./install.sh
#   ./install.sh --prefix /opt/ccfg-monitor --bin-dir /usr/local/bin
#   ./install.sh --uninstall
#
# 环境变量（与 ccfg 相同优先级，命令行参数优先）：
#   CCFG_PREFIX   安装目录（默认 $HOME/.local/share/ccfg-monitor）
#   CCFG_BIN_DIR  软链接目录（默认 $HOME/.local/bin）
#   CCR_CONFIG_DIR 运行时配置目录（默认 $HOME/.codex，仅影响安装后的默认行为）
#
# 说明：
#   - ccfg 通过脚本自身位置定位 scripts/ccfg_monitor.py，软链接安装不影响升级；
#     更新时只需重新运行本脚本即可覆盖旧版本。
#   - 安装后运行 ccfg 使用的配置目录默认是 $HOME/.codex，
#     可用 CCR_CONFIG_DIR 覆盖（ccfg help 中也有说明）。
# ============================================================

set -Eeuo pipefail

PREFIX="${CCFG_PREFIX:-$HOME/.local/share/ccfg-monitor}"
BIN_DIR="${CCFG_BIN_DIR:-$HOME/.local/bin}"
UNINSTALL=0

usage() {
    cat <<EOF
用法：
  $0 [选项]

选项：
  --prefix DIR    安装目录（默认 \$HOME/.local/share/ccfg-monitor）
  --bin-dir DIR   ccfg 软链接目录（默认 \$HOME/.local/bin）
  --uninstall     卸载：删除软链接与安装目录
  -h, --help      显示本帮助

环境变量：
  CCFG_PREFIX     同 --prefix
  CCFG_BIN_DIR    同 --bin-dir
EOF
}

die() {
    echo "错误：$*" >&2
    exit 1
}

# ---- 解析命令行参数 ----
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            [[ $# -ge 2 ]] || die "--prefix 需要参数"
            PREFIX="$2"
            shift 2
            ;;
        --prefix=*)
            PREFIX="${1#--prefix=}"
            shift
            ;;
        --bin-dir)
            [[ $# -ge 2 ]] || die "--bin-dir 需要参数"
            BIN_DIR="$2"
            shift 2
            ;;
        --bin-dir=*)
            BIN_DIR="${1#--bin-dir=}"
            shift
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            die "未知参数：$1（运行 $0 --help 查看用法）"
            ;;
    esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ============================================================
# 卸载
# ============================================================
if [[ "$UNINSTALL" -eq 1 ]]; then
    if [[ -L "$BIN_DIR/ccfg" ]]; then
        rm -f "$BIN_DIR/ccfg"
        echo "已删除软链接：$BIN_DIR/ccfg"
    elif [[ -f "$BIN_DIR/ccfg" ]]; then
        echo "注意：$BIN_DIR/ccfg 是普通文件，不是本脚本创建的软链接，已跳过。"
    fi

    # 只删除“看起来像本工具安装目录”的目录，避免误删
    if [[ -f "$PREFIX/ccfg" && -f "$PREFIX/scripts/ccfg_monitor.py" ]]; then
        rm -rf "$PREFIX"
        echo "已删除安装目录：$PREFIX"
    elif [[ -d "$PREFIX" ]]; then
        echo "注意：$PREFIX 不是本工具的安装目录（缺少 ccfg/scripts/ccfg_monitor.py），已跳过删除。"
    fi

    echo
    echo "卸载完成。$HOME/.codex 下的配置与分组未被改动。"
    exit 0
fi

# ============================================================
# 前置检查
# ============================================================
[[ -f "$REPO_DIR/ccfg" ]] || die "仓库中缺少 ccfg（$REPO_DIR/ccfg）"
[[ -f "$REPO_DIR/scripts/ccfg_monitor.py" ]] || die "仓库中缺少 scripts/ccfg_monitor.py"

command -v python3 >/dev/null 2>&1 || die "需要 python3（3.8+；3.11+ 自带 tomllib）"

if ! python3 -c 'import tomllib' >/dev/null 2>&1 && \
   ! python3 -c 'import tomli' >/dev/null 2>&1; then
    echo "警告：当前 python3 既没有 tomllib（Python 3.11+）也没有 tomli。"
    echo "       ccfg 仍可运行，但切换平台时跳过 TOML 格式校验。"
    echo "       建议安装：pip install tomli （以及可选的 pip install tomlkit，用于保留 plugins/mcp_servers 字段）"
fi

# ============================================================
# 安装核心文件
# ============================================================
echo "==> 安装到：$PREFIX"
mkdir -p "$PREFIX" "$BIN_DIR"

install -m 0755 "$REPO_DIR/ccfg" "$PREFIX/ccfg"
install -m 0644 "$REPO_DIR/README.md" "$PREFIX/README.md"
[[ -f "$REPO_DIR/LICENSE" ]] && install -m 0644 "$REPO_DIR/LICENSE" "$PREFIX/LICENSE"

# scripts / skills / .codex-plugin 整目录复制（保留结构，覆盖旧版本）
for sub in scripts skills .codex-plugin; do
    if [[ -d "$REPO_DIR/$sub" ]]; then
        rm -rf "$PREFIX/$sub"
        cp -r "$REPO_DIR/$sub" "$PREFIX/$sub"
    fi
done

# ============================================================
# 创建 / 更新软链接
# ============================================================
if [[ -L "$BIN_DIR/ccfg" ]] || [[ ! -e "$BIN_DIR/ccfg" ]]; then
    ln -sf "$PREFIX/ccfg" "$BIN_DIR/ccfg"
    echo "==> 已创建软链接：$BIN_DIR/ccfg -> $PREFIX/ccfg"
elif [[ -f "$BIN_DIR/ccfg" ]]; then
    backup="$BIN_DIR/ccfg.bak.$(date +%Y%m%d-%H%M%S)"
    mv "$BIN_DIR/ccfg" "$backup"
    ln -s "$PREFIX/ccfg" "$BIN_DIR/ccfg"
    echo "==> $BIN_DIR/ccfg 已存在且是普通文件，备份为 $backup"
    echo "==> 已创建软链接：$BIN_DIR/ccfg -> $PREFIX/ccfg"
else
    die "无法创建软链接：$BIN_DIR/ccfg 已存在且不是普通文件"
fi

# ============================================================
# 冒烟测试：用临时空配置目录验证，不触碰真实配置
# ============================================================
echo "==> 冒烟测试（临时配置目录）..."
TEST_DIR="$(mktemp -d)"
if CCR_CONFIG_DIR="$TEST_DIR" "$BIN_DIR/ccfg" list >/dev/null 2>&1; then
    echo "    通过：ccfg list 可以正常运行。"
    smoke_ok=1
else
    echo "    失败：ccfg list 运行出错，请检查 python3 环境。" >&2
    smoke_ok=0
fi
rm -rf "$TEST_DIR"

if [[ "$smoke_ok" -ne 1 ]]; then
    exit 1
fi

# ============================================================
# 完成
# ============================================================
cat <<EOF

安装完成。

  命令：$BIN_DIR/ccfg
  目录：$PREFIX

快速开始：
  $BIN_DIR/ccfg list        # 查看已有分组
  $BIN_DIR/ccfg status      # 刷新全部平台的额度与可用模型
  $BIN_DIR/ccfg help        # 查看全部命令

配置目录：默认 \$HOME/.codex，可用环境变量覆盖：
  CCR_CONFIG_DIR=/path/to/config $BIN_DIR/ccfg cc

安全提示：
  - 不要在交互 shell 里用 --api-key 传密钥，它会留在 shell 历史中。
    推荐运行 $BIN_DIR/ccfg add <分组名> 后按提示输入，或配合 read -s 使用。
  - 认证文件以 600 权限保存在配置目录，仅用于 Authorization 请求头。
  - 更新本工具：重新下载仓库后再次运行 ./install.sh 即可覆盖升级。
  - 卸载：$0 --uninstall
EOF
