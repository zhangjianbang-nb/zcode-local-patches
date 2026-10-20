/*
 * zcode-degencap —— 请求层文本退化闸（截断历史消息里的重复尾巴）
 *
 * 背景 (2026-10-07 取证, sess_7bfd9f51)：
 *   模型输出退化成 "I — I — I — I — ..."（token 级交替重复，562 次、14KB），
 *   尾巴直接进了 assistant 历史，之后**每个请求都在重放这 14KB 垃圾**。
 *   现有三层防线全部覆盖不到：
 *     - 原生 modelAnomalyGuard：只认「连续严格相同」，单行内 token 级交替不命中；
 *     - loop-guard 补丁：只看工具调用签名，文本流不可见；
 *     - 10-03 的 degen 熔断器在 gateway 里 —— 直连引擎后该层不存在。
 *
 * 本 shim 在请求出网前清洗 body.messages 里的 assistant 文本：
 *   检测「同一 n-gram 循环」的退化尾巴（"I — I" 模式 = 2-token 环），
 *   把尾巴截断并追加一句占位说明（模型知道这里被截断，不会继续模仿）。
 *   只清洗**历史消息**（除最后一条 assistant 外），不动本轮正在生成的流 ——
 *   流式侧的实时拦截属于另一层，不在这里做。
 *
 * 检测算法（对每条 assistant 文本）：
 *   取尾部若干 token（按空白切词）。对 period p ∈ [1..6]，检查尾部
 *   是否有 ≥ MIN_REPEATS 次连续重复的 p-gram。命中则回溯找到重复段的
 *   真实起点，从起点截断，替换为 "[degenerate repetition removed]"。
 *
 * 误伤防护：
 *   - 只处理 role==="assistant" 的消息（用户消息/system 原样）；
 *   - 最后一条消息不动（可能是本轮上下文的关键内容）；
 *   - 阈值要求 ≥8 次重复 —— 正常文本里 "..." 8 连环几乎不存在；
 *   - 只在命中时才重建 body，未命中零改动零开销。
 *
 * 组装顺序：与 imagecap 同款，包在 streamfetch 外层。
 * 降级约束：任何异常都原样透传，补丁坏了不能比没有补丁更糟。
 */
"use strict";

const MIN_REPEATS = 8;   // 尾部 n-gram 至少连续重复多少次才算退化
const MAX_PERIOD = 6;    // n-gram 环最长多少个"词"
const TAIL_TOKENS = 400; // 只检查每条消息尾部这么多词（省时且足够）
const EVICT_NOTE = "\n\n[degenerate repetition removed by degencap]";

// 2026-10-10 补形态⑥（sess_2ea4156c 取证）：结构级 token 错乱不是周期环，
// 环检测覆盖不到 —— 5 次 Write 产出 "if False else" / ", 0 0)" 残渣无人拦。
// 这类残渣有稳定签名：合法 Python/JS 里 "if False else"（合法场景仅 lambda/生成器
// 极少语境）与 "N N)"（逗号后缺逗号直接跟数字）在正常代码中几乎不存在，
// 而退化输出里高频出现。对 assistant 历史消息中的代码内容打占位标记，
// 阻止退化模式被后续请求重放、被模型当范例继续模仿。
const SYNTAX_SCRAP_PATTERNS = [
  [/\bif False else\b/g, "if-False-else"],
  [/,\s*\d+\s+\d+\)/g, "comma-space-number-number-paren"],
];

function detectSyntaxScrap(text) {
  if (typeof text !== "string" || text.length < 200) return null;
  let hits = 0;
  for (const [re] of SYNTAX_SCRAP_PATTERNS) {
    const m = text.match(re);
    if (m) hits += m.length;
  }
  // 阈值 3：正常代码不会出现 3 处以上此类残渣；退化输出动辄 5-10 处
  return hits >= 3 ? hits : null;
}

function tokenize(s) {
  return s.split(/\s+/).filter(Boolean);
}

/**
 * 返回截断描述；未退化返回 null。
 * 两段式：先用尾部 TAIL_TOKENS 窗口判定「是否存在 p-周期环」(p = 1..MAX_PERIOD)，
 * 判定命中后在整个文本的 token 序列上回溯，找出环的真实起点（把窗口外的
 * 重复也一并算进去），截断点 = 环起点。窗口内的数数只用来判定，不用来定位。
 */
