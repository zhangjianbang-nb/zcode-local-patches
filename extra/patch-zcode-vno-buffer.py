#!/usr/bin/env python3
"""zcode.cjs Vno bufferTokens patch — autoCompact 阈值调节（2026-09-25）

背景: FROZEN-VERSION.txt 记着旧 build 的 `patches=ETr=211e3` —— 把默认
bufferTokens 从 13e3 放大, 让 autoCompact 提前触发。升级到 3.14.3-27 后
常量名 ETr -> Vno, 补丁静默丢失, 阈值退回默认。本脚本把该补丁重放并参数化。

决策公式 (qdt / Rhn / Dhn / ANe):
  outputReserve = min(maxOutputTokens ?? 32e3, 21e3)      // cZs=21e3 硬上限
  effective     = contextWindow - outputReserve
  threshold     = max(0, effective - (bufferTokens ?? Vno))
  触发          = tokenCount >= threshold

代入现役真源 (v2/provider_config.json):
  contextWindow  = 1048576  (properties.contextWindow, 2026-10-01 TensorFold 上线后)
  maxOutputTokens= 32768    (optionSpecs.maxOutputTokens.max)
  outputReserve  = min(32768, 21000) = 21000
  effective      = 1027576
  threshold      = 1027576 - Vno       # 262144 -> Vno=765432（现役）

  ★ DEFAULT_THRESHOLD 必须跟真源一起走: 窗口从 262000 升到 1048576 后,
    旧的 200000 默认值仍在范围内但语义已错 (会变成占窗口 19% 而非 76%),
    裸跑不会报错 —— 比越界更隐蔽, 必须显式同步。

注意: config.json / cli/setting.json 的 compact.bufferTokens 是**死键** ——
runtime 键注册表 fs 里没有 compact.* 一项, this.config.compact 恒 undefined,
ZAo 里的 `...this.config.compact` 展开为空。全包 bufferTokens 只出现 1 次
(就是 ANe 里那行), 任何配置文件都填不进来。要改阈值只能动这个常量。

不动 contextWindow: 压 contextWindow 也能降阈值 (threshold=W-34000), 但会
连带牺牲 354K 可用窗口 + TUI ctx% 分母 + preflight 预算。改 Vno 是单点、
零附带损伤。contextWindow 安全上限 W<=356872 不受影响。

用法:
  python3 patch-zcode-vno-buffer.py                      # 应用 DEFAULT_THRESHOLD
  python3 patch-zcode-vno-buffer.py --threshold 200000   # 设成任意阈值
  python3 patch-zcode-vno-buffer.py --check              # 查现状, 不改
  python3 patch-zcode-vno-buffer.py --revert             # 还原 .bak (回到默认 13e3)
"""
import sys, re, shutil, os, subprocess

ZCODE = "/home/whu/.npm-global/lib/node_modules/zcode-app-cli/vendor/zcode.cjs"
BAK = ZCODE + ".bak-vno-buffer-20260925"

# 阈值想设多少就改这里 / 或用 --threshold
# 2026-10-01 修正: 窗口 262000 -> 1048576 (TensorFold 引擎上线), 阈值 200000 -> 262144
#   (占 1048576 窗口 25.0%, 对应 Vno=765432)。
#   ★ 硬约束: T + max_tokens(32768) ≤ 1048576 → T ≤ 1015808, 262144 远在其下。
#   ★ 改这里必须与 bundle 实际值一致 —— 守卫自动重打时用的就是这个默认值,
#     不一致会导致下次升级后阈值被悄悄换回。
DEFAULT_THRESHOLD = 120000

# ★ Vno 是**全局唯一常量**，所以「阈值」只对**实际在用的那个模型**有意义。
#   2026-10-14: provider_config 里有两条模型规则 (GLM 1048576 / DS4.1 524288)，
#   原来的 context_window() 取「第一条」(GLM) → 为错的模型反推 Vno。
DEFAULT_MODEL = "DeepSeek-V4.1-Flash-TF"

# 决策公式里的常量（见上）
PROVIDER_CONFIG = "/home/whu/.zcode/v2/provider_config.json"
OUTPUT_RESERVE = 21000          # min(maxOutputTokens=32768, cZs=21000)


