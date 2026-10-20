#!/usr/bin/env bash
#
# zcode-local-patches installer
#
#   ./install.sh                 # 只装请求层永久补丁（纯 JS，无 Python）
#   ./install.sh --full          # 另装其余补丁（需要 python3）+ 可选 systemd 自动重打
#   ./install.sh --dry-run       # 只打印将要做什么
#   ./install.sh --version=3.15.3-37
#
# 目标机前置：npm i -g zcode-app-cli@3.15.3-37
set -euo pipefail

PINNED_VERSION="3.15.3-37"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME:?HOME 未设置}"
PATCH_LIB="$HOME_DIR/.zcode/patches-lib"
BACKUP_DIR="$HOME_DIR/.zcode/patch-backups"

FULL=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --dry-run) DRY=1 ;;
    --version=*) PINNED_VERSION="${arg#*=}" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

run() {
  if [[ $DRY -eq 1 ]]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少依赖: $1" >&2; exit 1; }; }
need node
need npm

NPM_ROOT="$(npm root -g)"
BUNDLE="$NPM_ROOT/zcode-app-cli/vendor/zcode.cjs"
PKG_JSON="$NPM_ROOT/zcode-app-cli/package.json"

if [[ ! -f "$BUNDLE" ]]; then
  cat >&2 <<EOF
没找到 zcode：$BUNDLE
先安装官方包（版本要与补丁一致）：
  npm i -g zcode-app-cli@$PINNED_VERSION
EOF
  exit 1
fi

INSTALLED="$(node -p "require('$PKG_JSON').version")"
echo "zcode 版本 : $INSTALLED"
echo "bundle     : $BUNDLE"
if [[ "$INSTALLED" != "$PINNED_VERSION" ]]; then
  cat >&2 <<EOF
⚠️ 补丁的锚点是按 $PINNED_VERSION 编译产物写的，当前装的是 $INSTALLED。
   先执行：npm i -g zcode-app-cli@$PINNED_VERSION
   （仍要继续可加 --version=$INSTALLED，但很可能因 minifier 改名而失配）
EOF
  exit 1
fi

echo "补丁目录   : $PATCH_LIB"
run mkdir -p "$PATCH_LIB" "$BACKUP_DIR"

# ---- 1) 请求层永久补丁：streamfetch + imagecap + degencap 内联进 bundle ----
run install -m 0644 "$ROOT"/lib/shims/*.cjs "$PATCH_LIB"/
run install -m 0755 "$ROOT/lib/patch-zcode-permanent.cjs" "$PATCH_LIB"/
if [[ $DRY -eq 0 ]]; then
  ZCODE_CJS="$BUNDLE" ZCODE_PATCH_LIB="$PATCH_LIB" \
    node "$PATCH_LIB/patch-zcode-permanent.cjs"
  ZCODE_CJS="$BUNDLE" ZCODE_PATCH_LIB="$PATCH_LIB" \
    node "$PATCH_LIB/patch-zcode-permanent.cjs" --check
fi

# ---- 2) 其余补丁（--full）：需要 python3 ----
if [[ $FULL -eq 1 ]]; then
  need python3
  run cp "$ROOT"/extra/*.py "$PATCH_LIB"/
  run cp "$ROOT/zcode-patches.sh" "$PATCH_LIB"/
  run chmod +x "$PATCH_LIB/zcode-patches.sh"
  if [[ $DRY -eq 0 ]]; then
    # 这些脚本历史上写死了 /home/whu，安装时替换成当前用户家目录
    sed -i "s|/home/whu|$HOME_DIR|g" "$PATCH_LIB"/*.py "$PATCH_LIB/zcode-patches.sh" 2>/dev/null || true
    ZCODE_CJS="$BUNDLE" ZCODE_PATCH_DIR="$PATCH_LIB" "$PATCH_LIB/zcode-patches.sh" || true
  fi

  # ---- 3) systemd path unit：npm 升级覆写 bundle 后自动重打 ----
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    UNIT_DIR="$HOME_DIR/.config/systemd/user"
    run mkdir -p "$UNIT_DIR"
    if [[ $DRY -eq 0 ]]; then
      sed "s|/home/whu|$HOME_DIR|g" "$ROOT/systemd/zcode-patches.path" > "$UNIT_DIR/zcode-patches.path"
      sed "s|/home/whu|$HOME_DIR|g" "$ROOT/systemd/zcode-patches.service" > "$UNIT_DIR/zcode-patches.service"
      systemctl --user daemon-reload
      systemctl --user enable --now zcode-patches.path
      echo "systemd : zcode-patches.path 已启用"
    else
      echo "[dry-run] 安装 systemd 单元到 $UNIT_DIR 并 enable --now zcode-patches.path"
    fi
  else
    echo "systemd : 不可用（跳过自动重打；npm 升级后需手动重跑 ./install.sh）"
  fi
fi

cat <<EOF

✅ 完成。补丁只对**新启动**的 zcode 进程生效：
   退出当前 zcode 会话后重新打开（或 zcode --resume <sess_id>）。
   验证：zcode -p "Reply with ok" --mode yolo
   检查：node $PATCH_LIB/patch-zcode-permanent.cjs --check
EOF
