#!/usr/bin/env bash
# zcode 补丁守卫 —— 把全部本地 bundle 补丁的校验/重放收敛成一条命令。
#
# 背景: vendor/zcode.cjs 是 npm 包里的**唯一**补丁目标, 任何 `npm i -g
# zcode-app-cli@latest` 都会整体换代 vendor/ —— 补丁和它们的 .bak 一起被
# reify 删掉, 且 exit 0 完全无提示。2026-09-30 07:03 升到 3.14.4-29 时
# 5 个补丁全丢 (slash-path 更是上一轮 09-24 就丢了, 因为老清单漏记它)。
#
# 用法:
#   ./zcode-patches.sh            # 校验 + 缺失就重放 (默认, 幂等)
#   ./zcode-patches.sh --check    # 只校验, 不写任何文件 (exit 1 = 有缺失)
#   ./zcode-patches.sh --backup   # 只把当前 bundle 快照到包外备份区
#
# 退出码: 0 = 全部补丁就位; 1 = 有缺失/失败 (含锚点漂移需人工比对)
#
# 设计要点:
#   * 备份写到包**外** (~/.zcode/patch-backups/) —— 包内的 .bak-* 会随
#     npm reify 一起消失, 靠它们 revert 是不可靠的。
#   * 打补丁前先等文件写稳定 + node --check 完整性质检: systemd path unit
#     可能在 npm 还没写完时就被触发, 直接改写 14MB 半截文件 = 损坏 bundle。
#   * 只在校验发现缺失时才写盘 —— 否则 path unit 会被自己的写入再次触发。
set -uo pipefail

# 可移植：默认从 `npm root -g` / `npm prefix -g` 推算，补丁目录 = 本脚本所在目录
NPM_ROOT="$(npm root -g 2>/dev/null || true)"
NPM_PREFIX="$(npm prefix -g 2>/dev/null || true)"
BUNDLE="${ZCODE_CJS:-${NPM_ROOT}/zcode-app-cli/vendor/zcode.cjs}"
PKG_JSON="$(dirname "$(dirname "$BUNDLE")")/package.json"
PATCH_DIR="${ZCODE_PATCH_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BACKUP_DIR="${ZCODE_PATCH_BACKUP_DIR:-$HOME/.zcode/patch-backups}"
LOG="$BACKUP_DIR/zcode-patches.log"
LOCK="/tmp/zcode-patches.lock"
# 启动器 = 会被 pgrep 命中的那个命令行; 用来找"补丁前启动、仍跑旧代码"的 zcode
LAUNCHER="${ZCODE_LAUNCHER:-${NPM_PREFIX}/bin/zcode}"

