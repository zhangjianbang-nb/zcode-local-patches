#!/usr/bin/env python3
"""zcode.cjs 斜杠路径误判补丁 —— 绝对路径当 slash 命令（2026-09-26）

背景: TUI 里贴一条绝对路径让模型看图
    › /Users/zhangjianbang/Desktop/Screenshot\ 2026-09-26\ at\ 10.54.08.png ，你看看
zcode 回:
    Unknown command: /users/zhangjianbang/desktop/screenshot. Available commands: ...

根因: parseSlashCommand(B3e) 只看"是不是以 / 开头", 不看后面像不像命令名。
macOS/Linux 绝对路径天生以 / 开头 → 被切成 rawName=users/zhangjianbang/...
（首空白截断 + 全小写）→ 命中 type:"unknown" → TUI/headless 都走 $3e 的
unknown 分支报 Unknown command。headless 的 Wnc 门也是同样结果（unknown 且
不是自定义命令 → Wnc=true → Gnc → $3e → 报错），不是只有 TUI 有病。

修法（保守, 只放行"像路径"的输入）:
  1. 新增 zcodePathLikePrompt(text): 以 / 开头 + 满足任一条
       a) fs.existsSync 命中某个空白前缀（原样 + 反斜杠转义空格还原两套候选）
       b) 首个空白 token 里除引导 / 外还有 /   （多段路径 /Users/x/Desktop/Screenshot）
       c) 首 token 以 .ext 结尾
       d) 整行像 "绝对路径.ext 后面跟空白"
  2. $3e 的 unknown 分支: 先走原有自定义命令 IQo（自定义命令优先级不变），
     查不到再判 pathLike —— 是路径就走正常 submitPrompt（跟 !s 分支同一条路），
     否则保留原来的 Unknown command 报错（命令打错字的反馈不丢）。
  3. 附件拦截行同步放行 pathLike 文本（拖文件时输入框里的绝对路径不再被
     "Image attachments are only supported for normal prompts." 挡掉）。

为什么放在 IQo **之后**而不是解析后就清空 s:
  自定义命令名由文件路径段用 ':' 拼成（z3s: relative(x).split(/[\\/]+/).join(":")）
  → 名字里永远没有 '/'，但可以带 '.'（foo.png.md → foo.png）；
  而 /tmp 之类真实存在的目录会撞 existsSync。放 IQo 后面 = 自定义命令永远先赢。

语法约束: unknown 块里已有 `let u`，不能再 let u，故用 g（块级遮蔽无害）。
require("node:fs") 包 try/catch，取不到 fs 时退化到 b/c/d 语法规则 —— 用户那条
输入靠 b 就能修好。

用法（Linux / macOS 通用，自动找 npm 全局装的 zcode.cjs）:
  python3 patch-zcode-slash-path.py                # 应用（幂等）
  python3 patch-zcode-slash-path.py --check        # 查现状, 不改
  python3 patch-zcode-slash-path.py --revert       # 还原 .bak
  python3 patch-zcode-slash-path.py --file PATH    # 指定目标文件
  python3 patch-zcode-slash-path.py --selftest     # 只测解析/判定逻辑
"""
import os
import re
import shutil
import subprocess
import sys

MARKER = "/*zcode-slash-path-fix*/"
BAK_SUFFIX = ".bak-slash-path-20260926"

# ---- 1) 帮助函数: 插在 parseSlashCommand 前面 ----
# ★ 2026-10-20：minifier 名会变（B3e → Zae），按函数体形状匹配。
HELPERS_ANCHOR_RE = re.compile(
    r'function (?P<fn>[A-Za-z0-9_$]+)\(e\)\{if\(!e\.startsWith\("/"\)\)return null;')
HELPERS_TEXT = MARKER + r"""
function zcodePathLikePrompt(e){let t=typeof e=="string"?e.trim():"";if(!t.startsWith("/"))return!1;let n=t.search(/\s/),o=n===-1?t:t.slice(0,n);if(o.length<2)return!1;try{let f=require("node:fs"),g=[t,t.replace(/\\ /g," ")],c=[];for(let l of g){let u=l.split(/\s+/),a=[];for(let m=u.length-1;m>=0;m--){a.unshift(u[m]);let d=a.join(" ");if(d.length>=2&&d.length<=4096&&c.length<80)c.push(d)}}for(let l of c)if(f.existsSync(l))return!0}catch{}return o.indexOf("/")>0?!0:/\.[A-Za-z0-9]{1,8}$/.test(o)||/^\/\S*\/.*\.[A-Za-z0-9]{1,8}(?=\s|$)/.test(t)}
"""

