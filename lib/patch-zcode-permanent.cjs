#!/usr/bin/env node
/*
 * patch-zcode-permanent.cjs —— 纯 JS 安装器：把请求层补丁**内联**进 zcode bundle
 *
 * 取代原来的三个安装器：
 *   patch-zcode-streamfetch.py   (Python, prelude 只 require 外部 shim)
 *   patch-zcode-image-cap.cjs    (prelude require 外部 shim)
 *   patch-zcode-degencap.cjs     (prelude require 外部 shim)
 *
 * 现在的做法：把 ~/.zcode/patches-lib 下三个 shim 的**源码本身**注入 bundle
 * （不再 require 外部文件）。装完之后：
 *   - 补丁属于 zcode 自己，外部 shim 文件被删/被挪也不影响；
 *   - 没有 Python 参与；
 *   - 仍由 zcode-patches.sh + path unit 在 npm 升级后自动重放（升级覆盖是 npm 的
 *     行为，任何"磁盘补丁"都躲不开，除非改启动器——那属于机器级持久化，未做）。
 *
 * 注入顺序（与旧 prelude 完全一致）：
 *   __zcSf = degencap(imagecap(streamfetch(原始 fetch)))
 * streamfetch 必须最内层：imagecap 改完 body 后，由它重算 Content-Length。
 *
 * 用法：
 *   node patch-zcode-permanent.cjs            # 打补丁（幂等；旧 prelude 会被升级成内联版）
 *   node patch-zcode-permanent.cjs --check    # 0=已就位；1=缺失/形态不对
 *   node patch-zcode-permanent.cjs --revert   # 移除 prelude 并还原 transport
 *   ZCODE_CJS=/path/to/zcode.cjs node ...     # 指定目标
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

// 可移植：npm 全局根 + 本文件所在目录（shims 与安装器放在一起）
function npmRoot() {
  try {
    return execFileSync('npm', ['root', '-g'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch {
    return '';
  }
}

const BUNDLE =
  process.env.ZCODE_CJS ||
  path.join(
    npmRoot() || path.join(os.homedir(), '.npm-global', 'lib', 'node_modules'),
    'zcode-app-cli',
    'vendor',
    'zcode.cjs'
  );
const LIB = process.env.ZCODE_PATCH_LIB || __dirname;

const MARK = '[zcode-permanent]';
const SHIMS = [
  ['streamfetch', 'zcode-streamfetch.cjs'], // 最内层：原始 fetch → 无 bodyTimeout 的 node:http
  ['imagecap', 'zcode-imagecap.cjs'],       // 外层：裁掉超限的旧图
  ['degencap', 'zcode-degencap.cjs'],       // 最外层：截断退化周期环
];

const TRANSPORT_RE =
  /function ([A-Za-z0-9_$]+)\(e\)\{let t=e\.fetch\?\?globalThis\.fetch\.bind\(globalThis\);/;
const TRANSPORT_WRAPPED_RE =
  /function ([A-Za-z0-9_$]+)\(e\)\{let t=globalThis\.__zcSf\(e\.fetch\?\?globalThis\.fetch\.bind\(globalThis\)\);/;
const LEGACY_RE = /globalThis\.__zcSf=require\(/;
const HEAD_END = '/*\n * Modified by zcode-app-cli';

function readShim(file) {
  for (const p of [path.join(LIB, file), path.join(LIB, 'shims', file)]) {
    if (fs.existsSync(p)) return fs.readFileSync(p, 'utf8');
  }
  throw new Error(`缺少 shim: ${path.join(LIB, file)}（或 ${path.join(LIB, 'shims', file)}）`);
}

/** 把三个 shim 串成一段可内联的 JS（每个 shim 在自己独立的 CJS 作用域里求值）。 */
function inlinePrelude() {
  const parts = [`// ${MARK} 请求层补丁内联（streamfetch → imagecap → degencap），不依赖外部文件\n`];
  SHIMS.forEach(([name, file], i) => {
    const src = readShim(file);
    const m = `__zcp_m${i}`;
    const prev = `__zcp_p${i}`;
    if (i === 0) {
      parts.push(
        `try{${m}=(function(){var module={exports:{}};var exports=module.exports;\n` +
          `${src}\n` +
          `return module.exports;})();globalThis.__zcSf=typeof ${m}==="function"?${m}:function(o){return o};}` +
          `catch(e){globalThis.__zcSf=function(o){return o};try{console.error("[zcode-permanent] ${name} 加载失败，已降级:",e&&e.message)}catch(_){}}\n`
      );
    } else {
      parts.push(
        `try{${m}=(function(){var module={exports:{}};var exports=module.exports;\n` +
          `${src}\n` +
          `return module.exports;})();${prev}=globalThis.__zcSf;` +
          `globalThis.__zcSf=function(o){return ${m}(${prev}?${prev}(o):o)};}` +
          `catch(e){try{console.error("[zcode-permanent] ${name} 加载失败，已跳过:",e&&e.message)}catch(_){}}\n`
      );
    }
  });
  // 变量声明放在最前面，避免隐式全局
  const decl = `var ${SHIMS.map((_, i) => `__zcp_m${i},__zcp_p${i}`).join(',')};\n`;
  return decl + parts.join('');
}

