#!/usr/bin/env python3
"""(B) zcode 循环守卫 —— 目标不是掐死进程，而是**打断循环后让 zcode 继续正常干活**。

## 问题（2026-09-29 取证）

zcode 自带 modelAnomalyGuard 的判据是「连续严格相同」：
    t.repeatedToolCallSignature === u ? streak += 1 : (签名 = u, streak = 1)
    if (t.repeatedToolCallStreakCount !== o) continue;          // o = 3
对 A,B,C,…,I,A,B,C,… 这种**周期型回路**，streak 每次都被重置回 1，永远够不到 3 ——
实测 348 次 Read 轮读 9 个 PNG，相邻相同 0 次，守卫全程静默（该时段 ModelAnomalyWarning = 0）。
而且它**只注入一句劝告、从不改变能力**，yolo 模式下模型直接无视。

## 修复：注入提示词为主，禁工具为兜底，中止是最后手段

### 前提：判据必须看得见「内容有没有变」（文件指纹）

只按 `(tool, input)` 判重，会把两件相反的事混为一谈 —— 实测：

  合法迭代（同一文件反复 渲染→看图→改，每轮内容都变）→ 第 5 轮禁掉 Bash+Read（读图直接不可用）
  真死循环（反复读同一张没变的图）                    → 同样第 5 轮被禁，签名一模一样

所以签名并入**文件指纹** `mtimeMs-size`（`require("fs").statSync`）：
内容变了 = 新签名 → 放行；内容没变 = 重复 → 这才是要治的病。
没有指纹的调用（Bash/Grep/WebFetch）无法证明「结果没变」，**主动不参与重复判定** ——
宁可漏报（纯非文件回路交给外部 watchdog），也不误伤。

判据：两条，覆盖不同循环形态
  判据1「最近 W 次调用里该签名出现几次」—— 精确、能指名道姓，覆盖周期 ≤ W/Q 步
  判据2「连续 N 次调用没有产生新签名」—— 长周期回路的本质是整个周期不产生新信息，
          所以它不随周期衰减（判据1 对周期 40 的回路读数最高只有 2，够不到 6）

退让阶梯：
  L1【主力】count >= o(运行时配置，默认 3) → **注入纠正提示**
      原生实现只在「计数恰好等于 3」时发一句、且受「每回合 3 条」预算限制，
      计数涨到 4/5/6… 就彻底哑了 —— 这是模型无视劝告的原因之一。
      这里改成：重复程度**每加深一次**就注入一条（绕过预算），每回合封顶 maxwarn=12。
      滑动窗口里计数会自然饱和，所以回路稳定后注入自动停止，不会刷爆上下文。
  L2【兜底】禁工具 —— 已核实 turn 循环里**每步重算**工具表：

      let f = o1a(e),                      // o1a 读 e.toolDisallowlist
          g = f ? this.getTools(e.model).filter(z => !f.has(z.name)) : this.getTools(e.model);

  （`o1a` = `buildTurnDisallowedTools`。）把工具名 push 进 turn 的 `toolDisallowlist`，
  **下一步模型就看不到这个工具了** —— 物理上无法再重复，只能换工具/换办法/汇报，
  而会话和进程都完好。作用域仅本回合，下一条用户消息自动恢复。
  同时清零 `anomalyWarningsInjected`，否则原生「每回合 3 条」预算会把通知吞掉。
  L3 已禁 maxq=3 个工具还在绕 / L4 同签名占满窗口一半 → throw 中止（最后手段）。

## 告警文案也一起加强

原文案只有三句（"Do not repeat... unless the user explicitly asked"），太软。补丁在
`Oxo()` 的返回数组里追加两句可操作指令：重复不等于复核（拿到的是同样的字节）、
以及「若继续重复，本工具将在本回合被禁用」。

## 用法

  python3 patch-zcode-loop-guard.py --check              # 查状态
  python3 patch-zcode-loop-guard.py                      # 打补丁（默认参数见下方常量）
  python3 patch-zcode-loop-guard.py --window 80 --quarantine 8 --stale 25
  python3 patch-zcode-loop-guard.py --revert             # 还原（两处改动都还原）
  python3 patch-zcode-loop-guard.py --selftest           # 从补丁后的 bundle 抠出 Rxo 做真实功能验证

⚠️ 升级 zcode 会**静默丢掉**本补丁（与 Vno 补丁同命运），升级后必跑 --check。
⚠️ 打补丁后需**重启 zcode 进程**才生效。
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BUNDLE_DEFAULT = os.path.expanduser(
    "~/.npm-global/lib/node_modules/zcode-app-cli/vendor/zcode.cjs")

MARK = "__zlgWin"          # 幂等标记（同时是滑动窗口数组名）
WINDOW_DEFAULT = 60        # 判据窗口：最近多少次调用
QUARANTINE_DEFAULT = 6     # 窗口内同签名达到此数 → 本回合禁用该工具
ABORT_DEFAULT = 30         # 同签名占满窗口一半 → 中止（极端紧循环兜底）
MAXQ_DEFAULT = 3           # 本回合已禁用 N 个不同工具还在绕 → 中止
STALE_DEFAULT = 40         # 连续 N 次调用的签名都是本回合已见过的 → 长周期回路
MAXWARN_DEFAULT = 12       # 每回合最多注入多少条纠正提示（防止紧循环刷爆上下文）

# 为什么要两条判据（覆盖不同的循环形态）：
#   判据1「窗口内同签名计数」—— 精确、能指名道姓，但覆盖周期 ≤ WINDOW/QUARANTINE = 10 步。
#   判据2「连续无新签名」  —— 长周期回路的本质是**整个周期不产生任何新信息**，
#                            所以「连续 N 次调用都没见过新签名」不随周期衰减，
#                            周期 50 的回路也能抓（代价是需要跑完一个周期才开始计数）。
# 两者都走同一套退让：L2 禁工具 → L3/L4 才中止。
#
# 中止阈值必须与窗口自洽：周期 p 的回路里，同签名在窗口 W 内最多出现 floor(W/p) 次。
# 所以紧循环兜底用 W/2，跨工具兜底用「已禁用几个工具」（不随周期衰减）。

# ---------- 改动 1：Rxo 判据 + 三级退让 ----------

RXO_ORIG = ('let a=[];for(let c of e){let u=oLa(c.name,c.input);'
            'if(t.repeatedToolCallSignature===u?t.repeatedToolCallStreakCount+=1:'
            '(t.repeatedToolCallSignature=u,t.repeatedToolCallStreakCount=1),'
            't.repeatedToolCallStreakCount!==o)continue;')

RXO_TMPL = (
    'let a=[];'
    # ★ 2026-10-20：上游 minifier 把循环变量 l 改成 c、KSa 改成 oLa。
    #   模板内部仍用 l / l.name（可读性好），这里把 c 绑成 l 即可，改动面最小。
    'for(let c of e){{let l=c;'
    # 【关键】文件指纹：让判据看得见「目标内容有没有变」。
    # 只按 (工具,入参) 判重会把两种完全相反的情况混为一谈 —— 实测：
    #   合法迭代（同一文件反复 渲染→看图→改，每轮内容都变）→ 第 5 轮就被禁掉 Bash+Read
    #   真死循环（反复读同一张没变的图）        → 同样在第 5 轮被禁
    # 两者签名一模一样。带上 mtime+size 之后：内容变了 = 新签名（放行），
    # 内容没变 = 重复（这才是不产生新信息的空转，才是要治的病）。
    'let pp="",fp=l.input&&(l.input.file_path||l.input.notebook_path||l.input.path);'
    'try{{if(fp){{let s1=require("fs").statSync(fp);'
    'pp="|"+s1.mtimeMs+"-"+s1.size}}}}catch(e1){{}}'
    # 没有指纹的调用（Bash/Grep/WebFetch 等）无法证明「结果没变」，因此**不参与重复判定**：
    # 给个每次唯一的名字，其计数恒为 1。宁可漏报（这类纯非文件回路交给外部 watchdog），
    # 也不能误伤 —— 实测误伤会把合法迭代直接掐死。
    'let u=pp?oLa(l.name,l.input)+pp:"uniq"+(t.__zlgU=(t.__zlgU||0)+1);if('
    't.{MARK}||(t.{MARK}=[]),t.{MARK}.push(u),'
    't.{MARK}.length>{WINDOW}&&t.{MARK}.splice(0,t.{MARK}.length-{WINDOW}),'
    # 与签名窗口对齐的工具名窗口：长周期判据要靠它挑出「最常重复的那个工具」
    't.__zlgTw||(t.__zlgTw=[]),t.__zlgTw.push(l.name||""),'
    't.__zlgTw.length>{WINDOW}&&t.__zlgTw.splice(0,t.__zlgTw.length-{WINDOW}),'
    't.repeatedToolCallSignature=u,'
    't.repeatedToolCallStreakCount=t.{MARK}.filter(function(x){{return x===u}}).length,'
    # 本回合是否见过这个签名（键加 "s" 前缀，避免撞 Object.prototype）
    't.__zlgSeen||(t.__zlgSeen={{}}),'
    't.__zlgSeen["s"+u]?(t.__zlgStale=(t.__zlgStale||0)+1)'
    ':(t.__zlgSeen["s"+u]=1,t.__zlgStale=0),'
    # L4 极端紧循环：同一入参占了窗口一半以上
    't.repeatedToolCallStreakCount>={ABORT})throw new Error('
    '"LOOP_GUARD: "+l.name+" repeated "+t.repeatedToolCallStreakCount+'
    '" times within the last {WINDOW} tool calls - tight loop, aborting turn '
    '(zcode loop-guard patch)");'
    # L1【主力】注入纠正提示：重复程度每加深一次就注入一条
    #    原生实现只在「计数恰好等于阈值(3)」时发一句，且受「每回合 3 条」预算限制，
    #    计数涨到 4/5/6… 就彻底哑了 —— 这正是模型无视劝告的原因之一。
    #    这里改成：计数比上次注入时更高（=重复程度在加深）就注入，绕过预算；
    #    但每回合封顶 {MAXWARN} 条，避免紧循环把上下文刷爆。
    #    计数在滑动窗口里会自然饱和（周期回路稳定后不再增长），注入也就自动停下。
    'if(t.repeatedToolCallStreakCount>=o&&l.name){{'
    't.__zlgW||(t.__zlgW={{}});'
    't.repeatedToolCallStreakCount>(t.__zlgW["w"+u]||0)&&('
    't.__zlgW["w"+u]=t.repeatedToolCallStreakCount,'
    't.__zlgWn=(t.__zlgWn||0)+1,'
    't.__zlgWn<={MAXWARN}&&a.push({{observedCount:t.repeatedToolCallStreakCount,'
    'threshold:o,toolCallId:l.id,toolName:l.name,warningInjected:!0}}))'
    '}}'
    # L2-a 判据1：短周期回路，精确指名（兜底：注入劝不动才禁工具）
    'if(t.repeatedToolCallStreakCount>={QUARANTINE}&&l.name){{'
    't.toolDisallowlist||(t.toolDisallowlist=[]);'
    'if(t.toolDisallowlist.indexOf(l.name)<0){{'
    't.toolDisallowlist.push(l.name),t.anomalyWarningsInjected=0,'
    't.__zlgQ||(t.__zlgQ=[]),t.__zlgQ.push(l.name),'
    'a.push({{observedCount:t.repeatedToolCallStreakCount,threshold:{QUARANTINE},'
    'toolCallId:l.id,toolName:l.name,warningInjected:!0}});'
    'if(t.__zlgQ.length>={MAXQ})throw new Error('
    '"LOOP_GUARD: turn kept looping after disabling "+t.__zlgQ.length+'
    '" different tools ("+t.__zlgQ.join("/")+") - aborting turn '
    '(zcode loop-guard patch)");'
    '}}'
    '}}'
    # L2-b 判据2：连续 {STALE} 次调用都没产生新签名 —— 长周期回路（周期 > WINDOW/QUARANTINE）
    'if(t.__zlgStale>={STALE}){{'
    'let st=t.__zlgStale;t.__zlgStale=0;'
    'let c={{}},best="",bn=0,ii;'
    'for(ii=0;ii<t.__zlgTw.length;ii++){{'
    'let nm=t.__zlgTw[ii];c["t"+nm]=(c["t"+nm]||0)+1;'
    'if(c["t"+nm]>bn){{bn=c["t"+nm];best=nm}}'
    '}}'
    't.toolDisallowlist||(t.toolDisallowlist=[]);'
    'if(best&&t.toolDisallowlist.indexOf(best)<0){{'
    't.toolDisallowlist.push(best),t.anomalyWarningsInjected=0,'
    't.__zlgQ||(t.__zlgQ=[]),t.__zlgQ.push(best),'
    'a.push({{observedCount:st,threshold:{STALE},toolCallId:l.id,'
    'toolName:best,warningInjected:!0}});'
    'if(t.__zlgQ.length>={MAXQ})throw new Error('
    '"LOOP_GUARD: turn kept looping after disabling "+t.__zlgQ.length+'
    '" different tools ("+t.__zlgQ.join("/")+") - aborting turn '
    '(zcode loop-guard patch)");'
    '}}'
    '}}'
    # 无条件 continue：原实现的尾部（`let f=…,a.push({…})}`）只在计数恰好等于 o 时
    # 推一条，且受「每回合 3 条」预算限制。我们的 L1 已经接管注入，不 continue 会重复推。
    # 那段代码因此变成死代码（仍在 for 体内，闭合括号仍然配平）。
    # ⚠️ 这里**不能**再补一个 `}`：原片段 ORIG 到 `continue;` 为止，
    # 并不包含 for 体的闭合括号（原文后面还有 `let f=…` 在循环体内）。
    # 多补一层就会把函数提前闭合 → 文件末尾多出一个 `}` 的语法错（踩过）。
    'continue;')

# ---------- 改动 2：Oxo 告警文案 ----------

# ★ 2026-10-20：Oxo → zOo（minifier 改名）。用告警首句定位，不写死函数名。
OXO_RE = re.compile(
    r'function [A-Za-z0-9_$]+\(e,t\)\{return\[`You have called \$\{e\}.*?\]\.join\(`\n`\)\}', re.S)

OXO_EXTRA = (
    # 注意开头的逗号：插在原数组最后一个元素之后，少了它就是 "…""…" 的语法错
    ',"Repeating the identical call is NOT double-checking: you get the exact same bytes back. '
    'If you keep repeating, this tool will be DISABLED for the rest of this turn.",'
    '"Stop re-reading / re-checking the same target. Write down what you have already confirmed '
    'and what is still unknown, then EITHER take a materially different approach (script it, use '
    'a different tool, summarise what you have) OR report the blocker to the user and finish the '
    'turn.",'
)


def build_rxo(window, quarantine, abort, maxq, stale, maxwarn):
    return RXO_TMPL.format(MARK=MARK, WINDOW=window, QUARANTINE=quarantine,
                           ABORT=abort, MAXQ=maxq, STALE=stale, MAXWARN=maxwarn)


def read_bundle(path):
    with open(path, encoding="utf8", errors="surrogateescape") as f:
        return f.read()


def write_bundle(path, text):
    with open(path, "w", encoding="utf8", errors="surrogateescape") as f:
        f.write(text)


def node_check(path):
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True,
                           text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"跑不了 node --check: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:400]
    return True, "ok"


# 用首尾锚点定位 Rxo 的判据区段：起点在原始版和所有补丁版里都存在，终点是区段内唯一的
# `continue;`。⚠️ 曾经把前缀写死成 `let u=KSa(l.name,l.input);`，指纹版把那行改成
# `let u=pp?...` 之后就匹配不上了 → 脚本误判「已是目标状态」、什么都没做（差点静默漏装）。
# 起点锚点必须**唯一**：只用 `let a=[];for(let l of e){` 会命中 2 处（另一处是无关函数
# Aqt 里的深比较循环），re.search 取第一个 → 会改坏无关代码。加上前面那段就唯一了。
_LEAD = "let a=[];for(let c of e){"
RXO_ANCHOR = "if(o<=0)return[];" + _LEAD
# 区段 = 锚点 + 原始片段去掉重复前缀的部分；补丁版 = 锚点 + 模板。
# 三者共用同一个起点，替换才有对称性（span ↔ RXO_ORIG_FULL ↔ 锚点+patch）。
RXO_ORIG_BODY = RXO_ORIG[len(_LEAD):]
RXO_ORIG_FULL = RXO_ANCHOR + RXO_ORIG_BODY
RXO_PATCH_RE = re.compile(re.escape(RXO_ANCHOR) + r".*?continue;", re.S)


def full_patch(patch):
    """锚点里已经含 `let a=[];for(let l of e){`，而模板也以它开头 —— 直接相加会写两遍，
    多出一个 `{`（区段括号深度变 2）。所以要把模板的重复前缀剥掉。"""
    if not patch.startswith(_LEAD):
        raise ValueError("模板前缀与 _LEAD 不一致，拒绝拼装")
    return RXO_ANCHOR + patch[len(_LEAD):]


def describe(src):
    m = RXO_PATCH_RE.search(src)
    if not m:
        return None
    span = m.group(0)
    patched = MARK in span
    info = {"patched": patched,
            "pristine": span == RXO_ORIG_FULL,
            "orig_gone": RXO_ORIG not in src,
            "fingerprint": "__zlgU" in span,
            "oxo_patched": "NOT double-checking" in src}
    m = re.search(r"t\.__zlgWin\.length>(\d+)", src)
    info["window"] = int(m.group(1)) if m else None
    m = re.search(r"repeatedToolCallStreakCount>=(\d+)&&l\.name", src)
    info["quarantine"] = int(m.group(1)) if m else None
    m = re.search(r"repeatedToolCallStreakCount>=(\d+)\)throw", src)
    info["abort"] = int(m.group(1)) if m else None
    m = re.search(r"t\.__zlgQ\.length>=(\d+)\)throw", src)
    info["maxq"] = int(m.group(1)) if m else None
    m = re.search(r"t\.__zlgStale>=(\d+)\)", src)
    info["stale"] = int(m.group(1)) if m else None
    m = re.search(r"t\.__zlgWn<=(\d+)&&a\.push", src)
    info["maxwarn"] = int(m.group(1)) if m else None
    info["inject_first"] = "__zlgWn" in src
    return info


def is_target(info, window, quarantine, abort, maxq, stale, maxwarn):
    return bool(info) and info["patched"] and info["fingerprint"] and (
        info["window"], info["quarantine"], info["abort"], info["maxq"],
        info["stale"], info["maxwarn"]) == (window, quarantine, abort, maxq, stale, maxwarn)


def do_check(bundle):
    src = read_bundle(bundle)
    info = describe(src)
    if info is None or not info["patched"]:
        print(f"未打补丁。bundle={bundle}")
        print(f"  RXO 原片段存在: {RXO_ORIG in src}（应为 True）")
        # 退出码约定（与 patch-zcode-slash-path.py 一致，也是 zcode-patches.sh
        # run_patch() 的契约）：0=已就位，1=缺失/异常。
        # 2026-10-09 修：此处原本 return 0 —— 于是 `--check` 一边打印「未打补丁」、
        # 一边 exit 0，而 zcode-patches.sh 只看退出码 → 对 loop-guard 打出**假绿灯**，
        # 补丁实际缺失多日无人察觉（真凶：npm 升级洗掉后守卫判定为「已就位」不重放）。
        return 1
    print(f"已打补丁。bundle={bundle}")
    print(f"  文件指纹  : {'有（内容变了就不算重复）' if info['fingerprint'] else '无 ← 旧版！会误伤合法迭代，必须重放'}")
    print(f"  window     = {info['window']}   （判据窗口：最近多少次调用内统计重复）")
    print(f"  quarantine = {info['quarantine']}   （窗口内同签名达到此数 → 本回合禁用该工具 = 退出循环）")
    print(f"  abort      = {info['abort']}  （同签名占满窗口一半 → 中止，极端紧循环兜底）")
    print(f"  maxq       = {info['maxq']}   （本回合禁用 N 个工具后还在绕 → 中止）")
    print(f"  stale      = {info['stale']}  （连续 N 次调用没有新签名 → 长周期回路，禁用最常重复的工具）")
    print(f"  maxwarn    = {info['maxwarn']}  （每回合最多注入几条纠正提示）")
    print(f"  注入优先  : {'是（每次重复加深都注入，禁工具为兜底）' if info['inject_first'] else '否 ← 旧版，建议重放'}")
    print(f"  告警文案增强: {'是' if info['oxo_patched'] else '否 ← 缺失，建议重放'}")
    if info["window"] and info["quarantine"]:
        print(f"  判据1（窗口内同签名）覆盖循环周期 ≤ {info['window'] // info['quarantine']} 步")
    if info["stale"]:
        print(f"  判据2（连续无新签名）覆盖长周期回路：约「跑完一个周期 + {info['stale']} 次」后禁用")
    if info["window"] and info["quarantine"]:
        print(f"  更长周期交给 watchdog zcode-loop-watchdog.py（15 分钟内报警）")
    if not info["orig_gone"]:
        print("  ⚠️ RXO 原片段仍在 —— 补丁可能打了一半，请 --revert 后重来")
    return 0


def apply_patch(bundle, window, quarantine, abort, maxq, stale, maxwarn,
                dry_run=False):
    src = read_bundle(bundle)
    new = src
    actions = []

    patch = build_rxo(window, quarantine, abort, maxq, stale, maxwarn)
    params = (f"window={window}, quarantine={quarantine}, abort={abort}, "
              f"maxq={maxq}, stale={stale}, maxwarn={maxwarn}")
    m = RXO_PATCH_RE.search(new)
    if m is None:
        print(f"❌ 找不到 Rxo 判据区段（连原始片段都没有）—— zcode 版本变了，本补丁不适用。\n"
              f"   拒绝改动，bundle 保持原样。", file=sys.stderr)
        return 1
    cur = describe(src)
    if cur and not cur["pristine"] and not cur["patched"]:
        print("❌ 区段既不是原始片段、也没有本补丁的标记 —— 拒绝改动。", file=sys.stderr)
        return 1
    if is_target(cur, window, quarantine, abort, maxq, stale, maxwarn):
        actions.append(f"RXO 已是目标状态（{params}，含指纹）")
    else:
        was = "首次打补丁" if (cur and cur["pristine"]) else "更新/升级"
        new = new[:m.start()] + full_patch(patch) + new[m.end():]
        actions.append(f"RXO {was} → {params}")

    om = OXO_RE.search(new)
    if not om:
        print("❌ 找不到 Oxo() 告警文案函数 —— 版本变了，拒绝改动。", file=sys.stderr)
        return 1
    if "NOT double-checking" in om.group(0):
        actions.append("Oxo 文案已是增强版")
    else:
        aug = om.group(0).replace("].join(`\n`)", OXO_EXTRA + "].join(`\n`)")
        new = new[:om.start()] + aug + new[om.end():]
        actions.append("Oxo 文案追加可操作指令")

    if all("已是" in a for a in actions):
        print("已是目标状态，无需改动：")
        for a in actions:
            print("  -", a)
        return 0

    if dry_run:
        print("[dry-run] 将执行：")
        for a in actions:
            print("  -", a)
        return 0

    bak = f"{bundle}.bak-loopguard-{datetime.date.today():%Y%m%d}"
    if not os.path.exists(bak):
        shutil.copy2(bundle, bak)
        print(f"已备份 → {bak}")

    write_bundle(bundle, new)
    ok, msg = node_check(bundle)
    if not ok:
        write_bundle(bundle, src)
        print(f"❌ 打补丁后语法校验失败，已回滚：\n{msg}", file=sys.stderr)
        return 1

    print("✅ 已应用：")
    for a in actions:
        print("  -", a)
    print("  node --check 通过")
    print("  ⚠️ 需**重启 zcode 进程**才生效（已跑的会话仍用旧代码）")
    return 0


def revert(bundle):
    src = read_bundle(bundle)
    new = src
    did = []
    m = RXO_PATCH_RE.search(new)
    # 必须还原成 RXO_ORIG_FULL（含锚点 `if(o<=0)return[];`），不是 RXO_ORIG ——
    # 区段起点在锚点上，用 RXO_ORIG 替换会把这 17 字节吃掉（踩过，cmp 抓出来的）。
    if m and m.group(0) != RXO_ORIG_FULL:
        new = new[:m.start()] + RXO_ORIG_FULL + new[m.end():]
        did.append("RXO 判据还原为原始 streak")
    om = OXO_RE.search(new)
    if om and "NOT double-checking" in om.group(0):
        aug = om.group(0).replace(OXO_EXTRA, "")
        new = new[:om.start()] + aug + new[om.end():]
        did.append("Oxo 文案还原")
    if not did:
        print("当前没打补丁，无需还原。")
        return 0
    write_bundle(bundle, new)
    ok, msg = node_check(bundle)
    if not ok:
        write_bundle(bundle, src)
        print(f"❌ 还原后语法校验失败，已回滚：\n{msg}", file=sys.stderr)
        return 1
    print("✅ 已还原：" + "；".join(did) + "；node --check 通过。")
    return 0


# ---------- 自测 ----------

def extract_function(src, name):
    """从 bundle 里抠出 function <name>(...) 的完整源码（含嵌套大括号）。"""
    i = src.index(f"function {name}(")
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        c = src[k]
        if c in "\"'`":
            q, k = c, k + 1
            while k < len(src) and src[k] != q:
                k += 2 if src[k] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise ValueError(f"没能抠出 {name}")


HARNESS = r"""
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(process.argv[2], 'utf8');
const fnSrc = process.argv[3];
const cfg = JSON.parse(process.argv[4]);
const DIR = process.argv[5];

