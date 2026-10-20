/*
 * zcode-streamfetch —— 绕开 undici 300s bodyTimeout 的传输层替换
 *
 * 背景 (2026-10-04)：
 *   TensorFold 对「超长单个工具调用」整块缓冲、完成才 flush（实测 305s）。
 *   Node 内置 fetch (undici) 默认 bodyTimeout=300s，正好差 6 秒被砍 →
 *   UND_ERR_BODY_TIMEOUT → zcode 报 "Model request failed" 并重试 5 次。
 *
 *   Node 的 fetch 只接受它**自己内部**的 undici Dispatcher（外部 undici 包
 *   的 Agent 会被 UND_ERR_INVALID_ARG 拒掉，已实测），所以无法通过
 *   dispatcher 调大 bodyTimeout。改用 node:http 自建，天生没有 body timeout。
 *
 * 用法（由 patch-zcode-streamfetch.py 注入 bundle）：
 *   globalThis.__zcodeStreamFetch(origFetch) -> wrappedFetch
 *   只接管 LLM 端点 (/chat/completions, /completions, /responses, /messages)，
 *   其余 URL、以及任何无法判定的请求形态，一律透传给 origFetch。
 *
 * 设计约束：
 *   - 必须返回**真** Response（AI SDK 依赖 .body / .json() / .text()）。
 *   - 不得引入任何超时；只有调用方的 AbortSignal 能中断。
 *   - 任何异常都要能安全降级（抛错交给调用方，或直接透传）。
 *
 * ── 思考跑飞熔断（形态④）：2026-10-04 首版 → 2026-10-09 改为「熔断-恢复」──
 *
 * 首版（已废弃）命中后**收流 + 提示文案 + finish_reason=stop**，回合就此结束。
 * 用户实测反馈：这等于「思考被腰斩后什么也不干」——会话里 197 次触发、每次
 * 都在 420s 处把正常的长思考砍掉，永远不产出。三个缺陷：
 *
 *   ① 计时起点错：brake.start 定在**收到响应头**时。180k prompt 的 prefill/排队
 *      本身就要几分钟，于是 elapsed 里大头根本不是思考 → `>8000 字符且 >420s`
 *      这条判据几乎被 prefill 时间单独触发（实测 197 次全是这一条）。
 *      → 现在从**首个 delta 到达**起算。
 *   ② 行缓冲丢失：scanSseChunk 按 "\n" 切完只处理完整行，尾部半行**没有留到
 *      下一块** → 跨 chunk 的 data: 行被静默丢计（漏报）。
 *      → 现在 state.carry 跨块续接。
 *   ③ 命中后结束回合 = 停工。正确形态是**透明重发**（移植自网关 L1）：
 *        a. 关掉深度思考：chat_template_kwargs.enable_thinking=false
 *           （引擎 server.py:697 `thinking = bool(kwargs.pop("enable_thinking", ...))`）
 *           并置 reasoning_effort="none"（EFFORTS["none"]=(False,None)，双保险；
 *           注意 apply_effort 用的是 setdefault，所以必须显式覆写）。
 *        b. 追加一条「思考停下来，开始执行」的系统消息。
 *        c. 把**同一请求重新发一份**，把新流的 id 逐行改写成原流 id 后透传 ——
 *           AI SDK 依赖稳定的 response id，换 id 会被判协议错乱（网关踩过）。
 *        d. **回灌已完成的思考**：把上一轮 reasoning 正文当作 assistant 消息带上
 *           （上限 ZCODE_REASON_BRAKE_KEEP_CHARS，默认 120000 字符，超出截断并标注）。
 *           没有这一步，重发等于让模型从零再想一遍 = 白想（用户 2026-10-09 要求）。
 *     客户端全程无感。只重发 1 次；重发后仍跑飞 → 干净收尾
 *     （finish_reason=**stop**，绝不能用 length，否则被 zcode 续写逻辑喂回循环，形态③）。
 *
 * ── 形态⑤：n-gram 周期环判据（2026-10-09 事故后补）──────────────────────
 *   事故：sess_2ea4156c 模型算叉积 forward(-1,0,0)×up(0,0,1) 时在 reasoning
 *   通道塌成 `(0*` 环，**28695 字符 / 267 秒**零工具调用零结论，一路刷屏到用户
 *   手动 Esc。上面两条判据是按**字符数**设阈的（40000 字符、或 8000 字符+420 秒），
 *   符号密集环「一眼就退化」却堆不到那个量级 → 系统性漏判（10-02 那次散文自我
 *   推翻环堆到 102k 字符才触发，掩盖了这个洞）。
 *   修法：新增判据 0 —— 直接看**思考正文尾部的周期性**（p∈1..6，≥12 次重复，
 *   ≥120 token，≥400 字符；只看尾部 6000 字符，有界开销）。实测本次真实数据在
 *   **3400 字符**处命中（旧判据要 40000）。算法与 degencap 同源，但跑在
 *   **飞行中的流**上 —— degencap 只能清历史，拦不住实时刷屏。
 *
 * env 旋钮：ZCODE_REASON_BRAKE_{MAX_CHARS,MIN_CHARS,MAX_SEC,CONTENT_GRACE,RESEND,REMINDER}
 *          ZCODE_REASON_BRAKE_RING_{PERIOD,REPEATS,TOKENS,CHARS,SCAN,FLOOR}
 */
