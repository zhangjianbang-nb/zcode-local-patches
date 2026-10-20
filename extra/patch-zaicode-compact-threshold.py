#!/usr/bin/env python3
"""ZCode 桌面/dev 版（.zaicode-dist 那一支）的 autoCompact 阈值补丁。

背景（2026-10-04 事故）: 桌面/dev 版跑的是 `~/.local/bin/ZCode`
→ `~/.zaicode-dist/releases/<ver>/agent/zcode.cjs`，**不是** npm 的那份
`vendor/zcode.cjs`（那份由 patch-zcode-vno-buffer.py 管）。源码里两处入口
（prompt-command.ts / tui-prompt-handler.ts）注入了
`runtimeConfig.compact = { bufferTokens: ... }`，而这个字段是 **buffer 不是阈值**:

    threshold = max(0, (contextWindow − min(maxOutputTokens, 21000)) − bufferTokens)

2026-10-04 之前填的是 262_000（把「阈值 262k」当 buffer 填了），叠加该支配置
`~/.zaicode-home/.zcode/v2/provider_config.json` 里过期的 contextWindow=292000:

    292000 − 21000 − 262000 = 9000   → 开局任何会话都超 → 每步都压 → 熔断
    （同一补丁在 1M 窗口下反而是 765576 = 永不压缩）

本脚本把三处 build 产物统一成**换算后的 buffer**:

    buffer = (contextWindow − 21000) − DEFAULT_THRESHOLD

目标靶（存在才处理，缺失只告警不失败 —— 这些是可选产物）:
  1. ~/.zaicode-dist/releases/*/agent/zcode.cjs          注入点 `compact: { bufferTokens: X }` ×2
  2. ~/workspace/ai_workspace/ZCode/packages/desktop/bundled-agents/*/*/zcode.cjs
                                                         常量块第 5 个 `=13e3`（bufferTokens 兜底）
  3. ~/workspace/ai_workspace/ZCode/apps/zcode-cli/packages/cli/dist/zcode.cjs   同 1 的注入点

另做**一致性检查**（只报告不自动改）: 上面那支配置的 contextWindow 必须与
引擎实际窗口一致（TensorFold = 1048576）；窗口一变，同一个 buffer 的语义就变了。

用法:
  python3 patch-zaicode-compact-threshold.py               # 应用（幂等）
  python3 patch-zaicode-compact-threshold.py --check       # 只校验（exit 1 = 有缺失）
  python3 patch-zaicode-compact-threshold.py --revert      # 从 .bak 还原
  python3 patch-zaicode-compact-threshold.py --threshold 262k
  python3 patch-zaicode-compact-threshold.py --selftest
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
ZCODE_REPO = f"{HOME}/workspace/ai_workspace/ZCode"
ZAICODE_HOME = f"{HOME}/.zaicode-home"

# 真源: 桌面/dev 版自己的 provider_config（窗口从这里读，别硬编码）
CONFIG = f"{ZAICODE_HOME}/.zcode/v2/provider_config.json"

# 1M 窗口 + 阈值 262144(25%) → buffer = (1048576-21000)-262144 = 765432
DEFAULT_THRESHOLD = 262144
OUTPUT_RESERVE = 21000            # min(maxOutputTokens=32768, cZs=21000)
BAK_SUFFIX = ".bak-bufferpatch"

# 靶 1/3: 注入点（源码两处入口的 minified 形态）
INJECT_RE = re.compile(r"compact: \{ bufferTokens: (\d+(?:\.\d+)?(?:e[+-]?\d+)?) \}")

# 靶 2: 常量块 `...=2e5,X=32e3,X=21e3,X=2e4,X=13e3,X=100,X=3;`（第 5 个是 bufferTokens 兜底）
# 组: 1=首值 2=32e3名 3=21e3名 4=2e4名 5=buffer名 6=当前buffer值 7=100名 8=3名
CONST_BLOCK_RE = re.compile(
    r"=(2e5),(\w+)=32e3,(\w+)=21e3,(\w+)=2e4,(\w+)=(\d+(?:e\d+)?),(\w+)=100,(\w+)=3;"
)


def lit_to_num(lit: str) -> int:
    return int(float(lit))


def num_to_lit(n: int) -> str:
    return f"{n // 1000}e3" if n % 1000 == 0 and n else str(n)


def parse_threshold(raw: str) -> int:
    raw = raw.replace(",", "").strip()
    low = raw.lower()
    mult = 1
    if low.endswith("k"):
        mult, raw = 1000, raw[:-1]
    elif low.endswith("m"):
        mult, raw = 1000000, raw[:-1]
    return int(float(raw) * mult)


def context_window() -> int:
    if not os.path.exists(CONFIG):
        return 0
    try:
        d = json.load(open(CONFIG, encoding="utf-8"))
    except Exception:
        return 0
    for r in d.get("config", {}).get("modelConfigRules", {}).get("providerModelRules", []):
        w = r.get("config", {}).get("properties", {}).get("contextWindow")
        if w:
            return int(w)
    return 0


def targets():
    """返回 [(kind, path)]，kind ∈ {inject, const}。"""
    out = []
    for p in sorted(glob.glob(f"{HOME}/.zaicode-dist/releases/*/agent/zcode.cjs")):
        out.append(("inject", p))
    cur = f"{HOME}/.zaicode-dist/current/agent/zcode.cjs"
    if os.path.exists(cur) and os.path.realpath(cur) not in {os.path.realpath(x[1]) for x in out}:
        out.append(("inject", os.path.realpath(cur)))
    out.append(("inject", f"{ZCODE_REPO}/apps/zcode-cli/packages/cli/dist/zcode.cjs"))
    for p in sorted(glob.glob(f"{ZCODE_REPO}/packages/desktop/bundled-agents/*/*/zcode.cjs")):
        out.append(("const", p))
    return [(k, p) for k, p in out if os.path.exists(p)]


def read(p: str) -> str:
    return open(p, encoding="utf-8", errors="replace").read()


def inspect(kind: str, path: str, want_buffer: int, want_lit: str):
    """返回 (状态, 说明)。状态 ∈ {ok, fixable, missing, broken}"""
    s = read(path)
    if kind == "inject":
        hits = INJECT_RE.findall(s)
        if not hits:
            return "missing", "找不到注入点 compact: { bufferTokens: N }（上游改了结构? 需人工比对）"
        vals = {lit_to_num(h) for h in hits}
        cur = num_to_lit(next(iter(vals))) if len(vals) == 1 else "mixed"
        if vals == {want_buffer}:
            return "ok", f"{len(hits)} 处 = {cur}"
        return "fixable", f"{len(hits)} 处 当前 {cur} → 目标 {want_lit}"
    m = CONST_BLOCK_RE.search(s)
    if not m:
        return "missing", "找不到常量块（=2e5,…,=13e3,=100,=3;）（上游改了常量名? 需人工比对）"
    cur = lit_to_num(m.group(6))
    if cur == want_buffer:
        return "ok", f"bufferTokens 常量({m.group(5)}) = {num_to_lit(cur)}"
    return "fixable", f"常量 {m.group(5)} = {num_to_lit(cur)} → {want_lit}"


def node_ok(p: str) -> bool:
    return subprocess.run(["node", "--check", p], capture_output=True, text=True).returncode == 0


def apply_patch(kind: str, path: str, want_lit: str) -> bool:
    # 护栏: 文件可能正被「重新打包」写到一半 —— 不可解析就别碰，否则会把半截产物写坏
    if not node_ok(path):
        for _ in range(20):
            time.sleep(1)
            if node_ok(path):
                break
        else:
            print(f"      ❌ {path} 20s 内仍不可解析（可能正在重建），本次跳过，稍后重跑")
            return False
    s = read(path)
    if kind == "inject":
        new, n = INJECT_RE.subn(f"compact: {{ bufferTokens: {want_lit} }}", s)
    else:
        new, n = CONST_BLOCK_RE.subn(
            lambda m: (f"={m.group(1)},{m.group(2)}=32e3,{m.group(3)}=21e3,{m.group(4)}=2e4,"
                       f"{m.group(5)}={want_lit},{m.group(7)}=100,{m.group(8)}=3;"),
            s, count=1)
    if n == 0 or new == s:
        print(f"      skip（无可改或已是目标值）")
        return n > 0
    if not os.path.exists(path + BAK_SUFFIX):
        shutil.copy(path, path + BAK_SUFFIX)
    open(path, "w", encoding="utf-8").write(new)
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.copy(path + BAK_SUFFIX, path)          # 写坏立刻回滚
        print(f"      ❌ node --check 失败，已回滚: {r.stderr.strip()[:160]}")
        return False
    print(f"      🔧 已写 {path} （{n} 处, node --check OK, 备份 {os.path.basename(path)}{BAK_SUFFIX}）")
    return True


def selftest():
    bad = 0
    for raw, want in {"262k": 262000, "262,000": 262000, "0.262M": 262000,
                      "765432": 765432, "262": 262}.items():
        got = parse_threshold(raw)
        ok = got == want
        bad += not ok
        print(f"  {'ok ' if ok else 'FAIL'} parse_threshold({raw!r}) = {got} (want {want})")
    for n in (13000, 262000, 765432):
        ok = lit_to_num(num_to_lit(n)) == n
        bad += not ok
        print(f"  {'ok ' if ok else 'FAIL'} lit 往返 {n} -> {num_to_lit(n)}")
    # 常量块替换: 用真实形态做一次干跑（识别 + 改写 + 回读）
    sample = 'var a,b,c,d,e,f,g=X(()=>{"use strict";z();a=2e5,b=32e3,c=21e3,d=2e4,e=13e3,f=100,g=3;'
    m = CONST_BLOCK_RE.search(sample)
    ok = bool(m) and m.group(5) == "e" and lit_to_num(m.group(6)) == 13000
    bad += not ok
    print(f"  {'ok ' if ok else 'FAIL'} 常量块锚点识别: {m.groups() if m else None}")
    if m:
        out, n = CONST_BLOCK_RE.subn(
            lambda x: (f"={x.group(1)},{x.group(2)}=32e3,{x.group(3)}=21e3,{x.group(4)}=2e4,"
                       f"{x.group(5)}=765432,{x.group(7)}=100,{x.group(8)}=3;"), sample, count=1)
        back = CONST_BLOCK_RE.search(out)
        ok2 = n == 1 and back and back.group(5) == "e" and lit_to_num(back.group(6)) == 765432
        bad += not ok2
        print(f"  {'ok ' if ok2 else 'FAIL'} 常量块改写回读 = 765432  片段: ...{out[out.find(',e=') - 20:out.find(',e=') + 20]}...")
    print("selftest:", "ALL PASS" if bad == 0 else f"{bad} FAILED")
    sys.exit(1 if bad else 0)


def main():
    if "--selftest" in sys.argv:
        selftest()

    check_only = "--check" in sys.argv
    revert = "--revert" in sys.argv

    if "--threshold" in sys.argv:
        try:
            thr = parse_threshold(sys.argv[sys.argv.index("--threshold") + 1])
        except (IndexError, ValueError):
            print("用法: --threshold <整数或带 k/m, 例 262144 或 262k>")
            sys.exit(2)
    else:
        thr = DEFAULT_THRESHOLD

    W = context_window()
    print(f"配置真源 {CONFIG}")
    print(f"  contextWindow = {W or '❌ 读不到'}   (引擎 TensorFold 实际 = 1048576)")
    if not W:
        print("❌ 读不到 contextWindow，无法反推 buffer")
        sys.exit(2)
    effective = W - OUTPUT_RESERVE
    if thr <= 0 or thr >= effective:
        print(f"❌ 阈值必须在 (0, {effective}) 内")
        sys.exit(2)
    want_buffer = effective - thr
    want_lit = num_to_lit(want_buffer)
    print(f"  目标阈值 = {thr}  →  目标 buffer = {want_lit}")
    if W != 1048576:
        print(f"  ⚠️ 窗口 {W} 与引擎 1048576 不一致 —— 同一个 buffer 的语义会跟着变，请先对齐")

    if revert:
        n = 0
        for _, p in targets():
            if os.path.exists(p + BAK_SUFFIX):
                shutil.copy(p + BAK_SUFFIX, p)
                print(f"  已还原 {p}")
                n += 1
        print(f"还原 {n} 个文件" if n else "没有 .bak 可还原")
        return

    bad = 0
    for kind, p in targets():
        st, msg = inspect(kind, p, want_buffer, want_lit)
        flag = {"ok": "✅", "fixable": "❌", "missing": "⚠️", "broken": "❌"}[st]
        print(f"{flag} {p}\n      {msg}")
        if st == "ok":
            continue
        if st == "missing":
            bad += 1
            continue
        if check_only:
            bad += 1
            continue
        if not apply_patch(kind, p, want_lit):
            bad += 1

    print()
    if bad:
        print(f"{'❌ 有 ' + str(bad) + ' 处未就位' if not check_only else '❌ 校验不通过: ' + str(bad) + ' 处未就位'}"
              "（改完需重启 ZCode 会话才生效）")
        sys.exit(1)
    print("✅ 全部就位（若刚改过，需重启 ZCode 会话才生效）")


if __name__ == "__main__":
    main()