const KSa = (name, input) => name + '|' + JSON.stringify(input);
const oLa = KSa;   // 3.15.3-36+ 上游 minifier 把同名 hash 函数改叫 oLa
eval(fnSrc);

const read  = (f) => ({ name: 'Read',  input: { file_path: f }, id: 'c1' });
const bash  = (c) => ({ name: 'Bash',  input: { command: c }, id: 'c2' });
const edit  = (f, n) => ({ name: 'Edit', input: { file_path: f, new_string: 'v' + n }, id: 'c3' });
const touch = (f, body) => fs.writeFileSync(f, body);          // 模拟「内容变了」
const fresh = () => ({ anomalyWarningsInjected: 0 });
const out   = (o) => console.log(JSON.stringify(o));

const P1 = path.join(DIR, 'p-1.png');
const RP = path.join(DIR, 'render.py');
touch(P1, 'img-v0'); touch(RP, 'code-v0');

// A) 【误伤回归】合法迭代：改代码 → 渲染（内容变）→ 看图 → 再改…
//    同一文件、同一渲染命令，但每次内容都变 → 绝不允许禁工具、也不该刷屏告警
let st = fresh(), injA = 0, bannedA = null;
for (let i = 0; i < 20; i++) {
  touch(RP, 'code-v' + i);            // 改代码
  touch(P1, 'img-v' + i);             // 重新渲染 → 内容变了
  for (const c of [edit(RP, i), bash('python3 render.py --page 1'), read(P1)]) {
    const a = Rxo([c], st, cfg);
    for (const e of (a || [])) if (e.warningInjected) injA++;
    if (bannedA === null && st.toolDisallowlist && st.toolDisallowlist.length) bannedA = i;
  }
}
out({ legitIteration: bannedA === null ? 'ok' : 'BANNED@' + bannedA,
      legitIterationInjected: injA,
      legitIterationBanned: JSON.stringify(st.toolDisallowlist || []) });