# ---- 2) $3e: 附件拦截 + unknown 分支改成"自定义命令 → 路径 → 报错" ----
# 3.15.3-36/-37 的 minifier 名（2026-10-20 对齐）：
#   s(解析结果) → i, IQo→spi, _2n→P4n, r2n→h4n, wTt→oAt, y2n→D4n(e)
#   x2n(登录检查)→j4n, pei(登录提示)→Kpi, dN(提交+格式化)→MN
BRANCH_OLD = (
    'if(a)return{mode:e.getMode?.(),response:"Image attachments are only supported for normal prompts."};'
    'if(i.type==="unknown"){let u=await spi(i.rawName,i.args,e,n);if(u)return await P4n(e,o.text,i),u;'
    "let f=await h4n(e);return{mode:e.getMode?.(),response:`Unknown command: /${i.rawName}. Available commands: ${oAt(D4n(e),f)}.`}}"
)
BRANCH_NEW = (
    'if(a&&!(i.type==="unknown"&&zcodePathLikePrompt(o.text)))return{mode:e.getMode?.(),response:"Image attachments are only supported for normal prompts."};'
    'if(i.type==="unknown"){let u=await spi(i.rawName,i.args,e,n);if(u)return await P4n(e,o.text,i),u;'
    "if(zcodePathLikePrompt(o.text)){if(await j4n(e))return{loginRequired:!0,mode:e.getMode?.(),response:Kpi(e.getLocale?.())};"
    "let g=await e.getApp();return MN(await g.submitPrompt(t,n),e,g)}"
    "let f=await h4n(e);return{mode:e.getMode?.(),response:`Unknown command: /${i.rawName}. Available commands: ${oAt(D4n(e),f)}.`}}"
)


# --------------------------------------------------------------------------
def resolve_target(cli_file=None):
    if cli_file:
        return cli_file
    if os.environ.get("ZCODE_CJS"):
        return os.environ["ZCODE_CJS"]
    cands = [
        "/home/whu/.npm-global/lib/node_modules/zcode-app-cli/vendor/zcode.cjs",
        os.path.expanduser("~/.npm-global/lib/node_modules/zcode-app-cli/vendor/zcode.cjs"),
        "/usr/local/lib/node_modules/zcode-app-cli/vendor/zcode.cjs",
        "/opt/homebrew/lib/node_modules/zcode-app-cli/vendor/zcode.cjs",
        os.path.expanduser("~/.nvm/versions/node/current/lib/node_modules/zcode-app-cli/vendor/zcode.cjs"),
    ]
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20)
        if root.returncode == 0 and root.stdout.strip():
            cands.insert(0, os.path.join(root.stdout.strip(), "zcode-app-cli", "vendor", "zcode.cjs"))
    except Exception:
        pass
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0]


def read(path):
    return open(path, encoding="utf-8", errors="replace").read()


def node_check(path):
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "OK")


def state(s):
    return {
        "helper": MARKER in s and "function zcodePathLikePrompt(" in s,
        "branch_new": BRANCH_NEW in s,
        "branch_old": BRANCH_OLD in s,
    }


def do_check(path):
    s = read(path)
    st = state(s)
    print(f"file           = {path}")
    print(f"helper         = {'YES' if st['helper'] else 'no'}")
    print(f"branch_patched = {'YES' if st['branch_new'] else 'no'}")
    print(f"branch_pristine= {'YES' if st['branch_old'] else 'no'}")
    if st["helper"] and st["branch_new"]:
        ok, msg = node_check(path)
        print(f"node --check   = {'OK' if ok else msg.strip()}")
        print("状态           = 已打补丁")
        sys.exit(0 if ok else 1)
    if st["branch_old"] and not st["helper"]:
        print("状态           = 未打补丁（原始 3.14.3-27 病灶还在）")
        sys.exit(1)
    print("状态           = 半补丁/源码已变，需人工比对")
    sys.exit(1)


def do_revert(path):
    bak = path + BAK_SUFFIX
    if os.path.exists(bak):
        shutil.copy(bak, path)
        print("reverted from", bak)
    else:
        print("no backup at", bak)
        return
    ok, msg = node_check(path)
    print("node --check:", "OK" if ok else msg.strip())