"use strict";

const http = require("node:http");
const https = require("node:https");

// ── 熔断参数（env 可调）────────────────────────────────────────────────
function numEnv(name, dflt) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return dflt;
  const n = Number(raw);
  return Number.isFinite(n) ? n : dflt;
}

const REASON_MAX_CHARS = numEnv("ZCODE_REASON_BRAKE_MAX_CHARS", 40000);
const REASON_MIN_CHARS = numEnv("ZCODE_REASON_BRAKE_MIN_CHARS", 8000);
const REASON_MAX_SEC = numEnv("ZCODE_REASON_BRAKE_MAX_SEC", 420);
const CONTENT_GRACE_CHARS = numEnv("ZCODE_REASON_BRAKE_CONTENT_GRACE", 2000);
const RESEND_ENABLED = numEnv("ZCODE_REASON_BRAKE_RESEND", 1) !== 0;
// 熔断时**回灌给模型**的思考正文上限。思考不能白想 —— 已想出来的分析要带上，
// 否则重发等于让它从零再来一遍（用户 2026-10-09 明确要求）。
const KEEP_CHARS = numEnv("ZCODE_REASON_BRAKE_KEEP_CHARS", 120000);

// ── n-gram 周期环判据（形态⑤：符号/短环密集退化）────────────────────────
// 上面两条判据按「字符数」设阈，对**符号密集环**是系统性漏判：
// 2026-10-09 sess_2ea4156c 实测模型在算叉积 forward(-1,0,0)×up(0,0,1) 时塌成
// `(0*` 环，只堆到 28695 字符 / 267 秒 —— 两条阈值都不满足（40000 字符、
// 8000 字符且 420 秒），于是一路刷屏到用户手动 Esc。这类环「一眼就退化」的
// 字符数远低于散文自我推翻环（10-02 那次堆到 102k 字符才触发），所以必须直接
// 看**正文尾部的周期性**，而不是等它堆够字符。
// 算法与 degencap 同源（尾部分词 + p∈1..MAX_PERIOD 周期检测），区别是它跑在
// **飞行中的流**上（degencap 只能清历史）。streamfetch 在最内层，反向 require
// degencap 会成环（degencap 包在它外面），所以这里自带一份。
const RING_MAX_PERIOD = numEnv("ZCODE_REASON_BRAKE_RING_PERIOD", 6);
const RING_MIN_REPEATS = numEnv("ZCODE_REASON_BRAKE_RING_REPEATS", 12);
const RING_MIN_TOKENS = numEnv("ZCODE_REASON_BRAKE_RING_TOKENS", 120);
const RING_MIN_CHARS = numEnv("ZCODE_REASON_BRAKE_RING_CHARS", 400);
const RING_SCAN_CHARS = numEnv("ZCODE_REASON_BRAKE_RING_SCAN", 6000);
const RING_FLOOR_CHARS = numEnv("ZCODE_REASON_BRAKE_RING_FLOOR", 1200);

/**
 * 尾部周期环检测。命中返回 {period, ringTokens, ringChars}，否则 null。
 * 只看尾部 RING_SCAN_CHARS 字符（有界开销，长流不退化）。
 * 阈值三重：环要够长(≥RING_MIN_TOKENS 个 token)、重复够多次(p*RING_MIN_REPEATS)、
 * 占的字符够多(≥RING_MIN_CHARS) —— 避免把短促的合法重复（分隔线、表格）当退化。
 */
