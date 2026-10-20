/*
 * zcode-imagecap —— 请求层图片上限（最近 N 张图，其余替换为占位符）
 *
 * 背景 (2026-10-07 取证, sess_4005e89e)：
 *   引擎 TensorFold 曾对单请求图片数硬上限 8（GLM53_TF_VISION_MAX_IMAGES，
 *   实测 9 图 → 400 "9 images in one request: at most 8"）。
 *   zcode 正常路径每请求只带 1 张图（bodyMessagesKind=delta，引擎有状态），
 *   但**截断续写/全量重放路径**（bodyMessagesKind=full）会把历史里每一步
 *   Read 的截图全部带上 —— sess_4005e89e 第 31 号请求带 9 图 76 消息，
 *   400 invalid_request 不可重试 → Turn failed，任务当场死亡。
 *
 * 本 shim 在请求出网前统一裁剪：
 *   - 统计 body.messages 里全部 image_url 图片部件；
 *   - 总数 ≤ N 直接放行（正常增量请求零改动、零开销）；
 *   - 超限时把最旧的图替换为 {type:"text", text:"[image evicted: ...]"}
 *     占位符（与旧 gateway 滚动窗口同款约定），**最后一条消息里的图
 *     永不驱逐**——除非最后一条消息自己就超限（极端情况，从旧到新逐出）；
 *   - N 默认 100000（2026-10-10 用户令：引擎侧也已放开，两侧同为"不限"，
 *     即正常情况永不驱逐），环境变量 ZCODE_IMAGE_CAP 可覆盖（下限 1，上限 = ENGINE_HARD_LIMIT）。
 *
 * ⚠️ 放开后的新失效模式：全量重放路径不再被张数拦住，但引擎的 1M 上下文检查仍在
 *   （每张图最少 16 行）。若 zcode 对图片 token 的估算偏低而 autoCompact 不触发
 *   （见记忆 zcode-glm53-context-stall-fix），重放会长到 context_length_exceeded —
 *   即 400 从"at most 8 images"变成"context_length_exceeded"。需要保护时用
 *   ZCODE_IMAGE_CAP 显式设一个有限值，不必改本文件。
 *
 * 组装顺序（由 bundle prelude 保证）：
 *   globalThis.__zcSf = imagecap( streamfetch( origFetch ) )
 *   imagecap 在**外层**改 body，streamfetch 在内层按新 body 重算
 *   Content-Length —— 顺序反了 TensorFold 会因 chunked 直接 500。
 *
 * 降级约束（与 streamfetch 同款）：
 *   - body 不是 JSON 对象 / 无 messages 数组 / 解析异常 → 原样透传；
 *   - Request 实例、ReadableStream 等未知形态 → 原样透传；
 *   - 任何异常都不得抛出 —— 补丁坏了不能比没有补丁更糟。
 */
"use strict";

const ENGINE_HARD_LIMIT = 100000;

function capLimit() {
  const raw = Number(process.env.ZCODE_IMAGE_CAP);
  if (Number.isFinite(raw) && raw >= 1) return Math.min(Math.floor(raw), ENGINE_HARD_LIMIT);
  return 100000; // 2026-10-10: 引擎 GLM53_TF_VISION_MAX_IMAGES 已放开(8 -> 100000), 两侧同步为"不限"
}

const EVICT_MARK = "[image evicted: oldest image removed to fit per-request image cap]";

const EVICT_MARK_VIDEO = "[video evicted: oldest video removed to fit per-request video cap]";

/** 提取 JSON body；不可处理形态返回 null。 */
function bodyToJson(body) {
  if (body === undefined || body === null) return null;
  let text;
  if (typeof body === "string") text = body;
  else if (Buffer.isBuffer(body) || body instanceof Uint8Array) text = Buffer.from(body).toString("utf8");
  else return null;
  if (!text || text.length > 64 * 1024 * 1024) return null; // 64MB 防御
  try {
    const obj = JSON.parse(text);
    return obj && typeof obj === "object" ? obj : null;
  } catch (_) {
    return null;
  }
}

/** 判断一个 content part 是否是图片（openai image_url 形态）。 */
function isImagePart(part) {
  return !!part && typeof part === "object" && part.type === "image_url" &&
    !!part.image_url && typeof part.image_url === "object" &&
    typeof part.image_url.url === "string";
}