// B) 【要治的病】循环读图：反复读同一张**没变**的图
st = fresh();
let injB = 0, bannedB = null;
for (let i = 0; i < 20; i++) {
  const a = Rxo([read(P1)], st, cfg);
  for (const e of (a || [])) if (e.warningInjected) injB++;
  if (bannedB === null && st.toolDisallowlist && st.toolDisallowlist.length) bannedB = i;
}
out({ sameImageLoop: bannedB === null ? 'NOT-CAUGHT(bad)' : 'QUARANTINED@' + bannedB,
      sameImageLoopInjected: injB,
      sameImageLoopBanned: JSON.stringify(st.toolDisallowlist || []) });

// C) 纯非文件回路（重复同一条 Bash）：判据没有指纹、主动不参与 → 不该禁工具
st = fresh();
let bannedC = null;
for (let i = 0; i < 40; i++) {
  Rxo([bash('pytest -q tests/')], st, cfg);
  if (bannedC === null && st.toolDisallowlist && st.toolDisallowlist.length) bannedC = i;
}
out({ bashLoop: bannedC === null ? 'not-tracked(by-design)' : 'BANNED@' + bannedC });

// D) 极端紧循环：同一张没变的图连着读 30+ 次 → 必须 abort 兜底
st = fresh();
let threwD = null;
for (let i = 0; i < 200; i++) {
  try { Rxo([read(P1)], st, cfg); } catch (e) { threwD = { at: i, msg: e.message }; break; }
}
out({ tightLoop: threwD ? 'THREW@' + threwD.at : 'NO-THROW',
      tightLoopMsg: threwD ? threwD.msg.slice(0, 85) : '' });