# 顺序: ate/vno/loop-guard 是文档约定的顺序, slash-path 补在最后。
# (老清单漏了 slash-path —— 就是它在上一次升级静默丢失的原因。)
# 2026-10-02 补 truncation-resume (形态③续写文案) —— 它也曾在升级后静默丢失,
# 导致 sess_14159289 当天又跑了一轮「截断→续写→再截断」。
PATCHES=(
  "patch-zcode-ate-contextwindow.py"
  "patch-zcode-vno-buffer.py"
  "patch-zcode-loop-guard.py"
  "patch-zcode-slash-path.py"
  "patch-zcode-truncation-resume.py"
  # 2026-10-20 起请求层补丁改为**纯 JS + 内联**（用户令: 永久、不要 Python）：
  #   patch-zcode-permanent.cjs 把 streamfetch / imagecap / degencap 三个 shim 的
  #   源码直接注入 bundle（不再 require ~/.zcode/patches-lib 的外部文件）。
  # 取代原来的三个安装器：patch-zcode-streamfetch.py / -image-cap.cjs / -degencap.cjs。
  # 真因: TensorFold 对长思考块/超长工具调用整块缓冲，300s 内一个字节都不吐；
  #       undici 默认 bodyTimeout=300s 直接掐断 → Model request failed 重试 5 次。
  #       2026-10-20 实测一次 221713-token 请求：引擎 323.7s 生成 20146 tokens，
  #       客户端 324.75s 报 TypeError: terminated (Fetch.onAborted)。
  "patch-zcode-permanent.cjs"
  # 2026-10-04 补 subagent-watchdog —— streamfetch 之后同类故障上移一层:
  # 子代理看门狗 Jya() 600s 无活动就直接 abort(错误自己还标着 retryable),
  # 三个后台 agent 各因单请求 613~630s 被当场处决, 13 分钟工作全丢。
  # 本补丁把"当场处决"改成"有界延长 5 次"(=6×timeoutMs, 30min 配置下 3h)后再 abort。
  # 全部内联, 不引任何外部文件。
  "patch-zcode-subagent-watchdog.py"
  # 2026-10-04 补 turn-failure-reason —— 与 subagent-watchdog 配套。
  # 看门狗注入的 Gr(fr.ToolTimeout,"Subagent was inactive for Xms") 被
  # JC() 无条件归一成 "Turn was cancelled.", 于是 /tasks 显示得像用户按了 Esc,
  # 下游 Xqo() 还把它判成"取消"不再上报失败。本补丁只让 ToolTimeout 透传。
  "patch-zcode-turn-failure-reason.py"
  # 2026-10-04 补 subagent-limit —— 子代理并发铁律的硬约束。
  # 技术上原本**没有任何闸门**: subagents 配置无并发键; toolConcurrency.maxConcurrency
  # 只管单轮并行工具调用数, 而后台子代理的 handler 立刻返回 async_launched, 槽位随即
  # 释放 —— 8 个子代理照样全跑起来。本补丁在唯一入口 launch() 排队, 上限 4。
  "patch-zcode-subagent-limit.py"
  # image-cap / degencap 已并入上面的 patch-zcode-permanent.cjs（内联到同一段 prelude，
  # 顺序 streamfetch(最内) → imagecap → degencap(最外)，与旧实现完全一致）。
  # 2026-10-04 补 zaicode-compact-threshold —— 覆盖**另一支** ZCode:
  # 桌面/dev 版 `~/.local/bin/ZCode` → `~/.zaicode-dist/releases/*/agent/zcode.cjs`
  # （不是本守卫原先唯一盯着的 npm `vendor/zcode.cjs`）。此前它完全在盲区里，
  # 当天就出了事故: 源码两处入口把「阈值 262k」当 buffer 填（262_000），叠加
  # 该支配置里过期的 contextWindow=292000 → 阈值塌成 9000 → 开局即压 + 熔断。
  # 本脚本覆盖 dist/dev、repo build、桌面打包 agent 三处产物, 并校验配置窗口一致性。
  "patch-zaicode-compact-threshold.py"
  # 2026-10-10 补 no-websearch —— 禁用 provider-native WebSearch 工具。
  # 真因: zai 直连 openai-compatible 引擎后, WebSearch（provider-native, 仅
  # anthropic 分支有实现, default 分支 throw）仍被声明进工具清单; 模型调用
  # 无结果 → 把 `<tool_call>WebSearch>` 吐成纯文本 → 不被解析 → 反复重试
  # （sess_fe7c356b 连试 4 次, tool_usage 零记录）。modelConfigRules 路由
  # supportsNativeWebSearch:false 实测两轮不生效, 故补丁 Hwa() 恒 false,
  # 把 WebSearch 从工具清单剔除。WebFetch 等客户端工具不受影响。
  "patch-zcode-no-websearch.py"
  # 2026-10-10 补 no-safety —— 删除 system prompt 的安全防线段（用户令）。
  # 真因: bundle 常量 mno（buildSecurityNotice）把 "IMPORTANT: Assist with
  # authorized security testing... Refuse requests for..." 注入主 agent (CJs)
  # 与 workflow 子代理 (RJs) 的 system prompt；用户令删除该段、必须执行用户指令。
  # 补丁 = mno 置空串（引用点 hno/CJs/RJs 全保持合法，join 仅多一空行）。
  "patch-zcode-no-safety.py"
)

MODE="apply"
for a in "$@"; do
  case "$a" in
    --check)  MODE="check" ;;
    --backup) MODE="backup" ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
  esac
done

mkdir -p "$BACKUP_DIR"
exec 9>"$LOCK"
# 抢锁必须"等一下"而不是静默放弃: 早先 flock -n 静默 exit 0, 会让 --check
# 在另一个实例(常是 path unit 刚触发的那个)持锁时打出**假绿灯** —— 手动核验
# 明明有补丁缺失却被告知一切正常。check 也不静默: 超时即 exit 2 说清楚。
# check 要快问快答 (人工核验不该干等), apply 才值得长等
WAIT=180; [[ "$MODE" == "check" ]] && WAIT=20
if ! flock -w "$WAIT" 9; then
  if [[ "$MODE" == "check" ]]; then
    echo "❌ 另一个 zcode-patches 实例持锁 ${WAIT}s 未放, 无法核验 —— 不要当作'检查通过'" >&2
    exit 2
  fi
  echo "❌ 另一个 zcode-patches 实例持锁 ${WAIT}s 未放, 本次未执行" >&2
  exit 1