function detectRing(text) {
  if (typeof text !== "string" || text.length < RING_FLOOR_CHARS) return null;
  const tail = text.length > RING_SCAN_CHARS ? text.slice(-RING_SCAN_CHARS) : text;
  const starts = [];
  const re = /\S+/g;
  let mm;
  while ((mm = re.exec(tail)) !== null) starts.push({ at: mm.index, len: mm[0].length });
  const total = starts.length;
  if (total < RING_MIN_TOKENS) return null;
  const tok = (i) => tail.substr(starts[i].at, starts[i].len);

  let best = null;
  for (let p = 1; p <= RING_MAX_PERIOD; p++) {
    if (total < p * RING_MIN_REPEATS) continue;
    let i = total - 1 - p;
    while (i >= 0 && tok(i) === tok(i + p)) i--;
    const L0 = total - 1 - p - i; // 满足等式的 i 个数
    if (L0 <= 0) continue;
    const covered = L0 + p;
    const ringTokens = covered - (covered % p); // 对齐到环边界
    if (ringTokens < p * RING_MIN_REPEATS || ringTokens < RING_MIN_TOKENS) continue;
    if (!best || ringTokens > best.ringTokens) best = { period: p, ringTokens };
  }
  if (!best) return null;
  const ringChars = tail.length - starts[total - best.ringTokens].at;
  if (ringChars < RING_MIN_CHARS) return null;
  return { period: best.period, ringTokens: best.ringTokens, ringChars };
}

const REASON_CARRY_HEADER =
  "【以下是你在思考(reasoning)通道里已经完成的分析。直接采用其中的结论，不要重复或重述它。】";
const REASON_CARRY_TRUNC =
  "\n\n…（以上分析过长，已截断；请基于已有部分直接行动，不要重新推导。）";

const DEFAULT_REMINDER =
  "[系统提示] 思考停下来，立刻开始执行。" +
  "你已经在思考通道里连续生成很久，既没有给出结论、也没有发起任何工具调用；" +
  "继续想下去不会产生任何新的信息。现在二选一，直接做：" +
  "(1) 立刻发起下一步工具调用（读文件 / 跑命令 / 写代码），用拿到的事实推进；" +
  "(2) 用一句话给出当前结论并结束本回合。" +
  "禁止再复述或推翻已有分析。为打断循环，本回合已关闭深度思考——请直接行动。";
const REMINDER = process.env.ZCODE_REASON_BRAKE_REMINDER || DEFAULT_REMINDER;
const REMINDER_MARK = DEFAULT_REMINDER.slice(0, 12); // 幂等标记

/** 是否该熔断。elapsed 从**首个 delta** 起算（不含 prefill/排队）。 */
function shouldBrake(state) {
  // 有工具调用、或有实质正文 → 是在干活，放行
  if (state.toolCalls > 0 || state.contentChars > CONTENT_GRACE_CHARS) return null;
  const elapsedSec = state.startAt === null ? 0 : (Date.now() - state.startAt) / 1000;
  // 判据 0（形态⑤）：尾部周期性 —— 不依赖字符数/时间，符号环几百字符就能抓。
  const ring = detectRing(state.reasonText);
  if (ring) {
    return `reasoning tail is a ${ring.period}-gram ring: ${ring.ringTokens} tokens / ${ring.ringChars} chars (总 ${state.reasonChars} 字符)`;
  }
  if (state.reasonChars > REASON_MAX_CHARS) {
    return `reasoning ${state.reasonChars} chars > ${REASON_MAX_CHARS}`;
  }
  if (state.reasonChars > REASON_MIN_CHARS && elapsedSec > REASON_MAX_SEC) {
    return `reasoning ${state.reasonChars} chars in ${Math.floor(elapsedSec)}s > ${REASON_MAX_SEC}s`;
  }
  return null;
}

function newBrakeState() {
  return {
    reasonChars: 0,
    contentChars: 0,
    toolCalls: 0,
    done: false,
    finish: null,
    startAt: null, // 首个 delta 的时刻（不含 prefill）
    origId: null, // 原流的 response id，重发后改写用
    carry: "", // 跨 chunk 的半行
    fired: false,
    why: null,
    reasonText: "", // 思考正文（回灌给模型，不白想）
    reasonTruncated: false,
  };
}

