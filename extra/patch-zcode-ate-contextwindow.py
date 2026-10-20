#!/usr/bin/env python3
"""zcode.cjs Ate projection contextWindow patch (2026-09-23)

用户诉求: TUI 状态栏 `ctx 51% left` 错误, 真实分母应是 512K (524288)。
实测: gateway prompt=98542, 显示 51% left ⇒ 98542/200000=49% ⇒ 分母=200000。
真值: 98542/524288≈19% used ⇒ 应显示 81% left。

根因 (Ate 事件 reducer, zcode.cjs):
  1. 初值 j_r.contextWindow=2e5 (200000) 是写死默认
  2. 活跃会话从不发 SessionCreated (仅 hydrate 路径发)
  3. ModelSelected 已带 contextWindow:t.properties.contextWindow (=config 524288)
     但 Ate 根本没有 ModelSelected handler —— 被丢弃
  4. ModelComplete payload 也带 contextWindow, 但 handler 只写 contextUsed

修复 (三处, 均在 Ate handlers):
  - SessionCreated: 仅当 contextWindow 是正整数时才写, 防止 undefined 覆盖
  - 新增 ModelSelected handler: 写 payload.contextWindow
  - ModelComplete: 追加写 payload.contextWindow

不用 T3e() (别的模块作用域), 内联 typeof/Number.isInteger 检查。

用法: python3 patch-zcode-ate-contextwindow.py          # 应用(幂等)
      python3 patch-zcode-ate-contextwindow.py --revert # 还原 .bak
      python3 patch-zcode-ate-contextwindow.py --check  # 只检查不改
"""
import sys, shutil, os, subprocess

ZCODE = "/home/whu/.npm-global/lib/node_modules/zcode-app-cli/vendor/zcode.cjs"
BAK = ZCODE + ".bak-ate-ctxwindow-20260923"

POS = '(typeof {v}=="number"&&Number.isInteger({v})&&{v}>0?{v}:void 0)'

SESSION_OLD = (
    '[nt.SessionCreated]:(t,n)=>{let o=n.payload;return{...t,id:n.sessionId,mode:o.mode,'
    'planEnabled:o.planEnabled??o.mode==="plan",contextWindow:o.contextWindow,'
    'createdAt:n.timestamp,updatedAt:n.timestamp,status:"idle"}}'
)
SESSION_NEW = (
    '[nt.SessionCreated]:(t,n)=>{let o=n.payload,s='
    + POS.format(v="o.contextWindow") +
    ';return{...t,id:n.sessionId,mode:o.mode,'
    'planEnabled:o.planEnabled??o.mode==="plan",'
    '...s!==void 0?{contextWindow:s}:{},'
    'createdAt:n.timestamp,updatedAt:n.timestamp,status:"idle"}}'
)

MODEL_SELECTED_HANDLER = (
    '[nt.ModelSelected]:(t,n)=>{let o='
    + POS.format(v="n.payload?.contextWindow") +
    ';return{...t,...o!==void 0?{contextWindow:o}:{},updatedAt:n.timestamp}},'
)

COMPLETE_OLD = (
    '[nt.ModelComplete]:(t,n)=>{let o=n.payload;if(o.querySource===M7)return'
    '{...t,targetCompletionVerifications:[...t.targetCompletionVerifications,FDe(o.content)],'
    'updatedAt:n.timestamp};if(!c0s(o))return{...t,updatedAt:n.timestamp};'
    'let i=Lhe(o.usage);return{...t,...i!==void 0?{contextUsed:i}:{},updatedAt:n.timestamp}}'
)
COMPLETE_NEW = (
    '[nt.ModelComplete]:(t,n)=>{let o=n.payload;if(o.querySource===M7)return'
    '{...t,targetCompletionVerifications:[...t.targetCompletionVerifications,FDe(o.content)],'
    'updatedAt:n.timestamp};if(!c0s(o))return{...t,updatedAt:n.timestamp};'
    'let i=Lhe(o.usage),a=' + POS.format(v="o.contextWindow") +
    ';return{...t,...i!==void 0?{contextUsed:i}:{},'
    '...a!==void 0?{contextWindow:a}:{},updatedAt:n.timestamp}}'
)

MARKER = '[nt.ModelSelected]:(t,n)=>{let o=(typeof n.payload?.contextWindow=="number"'


def is_patched(s: str) -> bool:
    return MARKER in s and SESSION_NEW in s and COMPLETE_NEW in s


def main():
    if "--check" in sys.argv:
        s = open(ZCODE, encoding="utf-8", errors="replace").read()
        print("patched" if is_patched(s) else "NOT patched")
        sys.exit(0 if is_patched(s) else 1)

    if "--revert" in sys.argv:
        if os.path.exists(BAK):
            shutil.copy(BAK, ZCODE)
            print("reverted from", BAK)
        else:
            print("no backup at", BAK)
        r = subprocess.run(["node", "--check", ZCODE], capture_output=True, text=True)
        print("node --check:", "OK" if r.returncode == 0 else r.stderr)
        return

    s = open(ZCODE, encoding="utf-8", errors="replace").read()
    if is_patched(s):
        print("already patched, skip")
        return

    if not os.path.exists(BAK):
        shutil.copy(ZCODE, BAK)

    for name, old, new in (
        ("SessionCreated", SESSION_OLD, SESSION_NEW),
        ("ModelComplete", COMPLETE_OLD, COMPLETE_NEW),
    ):
        c = s.count(old)
        assert c == 1, f"{name} anchor count={c} (升级后源码变了, 需人工比对)"
        s = s.replace(old, new, 1)

    # 新增 ModelSelected handler, 插在 ModelComplete 前
    anchor = COMPLETE_NEW
    assert s.count(anchor) == 1, "ModelComplete new form not unique"
    s = s.replace(anchor, MODEL_SELECTED_HANDLER + anchor, 1)

    open(ZCODE, "w", encoding="utf-8").write(s)
    assert is_patched(s), "patch applied but marker missing"
    print("patched OK (Ate contextWindow: SessionCreated/ModelSelected/ModelComplete)")

    r = subprocess.run(["node", "--check", ZCODE], capture_output=True, text=True)
    print("node --check:", "OK" if r.returncode == 0 else r.stderr)
    if r.returncode != 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