def do_apply(path):
    s = read(path)
    st = state(s)
    if st["helper"] and st["branch_new"]:
        print("已打过补丁, skip (幂等)")
        ok, msg = node_check(path)
        print("node --check:", "OK" if ok else msg.strip())
        sys.exit(0 if ok else 1)
    if st["branch_new"] or st["helper"]:
        print("已有部分补丁痕迹但不完整, 停手人工比对 (可先 --revert)")
        sys.exit(1)
    _hm = HELPERS_ANCHOR_RE.search(s)
    if _hm is None or len(HELPERS_ANCHOR_RE.findall(s)) != 1:
        print(f"HELPERS_ANCHOR 命中 {len(HELPERS_ANCHOR_RE.findall(s))} 次(需=1), 升级后源码变了")
        sys.exit(1)
    if s.count(BRANCH_OLD) != 1:
        print(f"BRANCH_OLD 命中 {s.count(BRANCH_OLD)} 次(需=1), 升级后源码变了")
        sys.exit(1)

    bak = path + BAK_SUFFIX
    if not os.path.exists(bak):
        shutil.copy(path, bak)
        print("backup ->", bak)

    s2 = s[:_hm.start()] + HELPERS_TEXT + s[_hm.start():]
    s2 = s2.replace(BRANCH_OLD, BRANCH_NEW, 1)
    assert MARKER in s2 and s2.count("zcodePathLikePrompt") >= 3
    open(path, "w", encoding="utf-8").write(s2)
    print(f"patched OK: +zcodePathLikePrompt, unknown 分支改走 submitPrompt")

    ok, msg = node_check(path)
    print("node --check:", "OK" if ok else msg.strip())
    if not ok:
        sys.exit(1)
    back = read(path)
    st = state(back)
    assert st["helper"] and st["branch_new"] and not st["branch_old"], st
    print("回读校验: helper ✓ branch ✓")


# --------------------------------------------------------------------------
# 语义自检: 把补进包里的 zcodePathLikePrompt 抠出来真跑（不依赖 zcode 运行时）
SEM_JS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const i = src.indexOf('function zcodePathLikePrompt(');
if (i < 0) { console.log('helper not found'); process.exit(1); }
// 帮助函数是单行, 直接切到行尾（不用花括号配平, 免得被正则/字符串里的括号坑）
const fnSrc = src.slice(i, src.indexOf('\n', i));
const pathLike = new Function('require', 'return (' + fnSrc + ')')(require);

let bad = 0;
function t(name, text, want, files) {
  const made = [];
  (files || []).forEach(p => { fs.mkdirSync(require('path').dirname(p), {recursive:true}); fs.writeFileSync(p, 'x'); made.push(p); });
  const got = pathLike(text);
  made.forEach(p => { try { fs.unlinkSync(p); } catch {} });
  const ok = got === want;
  if (!ok) bad++;
  console.log((ok ? 'ok  ' : 'FAIL') + ' ' + name + ' -> ' + got + ' (want ' + want + ')');
}

const USER_ESCAPED = '/Users/zhangjianbang/Desktop/Screenshot\\ 2026-09-26\\ at\\ 10.54.08.png ，你看看';
const USER_PLAIN   = '/Users/zhangjianbang/Desktop/Screenshot 2026-09-26 at 10.54.08.png ，你看看';

t('user-pasted (escaped spaces)', USER_ESCAPED, true);
t('user-pasted (plain spaces)',   USER_PLAIN,   true);
t('truncated rawName-ish',        '/Users/zhangjianbang/Desktop/Screenshot', true);
t('typo /hlep',                   '/hlep', false);
t('typo /comapct',                '/comapct', false);
t('short /s',                     '/s', false);
t('two-seg no ext',               '/home/whu', true);
t('ext no slash',                 '/report.pdf 你看看', true);
t('nested custom style /a:b',     '/foo:bar', false);
t('trailing slash typo /help/',   '/help/', true);
t('non-slash text',               '看看这个 /Users/x/y.png', false);
t('tilde path',                   '~/Desktop/a.png 你看看', false);
t('relative path',                './foo/bar.png 你看看', false);
t('existsSync deep',              '/home/whu/workspace/ai_workspace/patch-zcode-vno-buffer.py 你看看', true);
t('existsSync with spaces',       '/tmp/zcode slash path test/a b.png 你好', true,
  ['/tmp/zcode slash path test/a b.png']);
t('d ext only',                   '/x/y.tar.gz', true);
t('bare root name',               '/tmp', true);
console.log('semantics:', bad === 0 ? 'ALL PASS' : bad + ' FAILED');
process.exit(bad ? 1 : 0);
"""


def selftest(target):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(SEM_JS)
        js = f.name
    try:
        r = subprocess.run(["node", js, target], capture_output=True, text=True, timeout=60)
        print(r.stdout, end="")
        if r.stderr.strip():
            print("stderr:", r.stderr.strip())
        sys.exit(r.returncode)
    finally:
        os.unlink(js)


def main():
    if "--selftest" in sys.argv:
        selftest(resolve_target(_arg("--file")))
        return
    if "--check" in sys.argv:
        do_check(resolve_target(_arg("--file")))
        return
    if "--revert" in sys.argv:
        do_revert(resolve_target(_arg("--file")))
        return
    do_apply(resolve_target(_arg("--file")))


def _arg(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


if __name__ == "__main__":
    main()
