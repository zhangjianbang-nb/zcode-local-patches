#!/usr/bin/env bash
#
#   ./uninstall.sh                  # 只停用 systemd 自动重打，保留补丁文件
#   ./uninstall.sh --restore        # 还原 bundle 到 pristine（撤销所有补丁）
#   ./uninstall.sh --purge          # 删除 ~/.zcode/patches-lib（先要求确认）
#
set -euo pipefail
HOME_DIR="${HOME:?}"
PATCH_LIB="$HOME_DIR/.zcode/patches-lib"
BACKUP_DIR="$HOME_DIR/.zcode/patch-backups"
NPM_ROOT="$(npm root -g)"
BUNDLE="$NPM_ROOT/zcode-app-cli/vendor/zcode.cjs"

for arg in "$@"; do
  case "$arg" in
    --restore)
      VERSION="$(node -p "require('$NPM_ROOT/zcode-app-cli/package.json').version")"
      SNAP="$BACKUP_DIR/zcode.cjs.$VERSION.pristine"
      if [[ ! -f "$SNAP" ]]; then
        echo "找不到 pristine 快照: $SNAP" >&2
        echo "可以改为重装官方包：npm i -g zcode-app-cli@$VERSION" >&2
        exit 1
      fi
      cp "$SNAP" "$BUNDLE"
      node --check "$BUNDLE" && echo "✅ 已还原 pristine（$VERSION），重启 zcode 生效"
      ;;
    --purge)
      read -r -p "确认删除 $PATCH_LIB ? [y/N] " ans
      [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "已取消"; exit 0; }
      rm -rf "$PATCH_LIB"
      echo "已删除 $PATCH_LIB"
      ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user disable --now zcode-patches.path 2>/dev/null || true
  rm -f "$HOME_DIR/.config/systemd/user/zcode-patches.path" "$HOME_DIR/.config/systemd/user/zcode-patches.service"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "systemd : 已停用并移除 zcode-patches.path"
fi