/** 解析一条 SSE 行，累计 reasoning/content/tool_calls，并记住 response id。 */
function scanLine(state, line) {
  if (!line.startsWith("data:")) return;
  const payload = line.slice(5).trim();
  if (payload === "[DONE]") {
    state.done = true;
    return;
  }
  if (!payload) return;
  let j;
  try {
    j = JSON.parse(payload);
  } catch (_) {
    return; // 非 JSON 行（心跳等）
  }
  if (j && typeof j === "object" && typeof j.id === "string" && !state.origId) {
    state.origId = j.id;
  }
  const ch = j && j.choices && j.choices[0];
  if (!ch) return;
  const d = ch.delta || ch.message || {};
  const rc = (d.reasoning_content != null ? d.reasoning_content : d.reasoning) || "";
  if (typeof rc === "string" && rc.length) {
    state.reasonChars += rc.length;
    // 保留思考正文（有上限，防止超长回路把内存顶爆）
    if (state.reasonText.length < KEEP_CHARS) {
      state.reasonText += rc;
      if (state.reasonText.length > KEEP_CHARS) {
        state.reasonText = state.reasonText.slice(0, KEEP_CHARS);
        state.reasonTruncated = true;
      }
    } else {
      state.reasonTruncated = true;
    }
  }
  if (typeof d.content === "string" && d.content.length) state.contentChars += d.content.length;
  if (Array.isArray(d.tool_calls) && d.tool_calls.length) state.toolCalls += d.tool_calls.length;
  if (ch.finish_reason) state.finish = ch.finish_reason;
  if (state.startAt === null && (state.reasonChars || state.contentChars || state.toolCalls)) {
    state.startAt = Date.now();
  }
}

/** 跨 chunk 行缓冲：半行留到下一块（旧版每块独立扫描，边界半行被静默丢弃 → 漏计）。 */
function feedLines(state, text) {
  state.carry += text;
  let idx;
  while ((idx = state.carry.indexOf("\n")) !== -1) {
    const line = state.carry.slice(0, idx);
    state.carry = state.carry.slice(idx + 1);
    scanLine(state, line);
  }
  // 非 SSE 的大 body 不设上界会撑内存；超过 64KB 说明本就不含完整行，丢弃
  if (state.carry.length > 65536) state.carry = "";
}

/** 把一行里的 response id 改成 origId（重发流的 id 必须与原流一致）。 */
function rewriteIdsInLine(line, origId) {
  if (!origId || !line.startsWith("data:")) return line;
  const payload = line.slice(5).trim();
  if (!payload || payload === "[DONE]") return line;
  try {
    const j = JSON.parse(payload);
    if (j && typeof j === "object" && "id" in j) j.id = origId;
    return "data: " + JSON.stringify(j);
  } catch (_) {
    return line;
  }
}

/** 行缓冲的 id 改写器：保证被 chunk 边界切开的 id 也能正确改写。 */
function makeIdRewriter(origId) {
  let carry = "";
  return {
    push(buf) {
      const text = carry + Buffer.from(buf).toString("utf8");
      const lines = text.split("\n");
      carry = lines.pop(); // 末尾是不完整行（或空串）
      let out = "";
      for (const line of lines) out += rewriteIdsInLine(line, origId) + "\n";
      return Buffer.from(out, "utf8");
    },
    flush() {
      const line = carry;
      carry = "";
      return Buffer.from(line ? rewriteIdsInLine(line, origId) : "", "utf8");
    },
  };
}

/**
 * 从原请求体构造重发体：回灌已完成的思考 + 关思考 + 注入「停止思考,开始执行」。
 * @param {Buffer} buf 原请求体
 * @param {string} [reasonText] 上一轮思考正文（回灌，避免白想）
 * @param {boolean} [reasonTruncated] 思考正文是否被截断
 * 失败返回 null（走兜底收尾）。
 */
