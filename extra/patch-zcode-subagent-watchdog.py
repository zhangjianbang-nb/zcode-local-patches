#!/usr/bin/env python3
"""子代理看门狗改为「有界延长」而非「当场处决」

## 问题（2026-10-04 取证）

zcode 报 `✗ Background task needs attention · agent_xxx · /tasks`，三个后台子代理
（写答案 0007-0038 / 0199-0230 / 0231-0262）全部阵亡，metadata.json 落的是
`status:"failed"`、`error:"Turn was cancelled."` —— 看起来像"用户取消了"。

真因在 model IO 日志里（~/.zcode/cli/debug/model-io-sess_subagent_agent_*.jsonl）：

  * 三个 agent 各自发出的**最后一次请求**耗时 613550 / 619979 / 630006 ms；
  * 错误是 `ToolTimeout: Subagent was inactive for 600000ms`；
  * 存活的 agent_3c0c3dc2 同时段单次请求就跑 **305502 ms** —— 后端常态就是 5 分钟级。

即：TensorFold 对超长生成整块缓冲、完成才 flush（见 patch-zcode-streamfetch.py 的
305s 取证）。那个补丁拆掉了 undici 的 300s bodyTimeout，**同类故障于是上移了一层** ——
现在卡住它们的是子代理看门狗 `Jya()` 的 600s（`YV = 6e5`）。

看门狗抛的那个错误**自己标着 `retryable:!0`**，但代码路径是直接 `e.abort(u)` 终结
整个 turn，一次都不重试。健康的慢请求 = 直接判死，13 分钟的工作全丢。

## 本补丁

把 `Jya()` 定时器回调里"算出 idle 时长 → 立刻造错 → abort"改成"先看延长预算"：

  * 预算 5 次（`__zcWatchdogExt` 记在 options 对象 e 上），每次延长会把计时基准 t
    重置、重新调度 —— 即最多等到 **6 × timeoutMs** 才真的动手（默认 30 分钟配置下 = 3 小时）；
  * 每次延长都打一条 `warn`（event=subagent.activity_timeout_extended, status=running），
    所以 "/tasks 里没消息但一直在跑"这件事在日志里可见，不是静默空转；
  * 预算用尽后才走原来的 abort 路径，原始错误文本一字不改。

**不改变**真正的取消语义：`e.abort` 仍是唯一的终止手段，调用方的 AbortSignal
（用户按 Esc）依旧当场生效。

## 全部改动都在 bundle 内联，不引任何外部文件

注入的 JS 只用到闭包里本来就有的 `t` / `s` / `e`，**没有 require / import / 外部 shim**，
所以不存在"shim 路径失效 → 静默降级"这个失败面（这正是 streamfetch 补丁要多一层 try/catch
兜底的原因）。本脚本本身只是**施加器**：bundle 是 npm 管的文件，升级即整体换代，
必须有个包外的程序能在升级后把改动重新打回去（见 zcode-patches.sh）。

## 改动点（一处）

锚点（全 bundle 唯一）：
    let l=Date.now()-t,u=Gr(fr.ToolTimeout,`Subagent was inactive for ${e.timeoutMs}ms`,{context:

预算数字是**正则识别**的（`__zcW<\\d+`），所以任何历史预算（<3 / <5 / <10 …）
都能被 check / revert / 原地升级，不必逐个硬编码。

用法：
    python3 patch-zcode-subagent-watchdog.py --check     # 查状态（exit 1 = 缺失/预算不符）
    python3 patch-zcode-subagent-watchdog.py             # 打补丁 / 把预算改成当前值（幂等）
    python3 patch-zcode-subagent-watchdog.py --revert    # 还原（任何预算形态都能还原）
    python3 patch-zcode-subagent-watchdog.py --dry-run   # 只校验锚点

⚠️ 升级 zcode 会静默丢掉本补丁，已登记进 ~/workspace/ai_workspace/zcode-patches.sh。
⚠️ 打补丁后需**重启 zcode 进程**才生效。
⚠️ 看门狗阈值本身另受 setting.json 的 `subagents.inactivityTimeoutMs` 控制
   （2026-10-04 已从默认 600000 上调到 1800000）；本补丁管的是"到点之后怎么办"。
"""

import argparse
import os
import re
import subprocess
import sys
import time

DEFAULT_BUNDLE = os.environ.get(
    "ZCODE_CJS", "/home/whu/.npm-global/lib/node_modules/zcode-app-cli/vendor/zcode.cjs"
)

# ★ 2026-10-20：minifier 每次重编都会换名（l→c、Gr→Ur、fr→nr 等），
#   原来的字面量锚点因此静默失配。改成按形状匹配，变量名全部用命名组带回。
ANCHOR_RE = re.compile(
    r"let (?P<idle>[A-Za-z0-9_$]+)=Date\.now\(\)-t,(?P<u>[A-Za-z0-9_$]+)="
    r"(?P<err>[A-Za-z0-9_$]+)\((?P<mod>[A-Za-z0-9_$]+)\.ToolTimeout,"
    r"`Subagent was inactive for \$\{e\.timeoutMs\}ms`,\{context:")


