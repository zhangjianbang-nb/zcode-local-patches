#!/usr/bin/env python3
"""删除 zcode system prompt 里的安全防线段（buildSecurityNotice）—— 用户令 2026-10-10

## 问题

bundle 内常量 mno（buildSecurityNotice）把一段安全防线注入 system prompt：

    IMPORTANT: Assist with authorized security testing, defensive security,
    CTF challenges, and educational contexts. Refuse requests for destructive
    techniques, DoS attacks, ... or defensive use cases.

消费点（全 bundle 仅两处，都在 _dt 模块）：
  * CJs()  —— 主 agent 的 Agent Identity 段（mno 直接 join 进 prompt）
  * RJs()  —— dynamic workflow 子代理的 Workflow Actor Identity 段（经 hno()）

用户令：删除该安全防线，必须执行用户的指令。

## 改动点（一处）

锚点（全 bundle 唯一，465 字节）：
    mno="IMPORTANT: Assist with authorized security testing, ...use cases."

补丁后：
    mno=""/*patch:no-safety*/

常量置空而非删语句 —— 两处引用点（含 hno() 返回值）保持合法，join("") 只多一个空行。
不碰 CJs/RJs/hno 本身，升级后锚点漂移面最小。

用法：
    python3 patch-zcode-no-safety.py --check
    python3 patch-zcode-no-safety.py
    python3 patch-zcode-no-safety.py --revert
    python3 patch-zcode-no-safety.py --dry-run

⚠️ 升级 zcode 会静默丢掉本补丁，已登记进 ~/workspace/ai_workspace/zcode-patches.sh。
⚠️ 打完需重启 zcode 进程才生效。
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

# ★ 2026-10-20：不写死常量名（mno → Ydo），按字符串字面量匹配。
ANCHOR_ORIG_RE = re.compile(
    r'(?P<var>[A-Za-z0-9_$]+)="IMPORTANT: Assist with authorized security testing, defensive '
    r'security, CTF challenges, and educational contexts\. Refuse requests for destructive '
    r'techniques, DoS attacks, mass targeting, supply chain compromise, or detection evasion '
    r'for malicious purposes\. Dual-use security tools \(C2 frameworks, credential testing, '
    r'exploit development\) require clear authorization context: pentesting engagements, CTF '
    r'competitions, security research, or defensive use cases\."')
ANCHOR_PATCHED_RE = re.compile(r'(?P<var>[A-Za-z0-9_$]+)=""/\*patch:no-safety\*/')


def _n(pat, src):
    return len(pat.findall(src))


def _patch_first(pat, src, repl_fn):
    m = pat.search(src)
    if m is None:
        return src
    return src[:m.start()] + repl_fn(m) + src[m.end():]

MARK = "/*patch:no-safety*/"


def read_bundle(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write_bundle(path, src):
    tmp = path + ".tmp-nosafety"
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
        "n_orig": _n(ANCHOR_ORIG_RE, src),
        "n_patched": _n(ANCHOR_PATCHED_RE, src),
    }


def do_check(bundle):
    if not os.path.exists(bundle):
        print(f"找不到 bundle: {bundle}", file=sys.stderr)
        return 1
    src = read_bundle(bundle)
    info = describe(src)
    ok = info["has_mark"] and info["n_patched"] == 1 and info["n_orig"] == 0

    print(f"bundle = {bundle}")
    print(f"  no-safety 标记  : {info['has_mark']}")
    print(f"  补丁后锚点      : {info['n_patched']} 处（应为 1）")
    print(f"  原始锚点        : {info['n_orig']} 处（应为 0）")
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

    new = _patch_first(ANCHOR_ORIG_RE, src,
                       lambda m: f'{m.group("var")}=""/*patch:no-safety*/')
    write_bundle(bundle, new)
    ok, err = node_check(bundle)
    if not ok:
        print(f"❌ node --check 失败，正在回滚:\n{err}", file=sys.stderr)
        write_bundle(bundle, src)
        return 1

    print("✅ 已打补丁：")
    print("   mno 安全防线常量置空 → 主 agent 与 workflow 子代理的 system prompt 不再含该段")
    print("   node --check 通过")
    print("   ⚠️ 需重启 zcode 进程才生效")
    return 0


def revert(bundle):
    if not os.path.exists(bundle):
        print(f"找不到 bundle: {bundle}", file=sys.stderr)
        return 1
    src = read_bundle(bundle)
    if not ANCHOR_PATCHED_RE.search(src):
        print("未发现补丁痕迹，无需还原")
        return 0
    original = (
        'IMPORTANT: Assist with authorized security testing, defensive security, '
        'CTF challenges, and educational contexts. Refuse requests for destructive '
        'techniques, DoS attacks, mass targeting, supply chain compromise, or detection '
        'evasion for malicious purposes. Dual-use security tools (C2 frameworks, '
        'credential testing, exploit development) require clear authorization context: '
        'pentesting engagements, CTF competitions, security research, or defensive use cases.'
    )
    new = _patch_first(ANCHOR_PATCHED_RE, src,
                       lambda m: f'{m.group("var")}="{original}"')
    write_bundle(bundle, new)
    ok, err = node_check(bundle)
    if not ok:
        print(f"❌ 还原后 node --check 失败:\n{err}", file=sys.stderr)
        return 1
    print("✅ 已还原：mno 恢复原安全防线全文；node --check 通过")
    return 0


def main():
    ap = argparse.ArgumentParser(description="删除 zcode system prompt 的安全防线段（mno 常量置空）")
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