function buildRetryBody(buf, reasonText, reasonTruncated) {
  let body;
  try {
    body = JSON.parse(Buffer.from(buf).toString("utf8"));
  } catch (_) {
    return null;
  }
  if (!body || typeof body !== "object") return null;

  // 关思考：显式覆写（引擎 apply_effort 用 setdefault，不覆写会被原值压掉）
  const ctk = Object.assign({}, body.chat_template_kwargs || {});
  ctk.enable_thinking = false;
  body.chat_template_kwargs = ctk;
  body.reasoning_effort = "none";

  const msgs = Array.isArray(body.messages) ? body.messages.slice() : [];
  const last = msgs[msgs.length - 1];
  const already =
    last && typeof last.content === "string" && last.content.includes(REMINDER_MARK);

  if (!already) {
    // ① 先把已完成的思考当作自己的上一轮输出回灌 —— 这些分析已经想过，直接用它
    if (typeof reasonText === "string" && reasonText.length) {
      msgs.push({
        role: "assistant",
        content: REASON_CARRY_HEADER + "\n\n" + reasonText + (reasonTruncated ? REASON_CARRY_TRUNC : ""),
      });
    }
    // ② 再给指令：停止思考，开始执行
    msgs.push({ role: "system", content: REMINDER });
  }
  body.messages = msgs;

  return JSON.stringify(body);
}

/** 熔断收尾（兜底）：构造正常 SSE 结尾（说明帧 + finish stop + [DONE]）。 */
function brakeFrames(model, reasonChars, why, cid) {
  const id = cid || `chatcmpl-zsfbrake-${Date.now()}`;
  const frame = (delta, finish) =>
    `data: ${JSON.stringify({
      id,
      object: "chat.completion.chunk",
      created: Math.floor(Date.now() / 1000) | 0,
      model,
      choices: [{ index: 0, delta, finish_reason: finish }],
    })}\n`;

  const notice =
    `[zcode-streamfetch] 已中止本回合：模型在思考(reasoning)通道连续生成 ${reasonChars} 字符，` +
    `既未给出结论也未发起工具调用（${why}），且关闭思考后的重发仍未收敛。` +
    `为避免空转到输出上限、再被续写逻辑喂回循环，这里主动硬熔断。建议把任务拆小。`;
  return Buffer.from(
    frame({ content: notice }, null) + frame({}, "stop") + "data: [DONE]\n"
  );
}

/** 只接管这些路径；其余一律走原 fetch。 */
const LLM_PATH_RE = /\/(chat\/completions|completions|responses|messages)(\?|$)/;

function isLlmRequest(url, method) {
  return method === "POST" && LLM_PATH_RE.test(url.pathname);
}

function normalizeHeaders(init) {
  const out = {};
  const h = init && init.headers;
  if (!h) return out;
  if (typeof h.forEach === "function" && typeof h.get === "function") {
    h.forEach((v, k) => {
      out[k] = v;
    });
    return out;
  }
  if (Array.isArray(h)) {
    for (const pair of h) {
      if (Array.isArray(pair) && pair.length >= 2) out[String(pair[0])] = String(pair[1]);
    }
    return out;
  }
  for (const k of Object.keys(h)) {
    const v = h[k];
    if (v === undefined || v === null) continue;
    out[k] = Array.isArray(v) ? v.map(String).join(", ") : String(v);
  }
  return out;
}

function bodyToBuffer(body) {
  if (body === undefined || body === null) return null;
  if (typeof body === "string") return Buffer.from(body, "utf8");
  if (Buffer.isBuffer(body)) return body;
  if (body instanceof Uint8Array) return Buffer.from(body);
  if (body instanceof ArrayBuffer) return Buffer.from(body);
  return undefined; // 未知形态（如 ReadableStream）→ 调用方透传
}

/**
 * 用 node:http(s) 发一次请求，返回原生 Response。
 * @param {*} target
 * @param {*} init
 * @param {{allowResend?: boolean}} [opts] allowResend=false 时熔断只做兜底收尾
 * @returns {Promise<Response>}
 */
