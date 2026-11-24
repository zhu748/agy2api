#!/data/data/com.termux/files/usr/bin/bash
# termux-install.sh — 在 Termux 上原生部署 agy2api（无 proot、无 glibc 容器）
#
# 用法:
#   bash termux-install.sh                 # 安装到 ~/agy2api
#   bash termux-install.sh --dir ~/x       # 安装到指定目录
#   bash termux-install.sh --skip-deps     # 跳过依赖安装（已装好时）
#
# 设计要点（都是踩过的坑）:
#   * pydantic-core 没有 android wheel，必须用 rust 从源码编译；
#     Termux 主源的 rust 1.67 太旧，需要 tur-repo 的 1.100
#   * 编译时 maturin 需要 ANDROID_API_LEVEL
#   * uv 自建的 wheel 会标成 linux_aarch64，与 Termux 的 android_* 标签不兼容，
#     因此这里用 pip 而不是 uv 安装依赖
set -euo pipefail

PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
HOME_DIR="${HOME:-/data/data/com.termux/files/home}"
SVDIR="${SVDIR:-$PREFIX/var/service}"
LOG_DIR="${LOG_DIR:-$PREFIX/var/log/sv/agy2api}"
SERVICE="agy2api"
REPO_URL="https://github.com/zhu748/agy2api.git"
DEFAULT_DIR="$HOME_DIR/agy2api"
MIN_RUST="1.80"

TARGET_DIR=""
SKIP_DEPS=0

usage() {
  sed -n '3,9p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)        TARGET_DIR="${2:-}"; shift 2 ;;
    --skip-deps)  SKIP_DEPS=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

TARGET_DIR="${TARGET_DIR:-$DEFAULT_DIR}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

say()  { printf '\033[1;32m[agy2api]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[agy2api]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[agy2api]\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "${PREFIX:-}" ] && [ -d "$PREFIX" ] || die "这看起来不是 Termux 环境（缺少 \$PREFIX）"

# ---------------------------------------------------------------- 1. 依赖
if [ "$SKIP_DEPS" -eq 0 ]; then
  if ! grep -qs 'tur-packages' "$PREFIX/etc/apt/sources.list.d/tur.list"; then
    say "启用 tur-repo（提供 python3.12 与新版 rust）"
    pkg install -y tur-repo || die "无法安装 tur-repo，请手动执行: pkg install tur-repo"
    pkg update -y || warn "pkg update 失败，继续尝试安装"
  fi

  say "安装依赖: python3.12 rust clang git nodejs-lts uv"
  pkg install -y python3.12 rust clang git nodejs-lts uv
fi

PY="$PREFIX/bin/python3.12"
RUSTC="$PREFIX/bin/rustc"
[ -x "$PY" ] || die "缺少 python3.12（预期路径 $PY）"
[ -x "$RUSTC" ] || die "缺少 rustc"

rust_ver="$("$RUSTC" --version | awk '{print $2}')"
if [ "$(printf '%s\n%s\n' "$MIN_RUST" "$rust_ver" | sort -V | head -1)" != "$MIN_RUST" ]; then
  die "rust $rust_ver 太旧，pydantic-core 需要 >= $MIN_RUST（请确认 tur-repo 已生效）"
fi
say "Python: $("$PY" -V 2>&1) / Rust: $rust_ver"

# ---------------------------------------------------------------- 2. 部署
if [ -f "$TARGET_DIR/.env" ]; then
  die "$TARGET_DIR 已存在 .env，拒绝覆盖；换个目录请用 --dir"
fi

mkdir -p "$TARGET_DIR"
if [ "$SCRIPT_DIR" = "$TARGET_DIR" ] || [ -f "$SCRIPT_DIR/web.py" ]; then
  if [ "$SCRIPT_DIR" != "$TARGET_DIR" ]; then
    say "从当前仓库复制到 $TARGET_DIR"
    tar -C "$SCRIPT_DIR" --exclude=.git --exclude=.venv --exclude=.env -cf - . \
      | tar -C "$TARGET_DIR" -xf -
  fi
else
  say "克隆 $REPO_URL"
  git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
fi
cd "$TARGET_DIR"
[ -f web.py ] || die "$TARGET_DIR 下没有 web.py，仓库不完整"

say "创建虚拟环境并安装依赖（首次会编译 pydantic-core，约 5–15 分钟）"
"$PY" -m venv .venv
.venv/bin/pip install -U pip
ANDROID_API_LEVEL=24 .venv/bin/pip install --no-cache-dir -r requirements.txt

say "生成 .env"
API_PW="$("$PY" -c 'import secrets;print(secrets.token_hex(16))')"
PANEL_PW="$("$PY" -c 'import secrets;print(secrets.token_hex(16))')"
cat > .env <<EOF
HOST=127.0.0.1
PORT=7861
API_PASSWORD=$API_PW
PANEL_PASSWORD=$PANEL_PW
CREDENTIALS_DIR=./creds
PROXY=
RETRY_429_ENABLED=true
RETRY_429_MAX_RETRIES=3
RETRY_429_INTERVAL=8

# 凭据（用户自备；留空则 OAuth 不可用）
# ANTIGRAVITY_CLIENT_ID=你的 client_id
# ANTIGRAVITY_CLIENT_SECRET=你的 client_secret
EOF
chmod 600 .env

cat > "$HOME_DIR/agy2api-credentials.txt" <<EOF
面板地址: http://127.0.0.1:7861
PANEL_PASSWORD=$PANEL_PW
API_PASSWORD=$API_PW
EOF
chmod 600 "$HOME_DIR/agy2api-credentials.txt"

# ---------------------------------------------------------------- 3. 服务与命令
mkdir -p "$HOME_DIR/.local/bin"
install -m 755 "$TARGET_DIR/termux-agyapi.sh" "$HOME_DIR/.local/bin/agyapi"
say "已安装快捷命令: ~/.local/bin/agyapi"

if [ -d "$SVDIR/$SERVICE" ]; then
  warn "检测到已有 $SVDIR/$SERVICE，跳过服务注册（避免覆盖正在运行的实例）"
else
  mkdir -p "$SVDIR/$SERVICE/log" "$LOG_DIR"
  cat > "$SVDIR/$SERVICE/run" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
export HOME="$HOME_DIR"
export PREFIX="$PREFIX"
export PATH="$HOME_DIR/.local/bin:$PREFIX/bin:\$PATH"
export ANDROID_API_LEVEL=24
cd "$TARGET_DIR" || exit 1
set -a; . ./.env; set +a
exec 2>&1
exec ./.venv/bin/python web.py
EOF
  cat > "$SVDIR/$SERVICE/log/run" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
exec svlogd -tt /data/data/com.termux/files/usr/var/log/sv/agy2api
EOF
  chmod 755 "$SVDIR/$SERVICE/run" "$SVDIR/$SERVICE/log/run"
  chmod 700 "$SVDIR/$SERVICE" "$SVDIR/$SERVICE/log" "$LOG_DIR"
  : > "$SVDIR/$SERVICE/down"   # 不自启，由 agyapi 手动拉起
  say "已注册 runit 服务: $SERVICE（默认不自启）"
fi

say "安装完成"
cat <<EOF

  目录      $TARGET_DIR
  面板      http://127.0.0.1:7861
  口令      $HOME_DIR/agy2api-credentials.txt
  启动      agyapi start
  状态      agyapi status
  停止      agyapi stop

  若尚未填写凭据，请编辑 $TARGET_DIR/.env 中的
  ANTIGRAVITY_CLIENT_ID / ANTIGRAVITY_CLIENT_SECRET，然后 agyapi restart。
EOF
