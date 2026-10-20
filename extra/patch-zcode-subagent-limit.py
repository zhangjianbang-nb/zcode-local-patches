#!/usr/bin/env python3
"""子代理并发硬闸门 —— 同屏最多 2 个（2026-10-15 用户令修订；原为 4）

## 背景

`~/.zcode/AGENTS.md` 的「子代理并发铁律」：同屏最多 2 个子代理同时跑（2026-10-15 由 4 收紧到 2：
DS4.1 引擎池 262k 行时 3 条大流就互踩到 0% 命中；池子已扩到 1M，实测「主会话 + 2 subagent」稳定 92–96%，
「主会话 + 4 subagent」超订掉到 48% 且主力会话受连累）。
但**光靠纪律不够**，而且技术上原本**没有任何闸门**：

  * `subagents` 配置只认 `enabled / inactivityTimeoutMs / autoBackgroundMs /
    outputRootDir / profiles / builtInModelSelectionOverrides` —— **无并发开关**；
  * `toolConcurrency.maxConcurrency`（`ZCODE_MAX_TOOL_CONCURRENCY`）作用在
    `ToolScheduler.groupByParallel`，只管「单轮内并行工具调用数」。Agent 工具被标了
    `concurrentSafe:!0` 且**后台子代理的 handler 立刻返回 `async_launched`** ——
    工具批次一结束槽位就释放，所以 8 个子代理照样全都跑起来。它管不到后台子代理。

事故背景：2026-10-04 一次派发 8 个子代理（写答案 0007-0262），后端单请求常态
300~500s，8 路并发下三个请求卡到 600s 看门狗上限被当场处决（见
patch-zcode-subagent-watchdog.py 的取证）。**并发数直接乘在后端排队时延上。**

## 本补丁

在**唯一入口** `launch()`（Agent 工具 handler 只调这一处：前台 `run` 与后台 `start`
都从这里分流）开头插一个闸门：排队等空位，直到在跑的子代理 < 2 才放行。

计数用的是**运行时任务注册表** `t.all()` 里 `type==="local_agent" && status==="running"`
的条数，不是 `n`（AbortController 那个 Map）。

⚠️ 这个选择是**时序决定**的，不是偏好：`run`/`start` 里

    t.register(vwn({...status:"running"}));      // ← 同步，且在 await 之前
    try{ await w4e(b,_,"running") }catch(z){...}
    let T=_wn(n,b.agentId,f?.signal)             // ← 写 n，在 await 之后！

`n` 要等 `await` 回来才 `.set()`，而闸门检查与 `run/start` 的同步前奏在**同一个
同步块**里 —— 所以用 `t.all()` 计数时"检查→放行→注册"不可被打断，天然无竞态；
用 `n.size` 则会出现两个 launch 同时通过、实际跑 5 个的漏判。

**防死锁**：排队超过 30 分钟仍无空位就**放行**并打 `console.error`（宁可超编也不
把派发卡死）。正常路径永远不会走到这里；只有注册表里真有记录卡在 `running`
（进程异常退出没更新状态）时才会兜底。

**全内联**：只用闭包里已有的 `t`，无 require / 无外部文件。

## 改动点（一处）

锚点（全 bundle 唯一）：
    async launch(u,f){let{profile:g,request:_}=gwn(s,u),b=$ya(_);

## 生效范围

计数是**注册表级（进程级）**的：同进程里多个会话共享这个上限。单会话工作时等价于
"本会话最多 2 个"；如果你确实想按会话分别计数，需要改用记录里的 `parentSessionId`
过滤（本补丁没做，因为你的铁律目标是"别把后端压垮"，进程级更严格更安全）。

## 用法

    python3 patch-zcode-subagent-limit.py --check
    python3 patch-zcode-subagent-limit.py
    python3 patch-zcode-subagent-limit.py --revert
    python3 patch-zcode-subagent-limit.py --dry-run

⚠️ 升级 zcode 会静默丢掉本补丁，已登记进 ~/workspace/ai_workspace/zcode-patches.sh。
⚠️ 打补丁后需**重启 zcode 进程**才生效。
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

# 锚点随 zcode 升级而变（2026-10-15 重取，用于 zcode-app-cli 当前 bundle）：
LAUNCH_HEAD = "async launch(f,g){"
BODY = "let{profile:_,request:b}=TCn(a(),f),w=QDa(b);"
ANCHOR_ORIG = LAUNCH_HEAD + BODY

MAX_CONCURRENT = 2
WAIT_TIMEOUT_MS = 30 * 60 * 1000

# 闸门 = 前缀 + 并发上限 + 后缀，数字用正则识别 → 改上限不必改锚点
GATE_PREFIX = (
    LAUNCH_HEAD + "let __zcT0=Date.now(),__zcLogged=!1;for(;;){"
    "let __zcN=0,__zcAll=t.all();"
    "for(let __zcK in __zcAll){let __zcE=__zcAll[__zcK];"
    "__zcE&&__zcE.type===\"local_agent\"&&__zcE.status===\"running\"&&__zcN++}"
    "if(__zcN<"
)
GATE_SUFFIX = (
    ")break;"
    "__zcLogged||(__zcLogged=!0,console.error(\"[zcode-subagent-limit] 并发已达上限 "
)

MARK = "__zcLogged"
# 只匹配闸门本身（用 LAUNCH_HEAD 起、在 setTimeout 那行收），**不含函数体** ——
# 否则 revert / 改上限时会把 body 一起吃掉、或拼出重复的 `async launch(u,f){`。
PATCHED_RE = re.compile(
    re.escape(LAUNCH_HEAD)
    + r"let __zcT0=Date\.now\(\),__zcLogged=!1;for\(;;\)\{"
    r".*?await new Promise\(__zcR=>setTimeout\(__zcR,500\)\)\}",
    re.S,
)


def gate(n):
    """闸门全文。上限 n 出现在两处：判定与报错文案。"""
    tail = (
        GATE_SUFFIX
        + str(n)
        + "，本次 launch 排队等待空位…\"));"
        + "if(Date.now()-__zcT0>"
        + str(WAIT_TIMEOUT_MS)
        + "){console.error(\"[zcode-subagent-limit] 排队超过 30 分钟仍无空位"
        "（疑似有 agent 记录卡在 running），放行以免死锁。当前在跑=\"+__zcN);break}"
        "await new Promise(__zcR=>setTimeout(__zcR,500))}"
    )
    return GATE_PREFIX + str(n) + tail


def read_bundle(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write_bundle(path, src):
    tmp = path + ".tmp-sublimit"
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
    hits = PATCHED_RE.findall(src)
    limits = []
    for h in hits:
        m = re.search(r"if\(__zcN<(\d+)\)break;", h)
        limits.append(int(m.group(1)) if m else None)
    return {
        "has_mark": MARK in src,
        "n_gate": len(hits),
        "limits": limits,
        "n_orig": src.count(ANCHOR_ORIG),
    }


def _commit(bundle, src, new, action):
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
    ok = info["n_gate"] == 1 and info["limits"] == [MAX_CONCURRENT] and info["n_orig"] == 0

    print(f"bundle = {bundle}")
    print(f"  闸门已注入     : {info['has_mark']}")
    print(f"  闸门处数       : {info['n_gate']}（应为 1）")
    print(f"  并发上限       : {info['limits'] or '无'}（应为 [{MAX_CONCURRENT}]）")
    print(f"  原始 launch 锚 : {info['n_orig']} 处（应为 0）")
    print("✅ 补丁就位" if ok else "❌ 补丁缺失/上限不符 —— 跑不带参数的本脚本重放")
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

    if info["n_gate"] == 1 and info["limits"] == [MAX_CONCURRENT]:
        print(f"✅ 已经是补丁状态（上限 {MAX_CONCURRENT}），无需重放")
        return 0

    if info["n_gate"] == 1 and info["n_orig"] == 0:
        old = info["limits"][0]
        if dry_run:
            print(f"--dry-run：闸门上限 {old} → {MAX_CONCURRENT}，未写盘")
            return 0
        new = PATCHED_RE.sub(lambda m: gate(MAX_CONCURRENT), src, count=1)
        return _commit(
            bundle, src, new,
            f"✅ 已把子代理并发上限 {old} → {MAX_CONCURRENT}；node --check 通过",
        )

    if info["n_orig"] != 1 or info["n_gate"]:
        print(
            f"❌ 锚点异常：原始 launch 锚 {info['n_orig']} 处（应为 1），已有闸门 {info['n_gate']} 处。\n"
            f"   bundle 可能已升级或被打过其它补丁 —— 请人工比对，不要硬打。",
            file=sys.stderr,
        )
        return 1
    if dry_run:
        print("--dry-run：锚点正常，未写盘")
        return 0

    new = src.replace(ANCHOR_ORIG, gate(MAX_CONCURRENT) + BODY, 1)
    return _commit(
        bundle, src, new,
        "✅ 已打补丁：\n"
        f"   1. launch() 入口排队闸门，在跑子代理 < {MAX_CONCURRENT} 才放行\n"
        "   2. 计数依据 = runtimeTaskRegistry 里 type=local_agent && status=running\n"
        f"   3. 排队超 30 分钟无空位则放行并报错（防死锁兜底）\n"
        "   4. 全部内联，不引任何外部文件\n"
        "   node --check 通过\n"
        "   ⚠️ 需重启 zcode 进程才生效",
    )


def revert(bundle):
    if not os.path.exists(bundle):
        print(f"找不到 bundle: {bundle}", file=sys.stderr)
        return 1
    src = read_bundle(bundle)
    info = describe(src)
    if info["n_gate"] != 1:
        print("未发现补丁痕迹，无需还原")
        return 0
    new = PATCHED_RE.sub(lambda m: LAUNCH_HEAD, src, count=1)
    return _commit(
        bundle, src, new,
        f"✅ 已还原：移除子代理并发闸门（原上限 {info['limits'][0]}）；node --check 通过",
    )


def main():
    ap = argparse.ArgumentParser(description="子代理并发硬闸门（默认上限 4）")
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