def orig_form(m):
    return (
        f"let {m.group('idle')}=Date.now()-t,{m.group('u')}="
        f"{m.group('err')}({m.group('mod')}.ToolTimeout,"
        "`Subagent was inactive for ${e.timeoutMs}ms`,{context:"
    )

# 延长逻辑 = HEAD + 预算数字 + TAIL，数字用正则匹配 → 历史预算全都认得
BUDGET_HEAD = (
    "let l=Date.now()-t,__zcW=e.__zcWatchdogExt||0;"
    "if(__zcW<"
)
BUDGET_TAIL = (
    "&&!e.signal.aborted){"
    "e.__zcWatchdogExt=__zcW+1;t=Date.now();"
    'e.logger?.warn("Explore subagent activity watchdog extended",'
    "{...Qa(e.lifecycle.runTraceContext),agentId:e.lifecycle.agentId,"
    "agentType:e.request.agentType,event:\"subagent.activity_timeout_extended\","
    "idleMs:l,extension:__zcW+1,module:\"core.subagent\","
    "parentToolCallId:e.request.parentToolCallId,status:\"running\","
    "timeoutMs:e.timeoutMs});s();return}"
    "u=Gr(fr.ToolTimeout,"
    "`Subagent was inactive for ${e.timeoutMs}ms`,{context:"
)

MARK = "__zcWatchdogExt"
MAX_EXTENSIONS = 5
PATCHED_RE = re.compile(
    r"let (?P<idle>[A-Za-z0-9_$]+)=Date\.now\(\)-t,__zcW=e\.__zcWatchdogExt\|\|0;"
    r"if\(__zcW<(?P<n>\d+)&&!e\.signal\.aborted\)\{"
    r"e\.__zcWatchdogExt=__zcW\+1;t=Date\.now\(\);"
    r'e\.logger\?\.warn\("Explore subagent activity watchdog extended",'
    r"\{\.\.\.Qa\(e\.lifecycle\.runTraceContext\),agentId:e\.lifecycle\.agentId,"
    r'agentType:e\.request\.agentType,event:"subagent\.activity_timeout_extended",'
    r"idleMs:(?P=idle),extension:__zcW\+1,module:\"core\.subagent\","
    r"parentToolCallId:e\.request\.parentToolCallId,status:\"running\","
    r"timeoutMs:e\.timeoutMs\}\);s\(\);return\}"
    r"(?P<u>[A-Za-z0-9_$]+)=(?P<err>[A-Za-z0-9_$]+)\((?P<mod>[A-Za-z0-9_$]+)\.ToolTimeout,"
    r"`Subagent was inactive for \$\{e\.timeoutMs\}ms`,\{context:")


def patched_form(n, m):
    idle, u = m.group("idle"), m.group("u")
    err, mod = m.group("err"), m.group("mod")
    return (
        f"let {idle}=Date.now()-t,__zcW=e.__zcWatchdogExt||0;"
        f"if(__zcW<{n}&&!e.signal.aborted){{"
        "e.__zcWatchdogExt=__zcW+1;t=Date.now();"
        'e.logger?.warn("Explore subagent activity watchdog extended",'
        '{...Qa(e.lifecycle.runTraceContext),agentId:e.lifecycle.agentId,'
        'agentType:e.request.agentType,event:"subagent.activity_timeout_extended",'
        f"idleMs:{idle},extension:__zcW+1,module:\"core.subagent\","
        "parentToolCallId:e.request.parentToolCallId,status:\"running\","
        "timeoutMs:e.timeoutMs});s();return}"
        f"{u}={err}({mod}.ToolTimeout,"
        "`Subagent was inactive for ${e.timeoutMs}ms`,{context:"
    )