function detectTruncation(text) {
  if (typeof text !== "string" || text.length < 200) return null;
  const all = tokenize(text);
  if (all.length < MIN_REPEATS) return null;
  const total = all.length;

  let best = null; // { period, suffixLen }
  for (let p = 1; p <= MAX_PERIOD; p++) {
    if (total < p * MIN_REPEATS) continue;
    // 从末尾往前：要求 all[i] === all[i+p] 对 i = total-1-p 一路连续成立到 i+1 处
    let i = total - 1 - p;
    while (i >= 0 && all[i] === all[i + p]) i--;
    const L0 = total - 1 - p - i; // 满足等式的 i 个数
    if (L0 <= 0) continue;
    const covered = L0 + p;       // 构成 p 周期的 token 数
    const ringTokens = covered - (covered % p); // 对齐到环边界
    if (ringTokens < p * MIN_REPEATS) continue;
    if (!best || ringTokens > best.suffixLen) best = { period: p, suffixLen: ringTokens };
  }
  if (!best) return null;

  const starts = [];
  const re = /\S+/g;
  let mm;
  while ((mm = re.exec(text)) !== null) starts.push(mm.index);
  const keepWords = total - best.suffixLen;
  if (keepWords <= 0 || keepWords >= starts.length) return null;
  return { cutAt: starts[keepWords], removedWords: best.suffixLen, period: best.period };
}

/** 清洗一条消息文本。命中返回新文本，未命中返回 null。
 *  2026-10-10 扩展：环检测之外加语法残渣检测（形态⑥）——
 *  assistant 历史里带 ≥3 处语法残渣的整段替换为占位说明，
 *  不让退化输出被后续请求重放、被模型当范例模仿。 */
function cleanText(t) {
  const det = detectTruncation(t);
  if (det) {
    return t.slice(0, det.cutAt).replace(/\s+$/, "") + EVICT_NOTE;
  }
  const scrap = detectSyntaxScrap(t);
  if (scrap) {
    return t.replace(/\s+$/, "") +
      `\n\n[syntax-scrap detected (${scrap} hits): degenerate code fragments removed by degencap]`;
  }
  return null;
}

/** 清洗一条 assistant 消息（原地）。返回清洗的文本段数。
 *  覆盖三种形态：content 为 string / content 数组里的 text 与 reasoning
 *  part / reasoning_content 字段 —— sess_7bfd9f51 实测退化在 type=reasoning。 */
function cleanOneMessage(m) {
  let n = 0;
  if (typeof m.content === "string") {
    const nt = cleanText(m.content);
    if (nt !== null) { m.content = nt; n++; }
  } else if (Array.isArray(m.content)) {
    for (const part of m.content) {
      if (part && typeof part === "object" && typeof part.text === "string" &&
          (part.type === "text" || part.type === "reasoning")) {
        const nt = cleanText(part.text);
        if (nt !== null) { part.text = nt; n++; }
      }
    }
  }
  if (typeof m.reasoning_content === "string") {
    const nt = cleanText(m.reasoning_content);
    if (nt !== null) { m.reasoning_content = nt; n++; }
  }
  return n;
}

/** 对 messages 数组清洗 assistant 文本。原地修改。返回清洗条数。 */
function cleanMessages(messages) {
  let n = 0;
  for (let mi = 0; mi < messages.length - 1; mi++) { // 最后一条不动
    const m = messages[mi];
    if (!m || typeof m !== "object" || m.role !== "assistant") continue;
    n += cleanOneMessage(m);
  }
  return n;
}

/** 主入口：包装 fetch。所有非可处理形态一律原样透传。 */
function createDegenCapFetch(origFetch) {
  const orig = typeof origFetch === "function" ? origFetch : (...a) => globalThis.fetch(...a);
  return async function degenCapFetch(input, init) {
    try {
      if (input instanceof Request || !init || init.body === undefined || init.body === null) {
        return orig(input, init);
      }
      let parsed;
      const bodyStr = typeof init.body === "string" ? init.body :
        (Buffer.isBuffer(init.body) || init.body instanceof Uint8Array) ? Buffer.from(init.body).toString("utf8") : null;
      if (!bodyStr) return orig(input, init);
      try { parsed = JSON.parse(bodyStr); } catch (_) { return orig(input, init); }
      if (!parsed || !Array.isArray(parsed.messages)) return orig(input, init);

      const cleaned = cleanMessages(parsed.messages);
      if (cleaned <= 0) return orig(input, init); // 未命中：原字节透传

      const newBody = Buffer.from(JSON.stringify(parsed), "utf8");
      const newInit = { ...init, body: newBody };
      const h = newInit.headers;
      if (h && typeof h === "object" && !(h instanceof Headers)) {
        for (const k of Object.keys(h)) {
          if (k.toLowerCase() === "content-length" || k.toLowerCase() === "transfer-encoding") delete h[k];
        }
        h["Content-Length"] = String(newBody.length);
      }
      return orig(input, newInit);
    } catch (_) {
      return orig(input, init);
    }
  };
}

module.exports = createDegenCapFetch;
module.exports.createDegenCapFetch = createDegenCapFetch;
module.exports._detectTruncation = detectTruncation;
module.exports._cleanMessages = cleanMessages;
module.exports._cleanText = cleanText;