function stripLegacyHead(src, start) {
  // 旧 prelude 从 start 一直延伸到上游注释块之前
  const end = src.indexOf(HEAD_END, start);
  if (end < 0) return null;
  return src.slice(0, start) + src.slice(end);
}

function applyTo(src) {
  if (!src.startsWith('#!')) throw new Error('第 1 行不是预期 shebang，拒绝改动');
  const nl = src.indexOf('\n');
  const shebang = src.slice(0, nl + 1);

  if (src.includes(MARK)) {
    return { src, changed: false, note: 'already inlined' };
  }

  // 形态 A：旧 prelude（require 外部 shim）→ 整段替换成内联版
  const head = src.slice(0, Math.max(0, src.indexOf(HEAD_END)));
  if (LEGACY_RE.test(head)) {
    let out = stripLegacyHead(src, nl + 1);
    if (!out) throw new Error('找不到上游注释块边界，拒绝改动');
    out = shebang + inlinePrelude() + out.slice(nl + 1);
    // transport 在旧补丁里已经包过；若没有则补上
    if (!TRANSPORT_WRAPPED_RE.test(out)) {
      const m = TRANSPORT_RE.exec(out);
      if (m) {
        out =
          out.slice(0, m.index) +
          `function ${m[1]}(e){let t=globalThis.__zcSf(e.fetch??globalThis.fetch.bind(globalThis));` +
          out.slice(m.index + m[0].length);
      }
    }
    return { src: out, changed: true, note: 'legacy prelude -> inlined' };
  }

  // 形态 B：干净 bundle → 首次安装
  const m = TRANSPORT_RE.exec(src);
  if (!m) throw new Error('找不到 transport 锚点（上游函数形状变了），拒绝改动');
  let out = shebang + inlinePrelude() + src.slice(nl + 1);
  const m2 = TRANSPORT_RE.exec(out);
  if (!m2) throw new Error('注入后锚点消失，拒绝写盘');
  out =
    out.slice(0, m2.index) +
    `function ${m2[1]}(e){let t=globalThis.__zcSf(e.fetch??globalThis.fetch.bind(globalThis));` +
    out.slice(m2.index + m2[0].length);
  return { src: out, changed: true, note: 'installed (inlined)' };
}

function revertFrom(src) {
  const start = src.indexOf('// ' + MARK);
  if (start < 0) return { src, changed: false, note: 'no patch' };
  const end = src.indexOf(HEAD_END, start);
  if (end < 0) throw new Error('找不到上游注释块边界，拒绝还原');
  const nl = src.indexOf('\n');
  let out = src.slice(0, nl + 1) + src.slice(end);
  const m = TRANSPORT_WRAPPED_RE.exec(out);
  if (m) {
    out =
      out.slice(0, m.index) +
      `function ${m[1]}(e){let t=e.fetch??globalThis.fetch.bind(globalThis);` +
      out.slice(m.index + m[0].length);
  }
  return { src: out, changed: true, note: 'reverted' };
}

function isInstalled(src) {
  return (
    src.includes(MARK) &&
    TRANSPORT_WRAPPED_RE.test(src) &&
    !LEGACY_RE.test(src.slice(0, Math.max(0, src.indexOf(HEAD_END))))
  );
}

function nodeCheck(file) {
  try {
    execFileSync(process.execPath, ['--check', file], { stdio: 'pipe' });
    return true;
  } catch (e) {
    return String((e.stderr || e.stdout || '').toString()).slice(0, 600) || 'node --check failed';
  }
}

function main() {
  const args = process.argv.slice(2);
  if (!fs.existsSync(BUNDLE)) {
    console.error(`找不到 bundle: ${BUNDLE}`);
    process.exit(2);
  }
  const src = fs.readFileSync(BUNDLE, 'utf8');

  if (args.includes('--check')) {
    const ok = isInstalled(src);
    console.log(`bundle   = ${BUNDLE}`);
    console.log(`inlined  = ${src.includes(MARK)}`);
    console.log(`transport= ${TRANSPORT_WRAPPED_RE.test(src) ? 'wrapped' : 'pristine'}`);
    console.log(`legacy   = ${LEGACY_RE.test(src.slice(0, Math.max(0, src.indexOf(HEAD_END))))}`);
    console.log(ok ? '✅ 已就位（内联，无外部 require）' : '❌ 未就位');
    process.exit(ok ? 0 : 1);
  }

  const { src: out, changed, note } =
    args.includes('--revert') ? revertFrom(src) : applyTo(src);

  if (!changed) {
    console.log(`无需改动：${note}`);
    process.exit(0);
  }

  const tmp = BUNDLE + '.tmp-permanent.cjs';
  fs.writeFileSync(tmp, out);
  const check = nodeCheck(tmp);
  if (check !== true) {
    fs.unlinkSync(tmp);
    console.error(`❌ node --check 失败，未写盘：\n${check}`);
    process.exit(1);
  }
  const st = fs.statSync(BUNDLE);
  fs.renameSync(tmp, BUNDLE);
  try { fs.utimesSync(BUNDLE, st.atime, new Date()); } catch (_) {}
  console.log(`✅ ${note}；node --check 通过（${src.length} → ${out.length} bytes）`);
  console.log('   ⚠️ 只对新启动的 zcode 进程生效');
}

main();
