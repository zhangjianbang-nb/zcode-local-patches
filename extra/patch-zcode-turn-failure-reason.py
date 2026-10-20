#!/usr/bin/env python3
"""保住子代理的真实死因 —— 别把 ToolTimeout 伪装成「用户取消」

## 问题（2026-10-04 取证）

三个后台子代理的 metadata.json 落的是 `status:"failed"` + `error:"Turn was cancelled."`，
看起来像用户按了 Esc。真因是看门狗 `ToolTimeout: Subagent was inactive for 600000ms`
（见 patch-zcode-subagent-watchdog.py 的取证）。

**唯一转换点**是 `JC()`（bundle 内注释名 `createTurnCancelledError`）：

    function JC(e){return Sf(e)&&e.type===fr.TurnCancelled?e:Gr(fr.TurnCancelled,"Turn was cancelled.",{cause:e...,recoverable:!0})}

它的语义是"把任何东西归一成 TurnCancelled"，所以看门狗注入的
`Gr(fr.ToolTimeout,"Subagent was inactive for Xms")` 只因 type 不是 TurnCancelled
就被无条件替换成 generic 文案。后果有两层：

  1. `/tasks` 里显示的死因无法定位（"Turn was cancelled." 既不说明是超时，也不说明是超时多久）；
  2. 下游 `Xqo(e)=e.type===fr.TurnCancelled` 把它判定成"取消"，于是
     `onTurnRejected` 走"用户主动取消"分支，不再上报失败。

`JC` 是 `jd()`（abort 后 `throw JC(signal.reason)`）与 `tie()`（`C0(e,t)?JC(e)`）
两条路径的**唯一汇聚点**，所以改这一处即可。

## 本补丁（刻意做窄）

只让 **`fr.ToolTimeout`** 一个类型透传，其余一律维持原行为：

    ...Sf(e)&&e.type===fr.ToolTimeout?e: <原文>

为什么不全放开（调研曾建议放开所有非取消型 core error）：`JC` 有 10 处调用点、
契约是"必返回 TurnCancelled"，放宽会同时改动模型错误 / 未知错误等路径的语义，
收益不明而回归面变大。ToolTimeout 恰恰是"本该报失败、绝不该被当成取消"的那一类，
而全库唯一的 `abort(Gr(...))` 注入点就是子代理看门狗（已枚举全部 abort 实参核实），
所以窄改法的影响面 == 看门狗本身。

真正的用户取消（Esc / 外部 abort）reason 是普通 Error / 字符串 / DOMException
`AbortError`，`Sf()` 为 false，全部照旧落进 else 分支 → 仍是 "Turn was cancelled."，
**取消语义不变**。

## 改动点（一处）

锚点（全 bundle 唯一）：
    function JC(e){return Sf(e)&&e.type===fr.TurnCancelled?e:Gr(fr.TurnCancelled,"Turn was cancelled.",{cause:e instanceof Error?e:void 0,recoverable:!0})}

用法：
    python3 patch-zcode-turn-failure-reason.py --check
    python3 patch-zcode-turn-failure-reason.py
    python3 patch-zcode-turn-failure-reason.py --revert
    python3 patch-zcode-turn-failure-reason.py --dry-run

⚠️ 与 patch-zcode-subagent-watchdog.py 是**配套**的：那个让慢请求别被误杀，
   这个让真被杀时死因可查。两个都要打、都要重启 zcode 才生效。
⚠️ 升级 zcode 会静默丢掉本补丁，已登记进 ~/workspace/ai_workspace/zcode-patches.sh。
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

# ★ 2026-10-20：minifier 名会变（JC→bE、Sf→Tf、fr→nr、Gr→Ur），按形状匹配。
ANCHOR_ORIG_RE = re.compile(
    r"function (?P<fn>[A-Za-z0-9_$]+)\(e\)\{return (?P<iserr>[A-Za-z0-9_$]+)\(e\)&&"
    r"e\.type===(?P<enum>[A-Za-z0-9_$]+)\.TurnCancelled\?e:"
    r"(?P<errf>[A-Za-z0-9_$]+)\((?P=enum)\.TurnCancelled,\"Turn was cancelled\.\","
    r"\{cause:e instanceof Error\?e:void 0,recoverable:!0\}\)\}")
ANCHOR_PATCHED_RE = re.compile(
    r"function (?P<fn>[A-Za-z0-9_$]+)\(e\)\{return (?P<iserr>[A-Za-z0-9_$]+)\(e\)&&"
    r"e\.type===(?P<enum>[A-Za-z0-9_$]+)\.ToolTimeout\?e:"
    r"(?P=iserr)\(e\)&&e\.type===(?P=enum)\.TurnCancelled\?e:"
    r"(?P<errf>[A-Za-z0-9_$]+)\((?P=enum)\.TurnCancelled,\"Turn was cancelled\.\","
    r"\{cause:e instanceof Error\?e:void 0,recoverable:!0\}\)\}")


def _orig_form(m):
    return (
        f"function {m.group('fn')}(e){{return {m.group('iserr')}(e)&&"
        f"e.type==={m.group('enum')}.TurnCancelled?e:"
        f"{m.group('errf')}({m.group('enum')}.TurnCancelled,\"Turn was cancelled.\","
        "{cause:e instanceof Error?e:void 0,recoverable:!0})}"
    )


def _patched_form(m):
    return (
        f"function {m.group('fn')}(e){{return {m.group('iserr')}(e)&&"
        f"e.type==={m.group('enum')}.ToolTimeout?e:"
        f"{m.group('iserr')}(e)&&e.type==={m.group('enum')}.TurnCancelled?e:"
        f"{m.group('errf')}({m.group('enum')}.TurnCancelled,\"Turn was cancelled.\","
        "{cause:e instanceof Error?e:void 0,recoverable:!0})}"
    )

# ★ 2026-10-20：不写死 minifier 名；判据用正则计数，标记只用于快速定位。
MARK = ".ToolTimeout?e:"


def read_bundle(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write_bundle(path, src):
    tmp = path + ".tmp-reason"
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
    return {
        "has_mark": MARK in src,
        "has_cancelled_text": src.count('"Turn was cancelled."'),
        "n_orig": len(ANCHOR_ORIG_RE.findall(src)),
        "n_patched": len(ANCHOR_PATCHED_RE.findall(src)),
    }


def do_check(bundle):
    if not os.path.exists(bundle):
        print(f"找不到 bundle: {bundle}", file=sys.stderr)
        return 1
    src = read_bundle(bundle)
    info = describe(src)
    ok = info["has_mark"] and info["n_patched"] == 1 and info["n_orig"] == 0

    print(f"bundle = {bundle}")
    print(f"  ToolTimeout 透传已注入 : {info['has_mark']}")
    print(f"  补丁后锚点             : {info['n_patched']} 处（应为 1）")
    print(f"  原始锚点               : {info['n_orig']} 处（应为 0）")
    print(f"  'Turn was cancelled.'  : {info['has_cancelled_text']} 处（应 >=1，证明取消兜底还在）")
    print("✅ 补丁就位" if ok else "❌ 补丁缺失/不完整 —— 跑不带参数的本脚本重放")
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

    if info["has_mark"] and info["n_patched"] == 1:
        print("✅ 已经是补丁状态，无需重放（幂等）")
        return 0
    if info["n_orig"] != 1 or info["n_patched"] != 0:
        print(
            f"❌ 锚点异常：原始 {info['n_orig']} 处（应为 1），已补丁 {info['n_patched']} 处。\n"
            f"   bundle 可能已升级或被打过其它补丁 —— 请人工比对，不要硬打。",
            file=sys.stderr,
        )
        return 1
    if dry_run:
        print("--dry-run：锚点正常，未写盘")
        return 0

    _m = ANCHOR_ORIG_RE.search(src)
    new = src[:_m.start()] + _patched_form(_m) + src[_m.end():]
    write_bundle(bundle, new)
    ok, err = node_check(bundle)
    if not ok:
        print(f"❌ node --check 失败，正在回滚:\n{err}", file=sys.stderr)
        write_bundle(bundle, src)
        return 1

    print("✅ 已打补丁：")
    print("   JC() 对 fr.ToolTimeout 原样透传（不再改写成 'Turn was cancelled.'）")
    print("   其余类型（含真取消的 AbortError/普通 Error）行为完全不变")
    print("   node --check 通过")
    print("   ⚠️ 需重启 zcode 进程才生效")
    return 0


def revert(bundle):
    if not os.path.exists(bundle):
        print(f"找不到 bundle: {bundle}", file=sys.stderr)
        return 1
    src = read_bundle(bundle)
    _m = ANCHOR_PATCHED_RE.search(src)
    if _m is None:
        print("未发现补丁痕迹，无需还原")
        return 0
    new = src[:_m.start()] + _orig_form(_m) + src[_m.end():]
    write_bundle(bundle, new)
    ok, err = node_check(bundle)
    if not ok:
        print(f"❌ 还原后 node --check 失败:\n{err}", file=sys.stderr)
        return 1
    print("✅ 已还原：JC() 恢复无条件归一；node --check 通过")
    return 0


def main():
    ap = argparse.ArgumentParser(description="JC(): 保住子代理 ToolTimeout 的真实死因")
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