def read_bundle(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write_bundle(path, src):
    tmp = path + ".tmp-watchdog"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(src)
    os.replace(tmp, path)


def node_check(path):
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        return False, f"跑不了 node --check: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:2000]
    return True, ""


def wait_stable(path, seconds=2.0, interval=0.3):
    """等文件写稳定（systemd path unit 可能在 npm 还没写完时触发）。"""
    last = None
    stable = 0.0
    while stable < seconds:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return False
        cur = (st.st_size, st.st_mtime_ns)
        if cur == last:
            stable += interval
        else:
            stable = 0.0
            last = cur
        time.sleep(interval)
    return True


def describe(src):
    budgets = [int(m.group("n")) for m in PATCHED_RE.finditer(src)]
    return {
        "has_mark": MARK in src,
        "n_orig": len(ANCHOR_RE.findall(src)),
        "budgets": budgets,
        "n_watchdog": src.count("Subagent was inactive for"),
    }


def _commit(bundle, src, new, action):
    """写盘 + node --check，失败回滚。"""
    write_bundle(bundle, new)
    ok, err = node_check(bundle)
    if not ok:
        print(f"❌ node --check 失败，正在回滚:\n{err}", file=sys.stderr)
        write_bundle(bundle, src)
        return 1
    print(action)
    return 0


def do_check(bundle):
    if not os.path.exists(bundle):
        print(f"找不到 bundle: {bundle}", file=sys.stderr)
        return 1
    src = read_bundle(bundle)
    info = describe(src)
    ok = (
        info["n_orig"] == 0
        and info["budgets"] == [MAX_EXTENSIONS]
        and info["n_watchdog"] == 1
    )

    print(f"bundle = {bundle}")
    print(f"  延长逻辑已注入 : {info['has_mark']}")
    print(f"  预算(__zcW<N)  : {info['budgets'] or '无'}（应为 [{MAX_EXTENSIONS}]）")
    print(f"  原始锚点       : {info['n_orig']} 处（应为 0）")
    print(f"  看门狗错误文本 : {info['n_watchdog']} 处（应为 1，证明没误伤别的分支）")
    print("✅ 补丁就位" if ok else "❌ 补丁缺失/预算不符 —— 跑不带参数的本脚本重放")
    return 0 if ok else 1


def apply_patch(bundle, dry_run=False):
    if not os.path.exists(bundle):
        print(f"找不到 bundle: {bundle}", file=sys.stderr)
        return 1
    if not wait_stable(bundle):
        print(f"bundle 不存在或一直在变: {bundle}", file=sys.stderr)
        return 1

    src = read_bundle(bundle)
    info = describe(src)

    # 已是当前预算 → 幂等
    if info["budgets"] == [MAX_EXTENSIONS] and info["n_orig"] == 0:
        print(f"✅ 已经是补丁状态（预算 {MAX_EXTENSIONS}），无需重放")
        return 0

    # 已有延长逻辑但预算不同（历史形态）→ 原地改预算，不要求先 revert
    if len(info["budgets"]) == 1 and info["n_orig"] == 0:
        old = info["budgets"][0]
        if dry_run:
            print(f"--dry-run：预算 {old} → {MAX_EXTENSIONS}，未写盘")
            return 0
        new = PATCHED_RE.sub(lambda m: patched_form(MAX_EXTENSIONS, m), src, count=1)
        return _commit(
            bundle, src, new,
            f"✅ 已把看门狗预算 {old} → {MAX_EXTENSIONS}"
            f"（单次等待上限 {MAX_EXTENSIONS + 1} × timeoutMs）；node --check 通过",
        )

    # 干净 bundle → 首次打补丁
    if info["n_orig"] != 1 or info["budgets"]:
        print(
            f"❌ 锚点异常：原始 {info['n_orig']} 处（应为 1），已有延长逻辑 {info['budgets']}。\n"
            f"   bundle 可能已升级或被打过其它补丁 —— 请人工比对，不要硬打。",
            file=sys.stderr,
        )
        return 1
    if info["n_watchdog"] != 1:
        print(
            f"❌ 'Subagent was inactive for' 出现 {info['n_watchdog']} 处（应为 1），拒绝改写",
            file=sys.stderr,
        )
        return 1
    if dry_run:
        print("--dry-run：锚点正常，未写盘")
        return 0

    _m = ANCHOR_RE.search(src)
    new = src[:_m.start()] + patched_form(MAX_EXTENSIONS, _m) + src[_m.end():]
    return _commit(
        bundle, src, new,
        "✅ 已打补丁：\n"
        f"   1. 看门狗超时先延长重试，最多 {MAX_EXTENSIONS} 次"
        f"（总计 {MAX_EXTENSIONS + 1} × timeoutMs）\n"
        "   2. 每次延长打 warn（event=subagent.activity_timeout_extended）\n"
        "   3. 预算用尽后才 abort，原错误文本不变\n"
        "   4. 全部内联，不引任何外部文件\n"
        "   node --check 通过\n"
        "   ⚠️ 需重启 zcode 进程才生效",
    )


def revert(bundle):
    if not os.path.exists(bundle):
        print(f"找不到 bundle: {bundle}", file=sys.stderr)
        return 1
    src = read_bundle(bundle)
    budgets = [int(m.group("n")) for m in PATCHED_RE.finditer(src)]
    if not budgets:
        print("未发现补丁痕迹，无需还原")
        return 0
    new = PATCHED_RE.sub(orig_form, src, count=1)
    return _commit(
        bundle, src, new,
        f"✅ 已还原：移除看门狗延长逻辑（原预算 {budgets[0]}）；node --check 通过",
    )


def main():
    ap = argparse.ArgumentParser(description="子代理看门狗：有界延长替代当场处决")
    ap.add_argument("--bundle", default=DEFAULT_BUNDLE)
    ap.add_argument("--check", action="store_true", help="只查状态，不写盘")
    ap.add_argument("--revert", action="store_true", help="还原")
    ap.add_argument("--dry-run", action="store_true", help="只校验锚点，不写盘")
    args = ap.parse_args()

    if args.check:
        return do_check(args.bundle)
    if args.revert:
        return revert(args.bundle)
    return apply_patch(args.bundle, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
