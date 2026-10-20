#!/usr/bin/env python3
"""(C) zcode 截断续写补丁 —— 掐掉「输出上限 → 接着写 → 再爆上限」这个正反馈回路。

## 问题（2026-10-01 取证，形态③）

会话 sess_65122374 在 22:23–22:32 空转 ~8 分钟，**循环期间一次工具调用都没真正执行**，
所以 watchdog R1/R2、bundle 循环守卫 L1–L4、原生 modelAnomalyGuard 全部静默（都只看
`type:'tool'` 的 part）。取证链（`~/.zcode/cli/debug/model-io-<sid>.jsonl`）：

  1. 模型把「修一个 md 里的笔误」写成自修改脚本，几十行互相抵消的 `src = src.replace(A,B)`
  2. 撑爆输出上限 → finishReason="length"、outputTokens=32768（顶格）
     → `<tool_call>Bash<arg_key>command</arg_key><arg_value>…` 在 `src = src.rep` 处被硬切
     → 标签未闭合、解析不出 toolCall → toolCalls=[] → **那个调用从未执行**
  3. 真凶在这里 —— bundle 里的续写分类器：

         function aEo(e){return e.toolCallCount>0||!kxn(e.finishReason,e.rawFinishReason)
                         ?"none":e.continuationCount<Wxa?"continue":"exhausted"}
         kxn = finishReason === "length" | rawFinishReason ∈ {max_tokens, max_output_tokens,
               model_context_window_exceeded}      Wxa = 3（最多续 3 次）

     因为 `toolCallCount===0`，它判定「只是话没说完」，于是把 qxa 当 user 消息注入。
  4. **要命的文案**：qxa = "Output token limit hit. Resume directly — … **Pick up mid-thought
     if that is where the cut happened.** Break remaining work into smaller pieces."
     「pick up mid-thought」= 让模型接着写刚才那段 `src.replace(...)` → 再爆 32768 → 再注入 → 循环。
     实测两轮 213s + 253s，第 3 轮才偶然发出一个短调用（`grep -n 'opt.items|opt opt'`）爬出来。

## 修复

分类器不动（`continue` 的机制本身合理，改它要动 turn 状态机，风险大），**只换文案**：
把「接着写中间那句」改成「切断点之后作废、别重发、现在就发一个短的完整响应」。
截断发生在工具调用内部时，这直接打断了「续写同一个巨型参数」的正反馈。

## 用法

  python3 patch-zcode-truncation-resume.py --check      # 查状态（exit 0 = 已就位）
  python3 patch-zcode-truncation-resume.py              # 打补丁
  python3 patch-zcode-truncation-resume.py --dry-run    # 只打印将要做的替换
  python3 patch-zcode-truncation-resume.py --revert     # 还原
  python3 patch-zcode-truncation-resume.py --selftest   # 从 bundle 抠出字符串做真实功能验证

⚠️ 升级 zcode 会**静默丢掉**本补丁，升级后必跑 --check（zcode-patches.sh 已纳入清单）。
⚠️ 打补丁后需**重启 zcode 进程**才生效。
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile

BUNDLE_DEFAULT = os.path.expanduser(
    "~/.npm-global/lib/node_modules/zcode-app-cli/vendor/zcode.cjs")

# 原文案。注意：bundle 里 em dash 是 JS 源码层面的 6 字符转义 —
# （实测 raw bytes = b'...directly \\u2014 no apology...'），不是 UTF-8 的 em dash 字符。
# 用 "\\u2014" 显式拼出来，避免源码里写成真字符而永远匹配不上。
OLD_TEXT = (
    "Output token limit hit. Resume directly "
    + "\\u2014"
    + " no apology, no recap of what you were doing. "
    "Pick up mid-thought if that is where the cut happened. "
    "Break remaining work into smaller pieces."
)

# 新文案：ASCII only、无引号无反斜杠，避免二次转义出错。
NEW_TEXT = (
    "Output token limit hit: your previous response was cut off mid-stream. "
    "Anything after the cut is incomplete -- a half-written tool call never "
    "ran and will not run. Do NOT resume mid-thought or repeat the cut-off "
    "text. Emit ONE short, complete response now: if you need a tool, send a "
    "single complete tool call with a small input, and split the remaining "
    "work into several small steps."
)

# 幂等标记：只有新文案里有的短语
MARK = "a half-written tool call never ran and will not run"


def read_bundle(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def width(path):
    """按字节数读写，保证非 ASCII 片段逐字节还原。"""
    with open(path, "rb") as f:
        return f.read()


def node_check(path):
    if not shutil.which("node"):
        return False, "找不到 node，无法做语法校验"
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True,
                           text=True, timeout=180)
        if r.returncode == 0:
            return True, "node --check 通过"
        return False, (r.stderr or r.stdout).strip()[:800]
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"跑不了 node --check: {e}"


def status(text):
    """返回 'new' / 'old' / 'unknown'。"""
    if MARK in text:
        return "new"
    if OLD_TEXT in text:
        return "old"
    return "unknown"


def write_atomic(path, blob):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".zcode-trunc-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup(bundle):
    bak = f"{bundle}.bak-truncresume-{datetime.date.today():%Y%m%d}"
    if not os.path.exists(bak):
        shutil.copy2(bundle, bak)
        print(f"  已备份: {bak}")
    else:
        print(f"  备份已存在: {bak}")
    return bak


def do_apply(bundle, dry_run=False):
    text = read_bundle(bundle)
    st = status(text)
    if st == "new":
        print("✅ 已是目标状态（补丁已就位）。")
        return 0
    if st == "unknown":
        print("❌ 既找不到原文案也找不到新文案 —— bundle 里 qxa 的措辞已经变了。", file=sys.stderr)
        print("   请先人工比对源码里的 'Output token limit hit' 那段。", file=sys.stderr)
        return 2
    n = text.count(OLD_TEXT)
    if n != 1:
        print(f"❌ 原文案在 bundle 里命中 {n} 次（必须恰好 1 次），拒绝改动。", file=sys.stderr)
        return 2
    if dry_run:
        print("--- 将被替换的原文 ---")
        print(OLD_TEXT)
        print("--- 替换为 ---")
        print(NEW_TEXT)
        print("(dry-run，未写盘)")
        return 0

    backup(bundle)
    new_blob = width(bundle).replace(OLD_TEXT.encode(), NEW_TEXT.encode())
    assert new_blob != width(bundle), "替换没有生效"
    write_atomic(bundle, new_blob)
    ok, msg = node_check(bundle)
    if not ok:
        print(f"❌ node --check 失败，自动回滚：{msg}", file=sys.stderr)
        do_revert(bundle, _quiet=True)
        return 1
    print("✅ 已打补丁；" + msg)
    print("⚠️ 需重启 zcode 进程才生效。")
    return 0


def do_revert(bundle, _quiet=False):
    text = read_bundle(bundle)
    if OLD_TEXT in text:
        if not _quiet:
            print("已是原始状态，无需还原。")
        return 0
    if MARK not in text:
        print("❌ bundle 里找不到本补丁的痕迹，无法还原。", file=sys.stderr)
        return 2
    if not _quiet:
        print("还原中…")
    blob = width(bundle).replace(NEW_TEXT.encode(), OLD_TEXT.encode())
    write_atomic(bundle, blob)
    ok, msg = node_check(bundle)
    if not ok:
        print(f"❌ 还原后 node --check 失败（严重，请用备份恢复）：{msg}", file=sys.stderr)
        return 1
    if not _quiet:
        print("✅ 已还原；" + msg)
    return 0


def do_check(bundle):
    try:
        text = read_bundle(bundle)
    except OSError as e:
        print(f"❌ 读不到 bundle: {e}", file=sys.stderr)
        return 1
    st = status(text)
    if st == "new":
        print("✅ 截断续写补丁已就位。")
        return 0
    if st == "old":
        print("❌ 补丁缺失（bundle 里还是「Pick up mid-thought」原文案）。")
        return 1
    print("⚠️ 状态未知：qxa 文案既非原文也非本补丁的新文，可能上游改过措辞。")
    return 1


# ------------------------------------------------------------------ selftest

def selftest():
    """从 patch 后的真 bundle 抠出补丁字符串，验证语义（不依赖内存里的常量）。"""
    fails = []
    bundle = os.environ.get("ZCODE_CJS", BUNDLE_DEFAULT)
    text = read_bundle(bundle)
    if status(text) != "new":
        print("❌ bundle 未打补丁，先跑一次本脚本再 --selftest。", file=sys.stderr)
        return 1

    # 1) 抠出实际写进 bundle 的那个字符串字面量（qxa= 后面的一整段）
    i = text.find('qxa="')
    if i < 0:
        print("❌ 找不到 qxa= 赋值。", file=sys.stderr)
        return 1
    j = text.find('"', i + 5)
    actual = text[i + 5:j]
    print(f"bundle 里的续写提示（{len(actual)} 字符）:\n  {actual}\n")

    # 2) 必须消除「接着写」的指令，否则循环不破
    for bad in ("Pick up mid-thought", "Resume directly", "no apology"):
        if bad in actual:
            fails.append(f"残留旧指令 {bad!r} —— 循环没被真正打断")
    # 3) 必须给出可执行的替代动作
    for need, why in (
            ("Do NOT resume mid-thought", "没禁止「接着写中间那句」"),
            ("never ran", "没告诉模型被截断的工具调用从未执行"),
            ("ONE short, complete", "没要求发一个短的完整响应"),
            ("small input", "没要求把工具入参收小"),
    ):
        if need not in actual:
            fails.append(f"缺少 {need!r}（{why}）")
    # 4) 字符串必须仍是合法 JS（无裸引号/反斜杠），否则我们打断了 bundle 语法
    if '"' in actual or "\\" in actual:
        fails.append("字符串里含裸引号或反斜杠 —— 会破坏 JS 语法")
    # 5) 分类器必须还在用 continue 分支（我们只改文案，不该动机制）
    if 'e.continuationCount<Wxa?"continue":"exhausted"' not in text:
        fails.append("续写分类器被改动了（本补丁只应改文案）")

    if fails:
        print("SELFTEST 失败:")
        for f in fails:
            print("  -", f)
        return 1
    print("SELFTEST 通过：旧指令已清除，替代动作齐备，字符串合法，分类器未被改动。")
    return 0


def main():
    p = argparse.ArgumentParser(description="zcode 截断续写补丁")
    p.add_argument("--bundle", default=os.environ.get("ZCODE_CJS", BUNDLE_DEFAULT))
    p.add_argument("--check", action="store_true")
    p.add_argument("--revert", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        return selftest()
    if not os.path.exists(args.bundle):
        print(f"❌ 找不到 bundle: {args.bundle}", file=sys.stderr)
        return 1
    if args.check:
        return do_check(args.bundle)
    if args.revert:
        return do_revert(args.bundle)
    return do_apply(args.bundle, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