/** 判断一个 content part 是否是视频（openai video_url 形态）。 */
function isVideoPart(part) {
  return !!part && typeof part === "object" && part.type === "video_url" &&
    !!part.video_url && typeof part.video_url === "object" &&
    typeof part.video_url.url === "string";
}

function videoCapLimit() {
  const raw = Number(process.env.ZCODE_VIDEO_CAP);
  if (Number.isFinite(raw) && raw >= 0) return Math.floor(raw);
  return 100000; // 2026-10-10: 与引擎侧同步为"不限"; 注意视频 token 极重, 单请求实际受 1M 上下文约束
}

/**
 * 对 messages 数组执行「保留最近 N 个<某类媒体部件>」。原地修改。返回驱逐个数。
 * matcher: isImagePart / isVideoPart；mark: 驱逐后写入的占位文本。
 * 规则：最后一条消息的该类部件永不驱逐（它是模型当下正在看的），
 *       除非全员逐出后仍超限，才从旧到新逐最后一条消息里的。
 */
function capPartsInMessages(messages, n, matcher, mark) {
  if (n < 0) n = 0;
  const locs = [];
  for (let mi = 0; mi < messages.length; mi++) {
    const m = messages[mi];
    if (!m || typeof m !== "object") continue;
    const c = m.content;
    if (!Array.isArray(c)) continue;
    for (let pi = 0; pi < c.length; pi++) {
      if (matcher(c[pi])) locs.push({ mi, pi });
    }
  }
  if (locs.length <= n) return 0;

  const lastMsgIdx = messages.length - 1;
  // 逐出候选：从旧到新；最后一条消息的部件排后面（保到最后）
  const evictOrder = locs
    .map((l, i) => ({ ...l, order: i }))
    .sort((a, b) => (a.mi === lastMsgIdx) - (b.mi === lastMsgIdx) || a.order - b.order);

  let excess = locs.length - n;
  let evicted = 0;
  for (const l of evictOrder) {
    if (excess <= 0) break;
    const m = messages[l.mi];
    if (!m || !Array.isArray(m.content)) continue;
    const part = m.content[l.pi];
    if (!matcher(part)) continue; // 已被逐过
    m.content[l.pi] = { type: "text", text: mark };
    evicted++;
    excess--;
  }
  return evicted;
}

/** 主入口：包装 fetch。所有非可处理形态一律原样透传。 */
function createImageCapFetch(origFetch) {
  const orig = typeof origFetch === "function" ? origFetch : (...a) => globalThis.fetch(...a);
  return async function imageCapFetch(input, init) {
    try {
      // Request 实例/无 init.body → 无法安全改写，透传
      if (input instanceof Request || !init || init.body === undefined || init.body === null) {
        return orig(input, init);
      }
      const parsed = bodyToJson(init.body);
      if (!parsed || !Array.isArray(parsed.messages)) return orig(input, init);

      const n = capLimit();
      const evImages = capPartsInMessages(parsed.messages, n, isImagePart, EVICT_MARK);
      const evVideos = capPartsInMessages(parsed.messages, videoCapLimit(), isVideoPart, EVICT_MARK_VIDEO);
      if (evImages + evVideos <= 0) return orig(input, init); // 未超限：body 原字节透传

      const newBody = Buffer.from(JSON.stringify(parsed), "utf8");
      const newInit = { ...init, body: newBody };
      // 双保险：显式修正 Content-Length（streamfetch 内层也会重算）
      const h = newInit.headers;
      if (h && typeof h === "object" && !(h instanceof Headers)) {
        const keys = Object.keys(h);
        for (const k of keys) {
          if (k.toLowerCase() === "content-length" || k.toLowerCase() === "transfer-encoding") delete h[k];
        }
        h["Content-Length"] = String(newBody.length);
      }
      return orig(input, newInit);
    } catch (_) {
      return orig(input, init); // 任何异常 → 原样放行，绝不比没补丁更糟
    }
  };
}

module.exports = createImageCapFetch;
module.exports.createImageCapFetch = createImageCapFetch;
module.exports._capLimit = capLimit;
module.exports._videoCapLimit = videoCapLimit;
module.exports._capPartsInMessages = capPartsInMessages;
module.exports._EVICT_MARK = EVICT_MARK;
module.exports._EVICT_MARK_VIDEO = EVICT_MARK_VIDEO;