def context_window() -> int:
    """contextWindow 真源 = provider_config.json 的 properties.contextWindow，**按模型选**。

    2026-09-27 起不再硬编码 —— 硬编码的 354000 曾与真源漂移 (实际 292000),
    导致 --check 报的 threshold 是幻觉值。

    2026-10-14: 该文件现在有**多条**模型规则，取「第一条」相当于拿错的窗口反推
    全局唯一的 Vno。默认用 DEFAULT_MODEL，`--model <id>` 可覆盖。
    找不到指定模型时：若所有规则窗口一致就照用，否则**报错退出**（宁可炸也不静默算错）。"""
    import json
    model = DEFAULT_MODEL
    if "--model" in sys.argv:
        i = sys.argv.index("--model")
        try:
            model = sys.argv[i + 1]
        except IndexError:
            raise SystemExit("用法: --model <modelId>")
    d = json.load(open(PROVIDER_CONFIG, encoding="utf-8"))
    rules = d["config"]["modelConfigRules"]["providerModelRules"]
    seen = {}
    for r in rules:
        w = r.get("config", {}).get("properties", {}).get("contextWindow")
        if w:
            seen[r.get("modelId")] = int(w)
    if model in seen:
        return seen[model]
    if len(set(seen.values())) == 1:
        return next(iter(seen.values()))
    raise SystemExit(f"provider_config.json 里没有模型 {model!r}；可选: {sorted(seen)}")


CONTEXT_WINDOW = context_window()
EFFECTIVE = CONTEXT_WINDOW - OUTPUT_RESERVE

# 常量块锚点（2026-10-20 起按「用法」发现，不写死名字）：
#   autoCompact 的默认 buffer 在 bundle 里长这样
#     function A4e(e={}){let t=Ibn(e),n=ave(e.bufferTokens)??Apo;...}
#   先用 `ave(e.bufferTokens)??<NAME>` 找到常量名，再改 `NAME=<数值>`。
#   ★ 历史上这里是写死的 `Vno=`；3.15.3-36 升级后 minifier 把该名字挪用了
#     （Vno 变成了插件 UI 解析函数），写死锚点直接失配 → 补丁静默丢失。
BUFFER_REF_RE = re.compile(r"\(\s*e\.bufferTokens\s*\)\s*\?\?\s*(?P<var>[A-Za-z0-9_$]+)")
FALLBACK_VNO_RE = re.compile(r"Vno=([0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?)")


def current_vno(s: str):
    """返回默认 buffer 常量的字面量（如 '13e3'），找不到返回 None。"""
    m = BUFFER_REF_RE.search(s)
    if m:
        var = m.group("var")
        vm = re.search(re.escape(var) + r"=([0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?)", s)
        if vm:
            return vm.group(1)
    hits = FALLBACK_VNO_RE.findall(s)
    return hits[0] if hits else None


def replace_vno(s: str, want_lit: str):
    """把默认 buffer 常量改成 want_lit，返回 (new_s, n)。"""
    m = BUFFER_REF_RE.search(s)
    if m:
        var = m.group("var")
        return re.subn(re.escape(var) + r"=([0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?)",
                       f"{var}={want_lit}", s, count=1)
    return FALLBACK_VNO_RE.subn(f"Vno={want_lit}", s, count=1)


def num_to_lit(n: int) -> str:
    """71000 -> '71e3'，与上游 minified 写法保持一致；非整千就写十进制。"""
    if n % 1000 == 0 and n != 0:
        return f"{n // 1000}e3"
    return str(n)


def lit_to_num(lit: str) -> int:
    return int(float(lit))          # '71e3' -> 71000.0 -> 71000


def read() -> str:
    return open(ZCODE, encoding="utf-8", errors="replace").read()


def parse_threshold(raw: str) -> int:
    """'262k'/'262K'/'262,000'/'262000'/'0.262M' -> 262000"""
    raw = raw.replace(",", "").strip()
    mult = 1
    low = raw.lower()
    if low.endswith("k"):
        mult, raw = 1000, raw[:-1]
    elif low.endswith("m"):
        mult, raw = 1000000, raw[:-1]
    return int(float(raw) * mult)


def selftest():
    cases = {
        "262000": 262000, "262,000": 262000, "262k": 262000, "262K": 262000,
        "0.262M": 262000, "262": 262, "13e3": 13000, "320k": 320000,
    }
    bad = 0
    for raw, want in cases.items():
        try:
            got = parse_threshold(raw)
        except Exception as e:
            print(f"  FAIL {raw!r}: raised {e}")
            bad += 1
            continue
        ok = got == want
        if not ok:
            bad += 1
        print(f"  {'ok ' if ok else 'FAIL'} {raw!r:12} -> {got}  (want {want})")
    # 常量字面量往返
    for n in (71000, 133000, 13000, 262000, 70856):
        lit = num_to_lit(n)
        back = lit_to_num(lit)
        ok = back == n
        if not ok:
            bad += 1
        print(f"  {'ok ' if ok else 'FAIL'} lit({n})={lit!r} -> {back}")
    print("selftest:", "ALL PASS" if bad == 0 else f"{bad} FAILED")
    sys.exit(1 if bad else 0)