function rawFetch(target, init, opts) {
  init = init || {};
  opts = opts || {};
  const allowResend = opts.allowResend !== false;
  const url = target instanceof URL ? target : new URL(String(target));
  const mod = url.protocol === "https:" ? https : http;
  const method = String(init.method || "GET").toUpperCase();
  const headers = normalizeHeaders(init);
  const buf = bodyToBuffer(init.body);

  // 有 body 时必须显式给 Content-Length：否则 node 会退化成 chunked
  // transfer-encoding，而 TensorFold 的 Python BaseHTTPRequestHandler 不认 chunked
  // （实测 500）。同时清掉调用方可能带来的冲突头。
  if (buf !== null && buf !== undefined) {
    for (const k of Object.keys(headers)) {
      const lk = k.toLowerCase();
      if (lk === "content-length" || lk === "transfer-encoding") delete headers[k];
    }
    headers["Content-Length"] = String(buf.length);
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    let req;
    const signal = init.signal;

    const onAbort = () => {
      if (settled) return;
      settled = true;
      try {
        req && req.destroy(new Error("aborted"));
      } catch (_) {}
      const err = new Error("The operation was aborted.");
      err.name = "AbortError";
      reject(err);
    };

    if (signal) {
      if (signal.aborted) return onAbort();
      signal.addEventListener("abort", onAbort, { once: true });
    }

    req = mod.request(
      {
        protocol: url.protocol,
        hostname: url.hostname,
        port: url.port || (url.protocol === "https:" ? 443 : 80),
        path: url.pathname + url.search,
        method,
        headers,
        // 明确不设 timeout —— 这正是本 shim 存在的理由
      },
      (res) => {
        if (settled) {
          res.destroy();
          return;
        }
        settled = true;
        if (signal) signal.removeEventListener("abort", onAbort);

        const isSse = /text\/event-stream/i.test(String(res.headers["content-type"] || ""));

        const stream = new ReadableStream({
          start(controller) {
            let paused = false;
            const brake = newBrakeState();
            let brakeFired = false;
            let modelName = null;

            const getModel = () => {
              if (modelName !== null) return modelName;
              try {
                modelName = JSON.parse(Buffer.from(buf).toString("utf8")).model || "";
              } catch (_) {
                modelName = "";
              }
              return modelName;
            };

            const finishWithNotice = () => {
              try {
                controller.enqueue(
                  brakeFrames(getModel(), brake.reasonChars, brake.why, brake.origId)
                );
              } catch (_) {}
              try {
                controller.close();
              } catch (_) {}
              try {
                res.destroy();
              } catch (_) {}
            };

            // ── 熔断动作：优先透明重发（关思考 + 注入提示），失败/再跑飞才收尾 ──
            const fireBrake = (why) => {
              if (brakeFired) return;
              brakeFired = true;
              brake.why = why;
              console.error(`[zcode-streamfetch] reasoning runaway brake: ${why}`);

              if (!allowResend || !RESEND_ENABLED || buf === null || buf === undefined) {
                finishWithNotice();
                return;
              }
              const retryBody = buildRetryBody(buf, brake.reasonText, brake.reasonTruncated);
              if (!retryBody) {
                finishWithNotice();
                return;
              }
              const rheaders = Object.assign({}, headers, { "Content-Type": "application/json" });
              console.error(
                `[zcode-streamfetch] → 透明重发: origId=${brake.origId} 保留已想内容 ` +
                  `${brake.reasonText.length} 字符${brake.reasonTruncated ? "(截断)" : ""} ` +
                  `body=${retryBody.length}B url=${url.pathname}`
              );
              // 重发流本身 allowResend=false：再跑飞只干净收尾，不再套娃
              rawFetch(url, { method, headers: rheaders, body: retryBody, signal }, {
                allowResend: false,
              })
                .then(async (resp2) => {
                  console.error(
                    `[zcode-streamfetch] ← 重发响应: status=${resp2.status} ct=${resp2.headers.get("content-type")}`
                  );
                  if (resp2.status >= 400) throw new Error(`retry HTTP ${resp2.status}`);
                  try {
                    res.destroy();
                  } catch (_) {}
                  const rewriter = makeIdRewriter(brake.origId);
                  const reader = resp2.body.getReader();
                  let nChunks = 0;
                  let nBytes = 0;
                  for (;;) {
                    if (signal && signal.aborted) {
                      console.error(
                        `[zcode-streamfetch] ✗ 重发中断: 客户端已 abort (已转发 ${nChunks} 块/${nBytes}B)`
                      );
                      try {
                        await reader.cancel();
                      } catch (_) {}
                      return;
                    }
                    const { done, value } = await reader.read();
                    if (done) break;
                    const out = rewriter.push(value);
                    nChunks++;
                    if (out.length) {
                      nBytes += out.length;
                      controller.enqueue(new Uint8Array(out));
                    }
                  }
                  const tail = rewriter.flush();
                  if (tail.length) {
                    nBytes += tail.length;
                    controller.enqueue(new Uint8Array(tail));
                  }
                  console.error(
                    `[zcode-streamfetch] ✓ 重发完成: ${nChunks} 块 / ${nBytes}B 已透传; 关闭流`
                  );
                  try {
                    controller.close();
                  } catch (_) {}
                })
                .catch((e) => {
                  console.error(
                    `[zcode-streamfetch] 透明重发失败, 走兜底收尾: ${(e && e.message) || e}`
                  );
                  finishWithNotice();
                });
            };

            res.on("data", (chunk) => {
              if (brakeFired) return;
              if (isSse) {
                try {
                  feedLines(brake, chunk.toString("utf8"));
                  const why = shouldBrake(brake);
                  if (why) {
                    fireBrake(why);
                    return;
                  }
                } catch (_) {
                  /* 检查自身异常不影响传输 */
                }
              }
              controller.enqueue(new Uint8Array(chunk));
              // 简单背压：让出事件循环，避免超长响应把内存顶爆
              if (controller.desiredSize !== null && controller.desiredSize <= 0 && !paused) {
                paused = true;
                res.pause();
                queueMicrotask(() => {
                  paused = false;
                  res.resume();
                });
              }
            });
            res.on("end", () => {
              if (brakeFired) return;
              try {
                controller.close();
              } catch (_) {}
            });
            res.on("error", (e) => {
              if (brakeFired) return;
              try {
                controller.error(e);
              } catch (_) {}
            });
          },
          cancel() {
            try {
              res.destroy();
            } catch (_) {}
          },
        });

        const respHeaders = new Headers();
        for (const [k, v] of Object.entries(res.headers)) {
          if (v === undefined) continue;
          if (Array.isArray(v)) for (const one of v) respHeaders.append(k, String(one));
          else respHeaders.set(k, String(v));
        }

        let response;
        try {
          response = new Response(stream, {
            status: res.statusCode || 200,
            statusText: res.statusMessage || "",
            headers: respHeaders,
          });
        } catch (e) {
          reject(e);
          return;
        }
        resolve(response);
      }
    );

    req.on("error", (e) => {
      if (settled) return;
      settled = true;
      if (signal) signal.removeEventListener("abort", onAbort);
      reject(e);
    });

    if (buf !== null && buf !== undefined) req.write(buf);
    req.end();
  });
}