// E) 多文件轮读（同一工具、多个没变的文件）：Read 一旦被禁，循环就断了 —— 不要求 abort
fs.writeFileSync(path.join(DIR, 'p-2.png'), 'x');
fs.writeFileSync(path.join(DIR, 'p-3.png'), 'x');
st = fresh();
let brokeAt = null;
const filesE = ['p-1.png', 'p-2.png', 'p-3.png'];
for (let i = 0; i < 60; i++) {
  Rxo([read(path.join(DIR, filesE[i % 3]))], st, cfg);
  if (brokeAt === null && st.toolDisallowlist && st.toolDisallowlist.indexOf('Read') >= 0) brokeAt = i;
}
out({ multiFileCycle: brokeAt === null ? 'NOT-CAUGHT(bad)' : 'LOOP-BROKEN@' + brokeAt,
      multiFileCycleBanned: JSON.stringify(st.toolDisallowlist || []) });

// F) 长周期回路：40 个没变的文件轮读（周期 40 > window/quarantine = 10）
st = fresh();
let lpAt = null, lpRule1Max = 0;
for (let i = 0; i < 260; i++) {
  const f = path.join(DIR, 'LP', 'f-' + (i % 40) + '.png');
  if (!fs.existsSync(f)) { fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, 'z'); }
  Rxo([read(f)], st, cfg);
  lpRule1Max = Math.max(lpRule1Max, st.repeatedToolCallStreakCount || 0);
  if (lpAt === null && st.toolDisallowlist && st.toolDisallowlist.length) lpAt = i;
}
out({ longPeriod: lpAt === null ? 'NOT-CAUGHT(bad)' : 'QUARANTINED@' + lpAt,
      longPeriodRule1Max: lpRule1Max,
      longPeriodBanned: JSON.stringify(st.toolDisallowlist || []) });