def main():
    if "--selftest" in sys.argv:
        selftest()

    if "--check" in sys.argv:
        s = read()
        lit = current_vno(s)
        if lit is None:
            print("NOT patched (找不到 Vno= 常量, 升级后源码变了)")
            sys.exit(1)
        vno = lit_to_num(lit)
        thr = EFFECTIVE - vno
        is_default = vno == 13000
        # ★ 判据是「有效阈值达没达到」，不是「Vno 被改过」。
        #   threshold = contextWindow − 21000 − Vno，同一个目标值有两条等价路径：
        #     A) 直改常量      : Vno = EFFECTIVE − DEFAULT_THRESHOLD（如 1048576 窗口 → Vno=765432）
        #     B) 默认常量+对齐 : contextWindow = DEFAULT_THRESHOLD + 21000 + 13000 = 296144
        #                        → 默认 Vno=13e3 自己就落出 262144
        #   B 完全不依赖 bundle，npm 升级清不到它 —— 是更稳的形态。
        #   旧判据只看 is_default，会把 B 误报成「未打补丁」→ 守卫永远 exit 1 →
        #   path unit 反复重启撞 start-limit 死掉（2026-10-03 就是这样丢了 18 小时）。
        thr_ok = thr == DEFAULT_THRESHOLD
        route = ("默认值 + contextWindow 对齐（不依赖 bundle，抗升级）"
                 if is_default and thr_ok
                 else "bundle 直改常量" if thr_ok
                 else "—")
        print(f"Vno            = {lit}  ({vno})")
        print(f"threshold      = {thr}   (目标 {DEFAULT_THRESHOLD})")
        print(f"contextWindow  = {CONTEXT_WINDOW}  (真源: {PROVIDER_CONFIG})")
        print(f"占窗口比       = {thr / CONTEXT_WINDOW * 100:.1f}%")
        print(f"达成方式       = {route}")
        print("状态           = " + ("✅ 已就位" if thr_ok else "❌ 未达成目标阈值"))
        sys.exit(0 if thr_ok else 1)

    if "--revert" in sys.argv:
        if os.path.exists(BAK):
            shutil.copy(BAK, ZCODE)
            print("reverted from", BAK)
        else:
            print("no backup at", BAK)
        r = subprocess.run(["node", "--check", ZCODE], capture_output=True, text=True)
        print("node --check:", "OK" if r.returncode == 0 else r.stderr)
        return

    # 目标阈值
    if "--threshold" in sys.argv:
        i = sys.argv.index("--threshold")
        try:
            threshold = parse_threshold(sys.argv[i + 1])
        except (IndexError, ValueError):
            print("用法: --threshold <整数或带 k/m, 例 262000 或 262k 或 0.262M>")
            sys.exit(2)
    else:
        threshold = DEFAULT_THRESHOLD

    if threshold <= 0 or threshold >= EFFECTIVE:
        print(f"阈值必须在 (0, {EFFECTIVE}) 内 —— 上限是 effective={EFFECTIVE}, 否则等于永不触发")
        sys.exit(2)

    want_vno = EFFECTIVE - threshold
    want_lit = num_to_lit(want_vno)

    s = read()
    lit = current_vno(s)
    if lit is None:
        print("找不到 Vno= 常量, 升级后源码变了, 需人工比对")
        sys.exit(1)

    # 幂等 / 幂等改值: 无论当前是 13e3 还是已打过的值, 直接改写
    if not os.path.exists(BAK):
        shutil.copy(ZCODE, BAK)

    new_s, n = replace_vno(s, want_lit)
    assert n == 1, f"替换次数={n}"
    if new_s == s:
        print(f"已是目标值 Vno={want_lit}, skip")
    else:
        open(ZCODE, "w", encoding="utf-8").write(new_s)
        print(f"patched OK: Vno={lit} -> {want_lit}   threshold={threshold}")

    r = subprocess.run(["node", "--check", ZCODE], capture_output=True, text=True)
    print("node --check:", "OK" if r.returncode == 0 else r.stderr)
    if r.returncode != 0:
        sys.exit(1)

    # 自检: 读回确认
    back = current_vno(read())
    got = EFFECTIVE - lit_to_num(back)
    assert got == threshold, f"回读 threshold={got} != 目标 {threshold}"
    print(f"回读校验: Vno={back}, threshold={got} ✓")


if __name__ == "__main__":
    main()