fi

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }
die() { log "❌ $*"; exit 1; }

# npm reify 是「rm + rename 替换」，bundle 会**短暂消失**。早先这里立刻 die，
# 让 path unit 在一秒内连挂两次 → systemd 判 unit-start-limit-hit 停用 unit →
# 2026-10-03 那天守卫死了 18 小时，补丁全丢而无人知晓（事故复盘见
# zcode-tensorfold-bodytimeout-buffered）。给它一个复现窗口，只在真的没有时才放弃。
wait_appear() {
  local n=0
  while [[ ! -f "$BUNDLE" ]]; do
    n=$((n + 1))
    (( n % 10 == 0 )) && log "  等 bundle 出现… ${n}s"
    (( n > 120 )) && return 1
    sleep 1
  done
  return 0
}
wait_appear || die "bundle 120s 内始终未出现 (npm 是不是装坏了?): $BUNDLE"

VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PKG_JSON" 2>/dev/null | head -1)"
VERSION="${VERSION:-unknown}"

# --- 等文件写稳定 (npm reify 可能正在写) -------------------------------
wait_stable() {
  local prev="" cur="" n=0
  for _ in $(seq 1 60); do
    cur="$(stat -c '%s-%Y' "$BUNDLE" 2>/dev/null || echo missing)"
    if [[ "$cur" == "$prev" && "$cur" != "missing" ]]; then
      n=$((n + 1)); [[ $n -ge 3 ]] && return 0
    else
      n=0
    fi
    prev="$cur"; sleep 1
  done
  return 1
}

# --- 完整性质检: 半截/损坏的 bundle 绝不能往上打补丁 --------------------
# 致命判据只有「语法可解析 + 尺寸合理」——两者足以挡住 npm 半截写入。
# 锚点**不能**当判据: 补丁打完上游锚点本就消失 (slash-path 后
# branch_pristine=no), 拿它判会把已打补丁的 bundle 误判成损坏。
# 锚点漂移的正确定位方式是让补丁脚本自己去拒绝并回滚 (它会打印诊断)。
integrity_ok() {
  node --check "$BUNDLE" >/dev/null 2>&1 || return 1
  local sz; sz="$(stat -c %s "$BUNDLE" 2>/dev/null || echo 0)"
  [[ "$sz" -ge 10000000 ]] || return 1
  # 常量名 Vno 上游若再次改名, 只告警不阻断 —— 让 vno 补丁脚本去报错
  grep -q 'Vno=' "$BUNDLE" || log "⚠️  bundle 里找不到常量 Vno (上游可能又改名了), 继续尝试"
  return 0
}

# --- 包外 pristine 快照 (按版本存, 不覆盖) ------------------------------
snapshot() {
  local dst="$BACKUP_DIR/zcode.cjs.$VERSION.pristine"
  if [[ -f "$dst" ]]; then
    log "pristine 快照已存在: $dst"
  else
    cp -p "$BUNDLE" "$dst" || die "快照失败"
    log "已存 pristine 快照: $dst ($(stat -c %s "$dst") B)"
  fi
  sha256sum "$dst" | tee -a "$LOG"
}

# --- 逐个补丁: 校验 -> 缺失则重放 ---------------------------------------
# 2026-10-07 起支持两种安装器: *.py 用 python3, *.cjs 用 node（用户令:
# 补丁逻辑与 zcode 同语言, image-cap 补丁即为首个 .cjs 安装器）。
patch_runner() {
  case "$1" in
    *.py)  echo python3 ;;
    *.cjs) echo node ;;
    *)     echo python3 ;;
  esac
}
run_patch() {
  local script="$PATCH_DIR/$1" name="$1"
  [[ -f "$script" ]] || { log "❌ 缺脚本: $script"; return 2; }
  local runner; runner="$(patch_runner "$script")"
  if "$runner" "$script" --check >/dev/null 2>&1; then
    log "  ✅ 已就位  $name"
    return 0
  fi
  if [[ "$MODE" == "check" ]]; then
    log "  ❌ 缺失    $name"
    return 1
  fi
  log "  🔧 缺失, 重放 $name"
  local out rc
  out="$("$runner" "$script" 2>&1)"; rc=$?
  printf '%s\n' "$out" | tee -a "$LOG" | sed 's/^/      /'
  if [[ $rc -ne 0 ]] || ! "$runner" "$script" --check >/dev/null 2>&1; then
    log "  ❌ 重放失败 $name (exit=$rc) —— 可能是锚点漂移, 需人工比对源码"
    return 1
  fi
  log "  ✅ 重放成功 $name"
  return 0
}