// G) 配合的模型：Read 一被禁用就换别的办法 → 不许 abort
st = fresh();
let switched = false, abortG = false;
for (let i = 0; i < 300; i++) {
  const banned = st.toolDisallowlist && st.toolDisallowlist.indexOf('Read') >= 0;
  if (banned && !switched) switched = true;
  const call = banned ? edit(RP, i) : read(P1);
  try { Rxo([call], st, cfg); } catch (e) { abortG = true; break; }
}
out({ recovered: (!abortG && switched) ? 'ok' : (abortG ? 'ABORTED(bad)' : 'NEVER-SWITCHED(bad)'),
      recoveredBanned: JSON.stringify(st.toolDisallowlist || []) });

// H) 零误伤：N 个不同文件各读一次
st = fresh();
let injH = 0, bannedH = false;
for (let i = 0; i < 200; i++) {
  const f = path.join(DIR, 'UNIQ', 'u-' + i + '.png');
  fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, 'q');
  try { const a = Rxo([read(f)], st, cfg); for (const e of (a || [])) if (e.warningInjected) injH++; }
  catch (e) { bannedH = true; break; }
}
out({ distinct: bannedH ? 'ABORTED(bad)' : (injH ? 'WARNED(' + injH + ')' : 'ok'),
      distinctBanned: (st.toolDisallowlist || []).length });

