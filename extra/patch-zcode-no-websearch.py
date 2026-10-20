#!/usr/bin/env python3
"""禁用 provider-native WebSearch 工具 —— 直连 openai-compatible 引擎必崩的伪调用源

## 问题（2026-10-10 取证，sess_fe7c356b）

zai provider 直连 vLLM（openai-compatible）后，WebSearch 仍被声明进工具清单，
但它是 provider-native 工具，bundle 内 fFs() 只支持 anthropic 分支：

    function fFs(e,t){if(e.providerNative?.logicalName!=="WebSearch")return;
      ...switch(t.providerKind){case"anthropic":...;default:throw new w1(...)}}

openai-compatible 走 default 分支 throw。后果链：
  模型调 WebSearch → 请求层抛错/无结果 → 模型把 `<tool_call>WebSearch>` 吐成纯文本
  → zcode 不解析文本伪调用 → 永远无结果 → 模型反复重试（本会话连试 4 次）。

工具清单过滤点（全 bundle 唯一）：

    this.cachedTools.filter(t=>t.name!=="WebSearch"||Hwa.call(this,e))
    function Hwa(e){return e?e.properties.supportsNativeWebSearch:!0}

Hwa 在模型属性缺失时默认 true，而 modelConfigRules 路由（provider_config.json）
实测两轮均未生效（supportsNativeWebSearch:false 写了 providerId 仍 True），
故直接补丁：Hwa 恒 false → WebSearch 从工具清单剔除。
WebFetch（客户端执行）不受影响。

## 改动点（一处）

锚点（全 bundle 唯一）：
    function Hwa(e){return e?e.properties.supportsNativeWebSearch:!0}

补丁后：
    function Hwa(e){return!1/*patch:no-websearch*/}

用法：
    python3 patch-zcode-no-websearch.py --check
    python3 patch-zcode-no-websearch.py
    python3 patch-zcode-no-websearch.py --revert
    python3 patch-zcode-no-websearch.py --dry-run

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

# ★ 2026-10-20：不写死 minifier 名（Hwa → lFa），按函数体形状匹配。
ANCHOR_ORIG_RE = re.compile(
    r"function (?P<fn>[A-Za-z0-9_$]+)\(e\)\{return e\?e\.properties\.supportsNativeWebSearch:!0\}")
ANCHOR_PATCHED_RE = re.compile(
    r"function (?P<fn>[A-Za-z0-9_$]+)\(e\)\{return!1/\*patch:no-websearch\*/\}")


def _n(pat, src):
    return len(pat.findall(src))


def _patch_first(pat, src, repl_fn):
    m = pat.search(src)
    if m is None:
        return src
    return src[:m.start()] + repl_fn(m) + src[m.end():]

MARK = "/*patch:no-websearch*/"


def read_bundle(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write_bundle(path, src):
    tmp = path + ".tmp-nows"
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
    print(f"  no-websearch 标记 : {info['has_mark']}")
    print(f"  补丁后锚点     : {info['n_patched']} 处（应为 1）")
    print(f"  原始锚点       : {info['n_orig']} 处（应为 0）")
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
                       lambda m: f"function {m.group('fn')}(e){{return!1/*patch:no-websearch*/}}")
    write_bundle(bundle, new)
    ok, err = node_check(bundle)
    if not ok:
        print(f"❌ node --check 失败，正在回滚:\n{err}", file=sys.stderr)
        write_bundle(bundle, src)
        return 1

    print("✅ 已打补丁：")
    print("   Hwa() 恒 false → WebSearch 从模型工具清单剔除（provider-native 在 openai-compatible 下必 throw）")
    print("   WebFetch 等客户端工具不受影响")
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
    new = _patch_first(
        ANCHOR_PATCHED_RE, src,
        lambda m: f"function {m.group('fn')}(e){{return e?e.properties.supportsNativeWebSearch:!0}}")
    write_bundle(bundle, new)
    ok, err = node_check(bundle)
    if not ok:
        print(f"❌ 还原后 node --check 失败:\n{err}", file=sys.stderr)
        return 1
    print("✅ 已还原：Hwa() 恢复原判定；node --check 通过")
    return 0


def main():
    ap = argparse.ArgumentParser(description="禁用 provider-native WebSearch（openai-compatible 直连下必崩）")
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