# --- 盲区告警: 补丁写回磁盘 ≠ 生效 --------------------------------------
# 2026-09-30 事故暴露的洞: npm 升级清补丁 → 守卫把补丁重打回磁盘,
# 但**已经在跑的 zcode 进程仍用内存里的旧代码**。当天 07:03 升级、09:07 补丁
# 写回、直到 09:12:53 用户重启才生效 —— 中间 2 小时里唯一活跃会话
# sess_eb54f222 从 32k 涨到 212,685 tokens 一次没压(阈值被算成 228000)。
# 守卫不能替你杀会话(正在跑的活会丢), 但必须把这件事喊出来。
warn_stale_runners() {
  local bm; bm="$(stat -c %Y "$BUNDLE" 2>/dev/null)" || return 0
  local pid cl found=0
  for pid in $(pgrep -x node 2>/dev/null); do
    # 精确比对 cmdline, 避免匹配到调用本脚本的 shell 自身
    cl="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
    [[ "$cl" == "node $LAUNCHER " || "$cl" == "node $LAUNCHER" ]] || continue
    local st; st="$(stat -c %Y "/proc/$pid" 2>/dev/null)" || continue
    if (( st < bm )); then
      found=1
      log "⚠️  PID $pid 启动于补丁写入之前 ($(ps -o lstart= -p "$pid" 2>/dev/null | xargs))"
      log "    它仍在用**旧的**阈值/守卫 —— 必须重启该 zcode 会话才生效。"
    fi
  done
  (( found )) && log "    (守卫不替你重启: 会话里正在跑的活会丢。重启命令 = 退出后重开 zcode)"

  # --- 桌面/dev 版单列一段 ---------------------------------------------
  # 它的 argv[0] 被改成 `zcode-cli`（进程里看不到路径），所以上面按命令行的
  # pgrep 匹配不到它 —— 2026-10-04 事故就是从这条缝里冒出去的。
  local za za_mtime newest=0 p st cl2
  for za in "$HOME"/.zaicode-dist/releases/*/agent/zcode.cjs; do
    [[ -f "$za" ]] || continue
    za_mtime="$(stat -c %Y "$za" 2>/dev/null)" || continue
    (( za_mtime > newest )) && newest=$za_mtime
  done
  if (( newest > 0 )); then
    found=0
    for pid in $(pgrep -x zcode-cli 2>/dev/null); do
      st="$(stat -c %Y "/proc/$pid" 2>/dev/null)" || continue
      if (( st < newest )); then
        found=1
        log "⚠️ ZCode 桌面/dev 会话 PID $pid 启动于 agent 补丁之前 ($(ps -o lstart= -p "$pid" 2>/dev/null | xargs))"
        log "    仍在用旧的 compact bufferTokens(=旧阈值) —— 退出后重开 ZCode 才生效。"
      fi
    done
    (( found )) || log "  (桌面/dev 版: 无正在跑的旧会话)"
  fi
  return 0
}

# ---------------------------------------------------------------- main ---
log "===== zcode-patches mode=$MODE bundle=$BUNDLE version=$VERSION ====="

wait_stable || die "bundle 60s 内仍在变动 (npm 还在装?), 放弃本次打补丁"
integrity_ok || die "bundle 完整性检查失败 (node --check 或锚点缺失), 拒绝改写"

if [[ "$MODE" == "backup" ]]; then snapshot; log "完成 (仅快照)"; exit 0; fi
snapshot

fail=0
for p in "${PATCHES[@]}"; do run_patch "$p" || fail=1; done

warn_stale_runners

if [[ $fail -eq 0 ]]; then
  log "===== 全部 ${#PATCHES[@]} 个补丁就位 (version=$VERSION) ====="
  exit 0
fi
log "===== 有补丁未就位, 见上 ====="
exit 1