// I) 合理重读：同一张没变的图读 4 次（< quarantine）不该被禁
st = fresh();
let injI = 0;
for (let i = 0; i < 4; i++) {
  const a = Rxo([read(P1)], st, cfg);
  for (const e of (a || [])) if (e.warningInjected) injI++;
}
out({ benignRepeat4: (st.toolDisallowlist || []).length ? 'BANNED(bad)' : 'ok',
      benignRepeat4Injected: injI });

// J) 回归证据：复刻**原始事故形态**（9 个文件轮读，相邻不同）——
//    旧判据「连续相同」对这种回路 streak 恒为 1，够不到 3，全程静默
let sig, streak, best = 0;
for (let i = 0; i < 36; i++) {
  const u = 'Read|' + JSON.stringify({ file_path: path.join(DIR, 'cyc', 'p-' + (i % 9) + '.png') });
  streak = (sig === u) ? streak + 1 : 1; sig = u; best = Math.max(best, streak);
}
out({ oldStreakMax: best });
"""


def selftest(bundle):
    src = read_bundle(bundle)
    if MARK not in src:
        print("❌ bundle 未打补丁，先跑一次本脚本再 --selftest。", file=sys.stderr)
        return 1
    # ★ 2026-10-20：函数名也会被 minifier 改（Rxo → BOo），按「含判据锚点的函数」动态定位。
    _m = re.search(r"function ([A-Za-z0-9_$]+)\(e,t,n\)\{[^{}]*if\(o<=0\)return\[\];", src)
    if _m is None:
        print("❌ 定位不到 loop-guard 宿主函数，无法自测", file=sys.stderr)
        return 1
    fn_src = f"var Rxo={_m.group(1)};\n" + extract_function(src, _m.group(1))
    cfg = {"repeatedToolCallWarningThreshold": 3, "maxBudgetWarningsPerTurn": 3}

    import shutil as _sh
    workdir = tempfile.mkdtemp(prefix="loopguard-test-")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(HARNESS)
        hpath = f.name
    try:
        r = subprocess.run(["node", hpath, bundle, fn_src, json.dumps(cfg), workdir],
                           capture_output=True, text=True, timeout=180)
    finally:
        os.unlink(hpath)
        _sh.rmtree(workdir, ignore_errors=True)

    if r.returncode != 0:
        print("❌ 自测脚本执行失败:\n" + (r.stderr or r.stdout)[:900], file=sys.stderr)
        return 1

    got = {}
    for line in r.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("{"):
            got.update(json.loads(line))
    for k, v in got.items():
        print(f"  {k} = {v}")

    fails = []
    # ★ 最要紧的一条：合法迭代（同一文件反复渲染+查看，内容每轮都变）绝不能被误伤。
    #   带文件指纹之前，实测这里会在第 5 轮把 Bash 和 Read 一起禁掉 —— 用户点名的问题。
    if got.get("legitIteration") != "ok":
        fails.append(f"合法迭代被误伤：{got.get('legitIteration')} "
                     f"（禁用 {got.get('legitIterationBanned')}）—— 会导致读图/渲染不可用")
    if got.get("legitIterationInjected", 99) > 0:
        fails.append(f"合法迭代被注入了 {got.get('legitIterationInjected')} 条「你在循环」告警 —— 误报")
    # 要治的病：反复读同一张没变的图，必须被抓住
    if not str(got.get("sameImageLoop", "")).startswith("QUARANTINED@"):
        fails.append(f"循环读图没被抓到：{got.get('sameImageLoop')}")
    if got.get("sameImageLoopInjected", 0) < 2:
        fails.append(f"循环读图只注入了 {got.get('sameImageLoopInjected')} 条纠正提示 —— 注入是主力")
    # 无指纹的纯非文件回路：主动不参与判定（宁漏不误伤），交给 watchdog
    if not str(got.get("bashLoop", "")).startswith("not-tracked"):
        fails.append(f"无指纹回路本应不参与判定，却动了手：{got.get('bashLoop')}")
    # 兜底
    if not str(got.get("tightLoop", "")).startswith("THREW@"):
        fails.append("极端紧循环没有 abort —— 兜底失效")
    if not str(got.get("multiFileCycle", "")).startswith("LOOP-BROKEN@"):
        fails.append(f"多文件轮读没有被打断：{got.get('multiFileCycle')}")
    # 长周期
    if not str(got.get("longPeriod", "")).startswith("QUARANTINED@"):
        fails.append(f"长周期回路没被抓到：{got.get('longPeriod')} —— 缺口没收窄")
    if got.get("longPeriodRule1Max", 99) >= 6:
        fails.append(f"测试用例不成立：周期 40 时判据1 读数到了 {got.get('longPeriodRule1Max')}")
    # 继续干活 + 零误伤
    if got.get("recovered") != "ok":
        fails.append(f"配合的模型换工具后仍被中止/未换成功（{got.get('recovered')}）")
    if got.get("distinct") != "ok":
        fails.append(f"200 个不同文件被误伤：{got.get('distinct')}")
    if got.get("distinctBanned"):
        fails.append("200 个不同文件却被禁用了工具")
    if got.get("benignRepeat4") != "ok":
        fails.append(f"同文件重读 4 次被误伤：{got.get('benignRepeat4')}")
    if not (isinstance(got.get("oldStreakMax"), int) and got["oldStreakMax"] < 3):
        fails.append("测试用例没复刻出真实形态（旧判据 streak 应 <3）")
    if "NOT double-checking" not in src:
        fails.append("Oxo 告警文案没增强")

    if fails:
        print("SELFTEST 失败:")
        for f in fails:
            print("  -", f)
        return 1

    print("SELFTEST 通过：")
    print(f"  · 合法迭代（同一文件反复渲染+查看、内容每轮变）**零误伤**："
          f"0 条误报告警、未禁任何工具  ← 修复「会不会导致图片读取有问题」")
    print(f"  · 循环读图（反复读同一张没变的图）注入 {got['sameImageLoopInjected']} 条提示后"
          f"第 {got['sameImageLoop'].split('@')[1]} 次禁用工具  ← 治的是这个病")
    print(f"  · 长周期回路（周期 40）判据1 读数最高仅 {got['longPeriodRule1Max']}，"
          f"被判据2 第 {got['longPeriod'].split('@')[1]} 次抓住")
    print(f"  · 配合的模型换工具后不中止、正常继续")
    print(f"  · 多文件轮读第 {got['multiFileCycle'].split('@')[1]} 次禁用 Read → 循环被打断（同一工具禁一次就够）")
    print(f"  · 兜底：极端紧循环第 {got['tightLoop'].split('@')[1]} 次收手")
    print(f"  · 无指纹的纯非文件回路主动不判定（宁漏不误伤，交给 watchdog）")
    print(f"  · 零误伤：200 个不同文件 / 同文件重读 4 次均放行")
    print(f"  · 旧判据同场景 streak 最大仅 {got['oldStreakMax']}（确认失明）")
    return 0


def main():
    p = argparse.ArgumentParser(description="zcode 循环守卫（分级退让版）")
    p.add_argument("--bundle", default=BUNDLE_DEFAULT)
    p.add_argument("--window", type=int, default=WINDOW_DEFAULT,
                   help=f"判据窗口：最近多少次调用内统计重复（默认 {WINDOW_DEFAULT}）")
    p.add_argument("--quarantine", type=int, default=QUARANTINE_DEFAULT,
                   help=f"窗口内同签名达到此数 → 本回合禁用该工具（默认 {QUARANTINE_DEFAULT}）")
    p.add_argument("--abort", type=int, default=ABORT_DEFAULT,
                   help=f"同签名占满窗口一半 → 中止（默认 {ABORT_DEFAULT}）")
    p.add_argument("--maxq", type=int, default=MAXQ_DEFAULT,
                   help=f"本回合禁用 N 个工具后还在绕 → 中止（默认 {MAXQ_DEFAULT}）")
    p.add_argument("--stale", type=int, default=STALE_DEFAULT,
                   help=f"连续 N 次调用无新签名 → 长周期回路（默认 {STALE_DEFAULT}）")
    p.add_argument("--maxwarn", type=int, default=MAXWARN_DEFAULT,
                   help=f"每回合最多注入几条纠正提示（默认 {MAXWARN_DEFAULT}）")
    p.add_argument("--check", action="store_true")
    p.add_argument("--revert", action="store_true")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not os.path.exists(args.bundle):
        print(f"找不到 bundle: {args.bundle}", file=sys.stderr)
        return 1
    if not (3 <= args.quarantine <= 1000):
        print("--quarantine 必须 >= 3（低于 3 会误伤正常重读）", file=sys.stderr)
        return 1
    if args.abort <= args.quarantine:
        print(f"--abort({args.abort}) 必须大于 --quarantine({args.quarantine})，"
              f"否则没有「先禁用、后中止」的退让空间", file=sys.stderr)
        return 1
    if args.maxwarn < 1:
        print(f"--maxwarn({args.maxwarn}) 必须 >= 1", file=sys.stderr)
        return 1
    if args.stale < 5:
        print(f"--stale({args.stale}) 必须 >= 5 —— 太小时判据2 的条件近乎恒真，"
              f"会不停乱禁工具", file=sys.stderr)
        return 1

    if args.check:
        return do_check(args.bundle)
    if args.revert:
        return revert(args.bundle)
    if args.selftest:
        return selftest(args.bundle)
    return apply_patch(args.bundle, args.window, args.quarantine, args.abort, args.maxq,
                       args.stale, args.maxwarn, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