/**
 * 主入口：包装原 fetch。
 * @param {Function} origFetch
 * @returns {Function} fetch-compatible
 */
function createStreamFetch(origFetch) {
  const orig =
    typeof origFetch === "function" ? origFetch : (...a) => globalThis.fetch(...a);

  return async function zcodeStreamFetch(input, init) {
    let url = null;
    let method = "GET";
    try {
      if (input instanceof Request) {
        url = new URL(input.url);
        method = (init && init.method) || input.method || "GET";
      } else {
        url = new URL(String(input));
        method = String((init && init.method) || "GET").toUpperCase();
      }
    } catch (_) {
      return orig(input, init);
    }

    if (!isLlmRequest(url, method)) return orig(input, init);
    // Request 实例形态不接管（body/headers 语义易踩坑）→ 透传
    if (input instanceof Request) return orig(input, init);
    // 未知 body 形态（如 ReadableStream）→ 透传
    const probe = bodyToBuffer(init && init.body);
    if (probe === undefined) return orig(input, init);

    try {
      return await rawFetch(url, init, { allowResend: true });
    } catch (e) {
      // 网络层异常：交给原 fetch 再试一次，尽量保持原有错误语义
      if (e && e.name === "AbortError") throw e;
      try {
        return await orig(input, init);
      } catch (e2) {
        throw e2;
      }
    }
  };
}

module.exports = createStreamFetch;
module.exports.createStreamFetch = createStreamFetch;
module.exports._rawFetch = rawFetch;
// 测试用内部件
module.exports._detectRing = detectRing;
module.exports._shouldBrake = shouldBrake;
module.exports._newBrakeState = newBrakeState;
module.exports._scanLine = scanLine;
module.exports._feedLines = feedLines;
module.exports._buildRetryBody = buildRetryBody;
module.exports._makeIdRewriter = makeIdRewriter;
module.exports._brakeFrames = brakeFrames;
module.exports._REMINDER = REMINDER;
module.exports._limits = {
  REASON_MAX_CHARS,
  REASON_MIN_CHARS,
  REASON_MAX_SEC,
  CONTENT_GRACE_CHARS,
  RESEND_ENABLED,
  KEEP_CHARS,
};
