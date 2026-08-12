"use strict";
// 生图Agent · 离线本地代理（local-proxy）
// 运行在桌面端 Electron 主进程内，监听 127.0.0.1:8765。
// 作用：把云端 proxy.py 的「多服务商生图 / 竞品分析 / 抓取 / 提示词」转发逻辑搬到本地，
//       买家用自己的 API Key（或 Pollinations 免 Key），卖家零成本、工具离线可用（真·买断）。
// 前端 standalone.html 在 file:// 下会自动把 /api/* 指向 http://localhost:8765（见其 PROXY_URL 等定义）。
const http = require("http");
const https = require("https");
const crypto = require("crypto");
const { URL } = require("url");

const PORT = 8765;
const HOST = "127.0.0.1";
const ZHIPU_API = "https://open.bigmodel.cn/api/paas/v4/chat/completions";

// ───────────────────────── 服务商列表（与云端 proxy.py PROVIDERS 一致） ─────────────────────────
const PROVIDERS = [
  { id: "pollinations", name: "Pollinations（免 Key 免费）", needsKey: false,
    desc: "完全免费、免注册、免 Key，直接出图。适合快速试效果；仅文生图，图生图能力弱，且不稳定（无 SLA）。",
    getKey: "无需 Key" },
  { id: "gemini", name: "Gemini（Google 免费层）", needsKey: true,
    desc: "Google AI Studio 免费层（Nano Banana），每日约 500 张，支持图生图。需去 aistudio.google.com 拿 Key。免费层带隐形水印、商业用途受限。",
    getKey: "https://aistudio.google.com/apikey" },
  { id: "openai", name: "OpenAI（gpt-image-2）", needsKey: true,
    desc: "OpenAI 官方 gpt-image-2，新号有约 $5 试用金，支持图生图/文生图。需能访问 api.openai.com。",
    getKey: "https://platform.openai.com/api-keys" },
  { id: "redfox", name: "redfox.hk（gpt-image-2 转发）", needsKey: true,
    desc: "redfox.hk 转发 gpt-image-2，国内访问友好，需注册 Key（现多为付费）。",
    getKey: "https://redfox.hk/settings/api-keys?source=skillhub" },
  { id: "seedream", name: "Seedream 5.0（字节火山）", needsKey: true,
    desc: "字节 Seedream 5.0（火山方舟），官网只有 Pro 和 Lite 两种。Pro 质量高但额度紧；Lite 额度宽松，但要求图片总像素不低于 3686400（约 2048×2048）。",
    getKey: "https://console.volcengine.com/ark/region:cn-beijing/apikey",
    models: [
      { id: "doubao-seedream-5-0-lite-260128", name: "Lite版（额度宽松，默认）" },
      { id: "doubao-seedream-5-0-pro-260628", name: "Pro版（质量更高，额度紧）" }
    ] },
  { id: "qwen", name: "千问生图（阿里百炼）", needsKey: true,
    desc: "阿里千问生图（百炼/DashScope）。3.0 系支持文生图+图生图/参考图，付费：标准版 0.18元/张、旗舰 Pro 0.25元/张起；老版 plus/max 便宜、新用户开通百炼有免费额度。需阿里云百炼 API Key。",
    getKey: "https://bailian.console.aliyun.com",
    models: [
      { id: "qwen-image-3.0", name: "3.0 标准版（0.18元/张，默认）" },
      { id: "qwen-image-3.0-pro", name: "3.0 旗舰 Pro（0.25元/张，排版/画质更强）" },
      { id: "qwen-image-plus", name: "老版 plus（便宜，新用户有免费额度）" },
      { id: "qwen-image-max", name: "老版 max（画质高，免费额度少）" }
    ] },
  { id: "nano", name: "Nano Banana 2（Gemini 3.1 Flash）", needsKey: true,
    desc: "谷歌 Nano Banana 2（Gemini 3.1 Flash Image）家族：标准约$0.067/张、Lite 极速约$0.034/张、Pro 旗舰约$0.09/张。需 AI Studio Key。",
    getKey: "https://aistudio.google.com/apikey",
    models: [
      { id: "gemini-3.1-flash-image", name: "Nano Banana 2 标准（约$0.067/张，默认）" },
      { id: "gemini-3.1-flash-lite-image", name: "Nano Banana 2 Lite（约$0.034/张，极速）" },
      { id: "gemini-3-pro-image", name: "Nano Banana Pro（约$0.09/张，画质顶配）" }
    ] },
  { id: "custom", name: "自部署 / 自定义端点", needsKey: true,
    desc: "对接你自己部署的模型（如本地 ComfyUI、自建 OpenAI 兼容服务、或任意充值付费的兼容端点）。需填 Base URL。",
    getKey: "取决于你的部署；留空或填对应鉴权 Token" }
];

// ───────────────────────── 底层 HTTP 工具 ─────────────────────────
function httpReq(opts) {
  // opts: {url, method, headers, body(Buffer|string), timeout}
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(opts.url); } catch (e) { return reject(new Error("URL 非法：" + opts.url)); }
    const lib = u.protocol === "https:" ? https : http;
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body != null && headers["Content-Length"] == null && headers["content-length"] == null) {
      headers["Content-Length"] = Buffer.byteLength(opts.body);
    }
    const req = lib.request({
      protocol: u.protocol, host: u.hostname, port: u.port || (u.protocol === "https:" ? 443 : 80),
      path: u.pathname + u.search, method: opts.method || "GET", headers,
      timeout: opts.timeout || 120000
    }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(new Error("请求超时")); });
    if (opts.body != null) req.write(opts.body);
    req.end();
  });
}

function buildMultipart(fields, files) {
  const boundary = "----WBLocalBoundary" + crypto.randomBytes(12).toString("hex");
  const parts = [];
  for (const [k, v] of Object.entries(fields || {})) {
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${k}"\r\n\r\n${v == null ? "" : v}\r\n`));
  }
  for (const f of files || []) {
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${f.field}"; filename="${f.filename}"\r\nContent-Type: ${f.mime}\r\n\r\n`));
    parts.push(f.data);
    parts.push(Buffer.from("\r\n"));
  }
  parts.push(Buffer.from(`--${boundary}--\r\n`));
  return { body: Buffer.concat(parts), contentType: `multipart/form-data; boundary=${boundary}` };
}

function downloadDataUrl(url, timeout) {
  return httpReq({ url, method: "GET", timeout: timeout || 120000 })
    .then((r) => {
      const ct = (r.headers["content-type"] || "image/jpeg").split(";")[0];
      return "data:" + ct + ";base64," + r.body.toString("base64");
    });
}

function b64ToRaw(b64) {
  if (!b64) return null;
  if (b64.indexOf(",") !== -1) b64 = b64.split(",", 2)[1];
  try { return Buffer.from(b64, "base64"); } catch (e) { return null; }
}
function detectFmt(buf) {
  if (!buf) return ["png", "image/png"];
  if (buf[0] === 0x89 && buf[1] === 0x50) return ["png", "image/png"];
  if (buf[0] === 0xff && buf[1] === 0xd8) return ["jpeg", "image/jpeg"];
  if (buf[0] === 0x52 && buf[1] === 0x49 && buf[2] === 0x46 && buf[3] === 0x46 && buf[8] === 0x57 && buf[9] === 0x45 && buf[10] === 0x42 && buf[11] === 0x50) return ["webp", "image/webp"];
  return ["png", "image/png"];
}

function localizeError(msg) {
  if (!msg) return msg;
  const m = String(msg);
  if (m.indexOf("image size must be at least 3686400 pixels") !== -1)
    return "你选的尺寸太小：Seedream Lite 要求图片总像素不低于 3686400（例如 2048×2048、1664×2496），请在内容面板选更大画幅。";
  if (m.indexOf("The parameter `size` specified in the request is not valid") !== -1)
    return "图片尺寸参数不符合模型要求，请换更大的画幅（如 2048×2048 或 1664×2496）。";
  if (m.indexOf("inference limit") !== -1 && m.indexOf("Safe Experience Mode") !== -1)
    return "该模型当前额度已用完或被「安全体验模式」暂停。请切换 Pro/Lite 版本，或去火山方舟控制台关闭安全体验模式/开通付费额度。";
  if (m.indexOf("inference limit") !== -1)
    return "该模型当前额度已用完，请切换其他模型版本或去控制台开通更多额度。";
  if (m.indexOf("Model Activation") !== -1 || m.indexOf("Safe Experience Mode") !== -1)
    return "模型未激活或被安全体验模式限制，请去火山方舟控制台激活模型或关闭安全体验模式。";
  if (m.indexOf("Billing hard limit has been reached") !== -1 || m.indexOf("exceeded your current quota") !== -1 || m.indexOf("insufficient_quota") !== -1)
    return "你的 OpenAI 账号余额不足或免费额度已用完，请充值或换其他服务商。";
  if (m.indexOf("Invalid API Key") !== -1 || m.indexOf("Incorrect API key") !== -1)
    return "API Key 无效，请检查是否复制正确。";
  if (m.indexOf("content_policy_violation") !== -1 || m.indexOf("safety system") !== -1)
    return "提示词触发了内容安全过滤，请修改提示词后重试。";
  if (m.indexOf("API key not valid") !== -1)
    return "Gemini API Key 无效，请检查是否复制正确。";
  return m;
}

// ───────────────────────── 智谱 GLM（竞品分析 / 提示词） ─────────────────────────
async function callZhipu(key, model, messages, timeout) {
  if (!key) return [null, "未配置智谱 API Key（请在设置里填写「智谱 API Key」，用于竞品分析与提示词生成）"];
  let r;
  try {
    r = await httpReq({
      url: ZHIPU_API, method: "POST",
      headers: { "Authorization": "Bearer " + key, "Content-Type": "application/json" },
      body: JSON.stringify({ model, messages, temperature: 0.6, max_tokens: 1024 }),
      timeout: timeout || 70000
    });
  } catch (e) { return [null, "调用智谱 API 失败：" + e.message]; }
  const text = r.body.toString("utf-8");
  if (r.status !== 200) return [null, localizeError("智谱 API 返回 " + r.status + "：" + text.slice(0, 300))];
  try {
    const j = JSON.parse(text);
    return [j.choices[0].message.content, null];
  } catch (e) { return [null, "解析智谱返回失败：" + e.message]; }
}
function extractJson(text) {
  if (!text) return null;
  let s = text.trim();
  if (s.startsWith("```")) {
    const lines = s.split("\n");
    if (lines[0].startsWith("```")) lines.shift();
    if (lines[lines.length - 1].startsWith("```")) lines.pop();
    s = lines.join("\n").trim();
  }
  try { return JSON.parse(s); } catch (e) {}
  const a = s.indexOf("{"); const b = s.lastIndexOf("}");
  if (a !== -1 && b !== -1 && b > a) {
    try { return JSON.parse(s.slice(a, b + 1)); } catch (e) { return null; }
  }
  return null;
}
function asList(v) {
  if (Array.isArray(v)) {
    const out = [];
    for (const x of v) {
      if (typeof x === "string") { const s = x.trim(); if (s) out.push(s); }
      else if (x && typeof x === "object") { const s = Object.keys(x).map(k => `${k}：${x[k]}`).filter(([_, val]) => val != null && val !== "").join("；"); if (s) out.push(s); }
      else { const s = String(x).trim(); if (s) out.push(s); }
    }
    return out;
  }
  if (typeof v === "string" && v.trim()) return [v.trim()];
  return [];
}

// ───────────────────────── 生图：各服务商 ─────────────────────────
async function genPollinations(prompt, size, n) {
  try {
    let w = "1024", h = "1536";
    try { [w, h] = (size || "1024x1536").split("x"); } catch (e) {}
    const safe = encodeURIComponent((prompt || "").slice(0, 400));
    const count = Math.max(1, Math.min(4, parseInt(n, 10) || 1));
    const out = [];
    for (let i = 0; i < count; i++) {
      const u = `https://image.pollinations.ai/prompt/${safe}?width=${w}&height=${h}&nologo=true`;
      const r = await httpReq({ url: u, method: "GET", timeout: 120000 });
      out.push("data:image/jpeg;base64," + r.body.toString("base64"));
    }
    return [out, null];
  } catch (e) { return [null, "Pollinations 生图失败：" + e.message]; }
}

async function genGemini(key, images, prompt, size, n, model) {
  if (!key) return [null, "缺少 Gemini/Nano Banana API Key"];
  model = model || "gemini-2.5-flash-image";
  const count = Math.max(1, Math.min(4, parseInt(n, 10) || 1));
  const parts = [];
  for (const im of (images || [])) {
    const raw = b64ToRaw(im); if (!raw) continue;
    const [, mime] = detectFmt(raw);
    parts.push({ inline_data: { mime_type: mime, data: raw.toString("base64") } });
  }
  parts.push({ text: prompt });
  const ratio = geminiRatio(size);
  const payload = { contents: [{ parts }], generationConfig: { responseModalities: ["IMAGE"], imageConfig: { aspectRatio: ratio } } };
  try {
    const r = await httpReq({ url: `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`, method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeout: 240000 });
    const j = JSON.parse(r.body.toString("utf-8"));
    if (j.error) return [null, "Gemini 错误：" + (j.error.message || JSON.stringify(j.error))];
    const ps = (j.candidates[0].content.parts) || [];
    const out = [];
    for (const p of ps) { if (p.inline_data) { out.push(`data:${p.inline_data.mime_type || "image/png"};base64,${p.inline_data.data}`); if (out.length >= count) break; } }
    if (!out.length) return [null, "Gemini 未返回图片（可能触发安全过滤或提示词被拒）"];
    return [out, null];
  } catch (e) { return [null, "Gemini 请求失败（超时/网络）：" + e.message]; }
}
function geminiRatio(size) {
  let r = 0.75;
  try { const [w, h] = (size || "1024x1536").split("x").map(Number); r = w / h; } catch (e) {}
  const map = [["1:1", 1.0], ["3:4", 0.75], ["4:3", 1.3333], ["2:3", 0.6667], ["3:2", 1.5]];
  for (const [t, v] of map) if (Math.abs(r - v) < 0.05) return t;
  return "3:4";
}

async function genOpenai(key, images, prompt, size, n) {
  if (!key) return [null, "缺少 OpenAI API Key"];
  const count = Math.max(1, Math.min(4, parseInt(n, 10) || 1));
  try {
    if (images && images.length) {
      const files = [];
      for (let i = 0; i < images.length; i++) {
        const raw = b64ToRaw(images[i]); if (!raw) continue;
        const [fmt, mime] = detectFmt(raw);
        files.push({ field: "image", filename: `ref${i}.${fmt}`, data: raw, mime });
      }
      if (!files.length) return [null, "白底产品图/参考图解码失败"];
      const { body, contentType } = buildMultipart({ model: "gpt-image-2", prompt, n: String(count), size: size || "1024x1536" }, files);
      const r = await httpReq({ url: "https://api.openai.com/v1/images/edits", method: "POST", headers: { "Authorization": "Bearer " + key }, body, timeout: 240000 });
      return openaiParse(r);
    } else {
      const r = await httpReq({ url: "https://api.openai.com/v1/images/generations", method: "POST", headers: { "Authorization": "Bearer " + key, "Content-Type": "application/json" }, body: JSON.stringify({ model: "gpt-image-2", prompt, n: count, size: size || "1024x1536", quality: "high" }), timeout: 240000 });
      return openaiParse(r);
    }
  } catch (e) { return [null, "OpenAI 请求失败（超时/网络）：" + e.message]; }
}
async function openaiParse(r) {
  let j; try { j = JSON.parse(r.body.toString("utf-8")); } catch (e) { return [null, "OpenAI 返回非 JSON"]; }
  if (j.error) return [null, "OpenAI 错误：" + (j.error.message || JSON.stringify(j.error))];
  const out = [];
  for (const it of (j.data || [])) {
    if (it.b64_json) out.push("data:image/png;base64," + it.b64_json);
    else if (it.url) { try { out.push(await downloadDataUrl(it.url)); } catch (e) {} }
  }
  if (!out.length) return [null, "OpenAI 未返回图片"];
  return [out, null];
}

async function genRedfox(key, images, prompt, fidelity, size, quality, n) {
  if (!key) return [null, "缺少 redfox API Key"];
  const imageB64 = (images || [null])[0];
  if (!imageB64) return [null, "缺少白底产品图（redfox 为图生图，需上传底图）"];
  const raw = b64ToRaw(imageB64); if (!raw) return [null, "图片解码失败"];
  const [fmt, mime] = detectFmt(raw);
  try {
    const up = await httpReq({ url: "https://redfox.hk/story/api/parseWork/imageGen/uploadImage", method: "POST", headers: { "X-API-KEY": key }, body: buildMultipart({ format: fmt }, [{ field: "file", filename: `product.${fmt}`, data: raw, mime }]).body, timeout: 60000 });
    const upj = JSON.parse(up.body.toString("utf-8"));
    if (!String(upj.code || "").startsWith("2")) return [null, "上传失败(" + upj.code + ")：" + (upj.msg || "")];
    const imageUrl = (upj.data || {}).imageUrl;
    if (!imageUrl) return [null, "上传成功但未返回图片地址"];
    const params = { modelName: "gpt-image-2", n: String(Math.max(1, Math.min(10, parseInt(n, 10) || 1))), size: size || "1024x1536", quality: quality || "high", outputFormat: "png", background: "auto", outputCompression: 0 };
    if (fidelity) params.inputFidelity = fidelity;
    const sj = await httpReq({ url: "https://redfox.hk/story/api/parseWork/imageGen/submitSkill", method: "POST", headers: { "X-API-KEY": key, "Content-Type": "application/json" }, body: JSON.stringify({ prompt, source: "GPT image2-SkillHub", operation: "edit", images: [{ url: imageUrl }], parameters: params }), timeout: 30000 });
    const sjj = JSON.parse(sj.body.toString("utf-8"));
    if (!String(sjj.code || "").startsWith("2")) return [null, "提交失败(" + sjj.code + ")：" + (sjj.msg || "")];
    const taskId = (sjj.data || {}).taskId;
    if (!taskId) return [null, "接口未返回 taskId"];
    for (let i = 0; i < 40; i++) {
      await new Promise(res => setTimeout(res, 3000));
      const rr = await httpReq({ url: "https://redfox.hk/story/api/parseWork/imageGen/result", method: "POST", headers: { "X-API-KEY": key, "Content-Type": "application/json" }, body: JSON.stringify({ taskId }), timeout: 15000 });
      const rj = JSON.parse(rr.body.toString("utf-8"));
      if (!String(rj.code || "").startsWith("2")) return [null, "查询结果失败：" + (rj.msg || "")];
      const st = (rj.data || {}).status;
      if (st === "success") {
        const paths = (rj.data || {}).imagePaths || [];
        if (!paths.length) return [null, "生图成功但未返回图片"];
        const out = [];
        for (const p of paths.slice(0, Math.max(1, Math.min(4, parseInt(n, 10) || 1)))) { try { out.push(await downloadDataUrl(p)); } catch (e) {} }
        if (!out.length) return [null, "下载生成图失败"];
        return [out, null];
      } else if (st === "failed") return [null, "生图失败：" + ((rj.data || {}).failReason || "未知原因")];
    }
    return [null, "等待超时（约 120 秒）"];
  } catch (e) { return [null, "redfox 请求失败：" + e.message]; }
}

async function genCustom(key, images, prompt, size, n, endpoint) {
  if (!endpoint) return [null, "自定义端点需填写 Base URL"];
  const count = Math.max(1, Math.min(4, parseInt(n, 10) || 1));
  try {
    if (images && images.length) {
      const files = [];
      for (let i = 0; i < images.length; i++) { const raw = b64ToRaw(images[i]); if (!raw) continue; const [fmt, mime] = detectFmt(raw); files.push({ field: "image", filename: `ref${i}.${fmt}`, data: raw, mime }); }
      if (!files.length) return [null, "白底产品图/参考图解码失败"];
      const { body, contentType } = buildMultipart({ prompt, n: String(count), size: size || "1024x1536" }, files);
      const headers = { "Authorization": "Bearer " + key }; if (!key) delete headers["Authorization"];
      const r = await httpReq({ url: endpoint, method: "POST", headers, body, timeout: 240000 });
      return openaiParse(r);
    } else {
      const headers = { "Authorization": "Bearer " + key, "Content-Type": "application/json" }; if (!key) { delete headers["Authorization"]; }
      const r = await httpReq({ url: endpoint, method: "POST", headers, body: JSON.stringify({ prompt, n: count, size: size || "1024x1536" }), timeout: 240000 });
      return openaiParse(r);
    }
  } catch (e) { return [null, "自定义端点请求失败（超时/网络）：" + e.message]; }
}

function seedreamValidSize(size, model) {
  let minA, maxA;
  if (model === "doubao-seedream-5-0-pro-260628") { minA = 921600; maxA = 4624220; }
  else { minA = 3686400; maxA = 16777216; }
  let w = 1024, h = 1536;
  try { [w, h] = (size || "1024x1536").split("x").map(Number); } catch (e) {}
  if (w <= 0 || h <= 0) { w = 1024; h = 1536; }
  const area = w * h;
  let scale = 1;
  if (area < minA) scale = Math.sqrt(minA / area);
  else if (area > maxA) scale = Math.sqrt(maxA / area);
  if (scale === 1) return `${w}x${h}`;
  let nw = Math.round(w * scale), nh = Math.round(h * scale);
  if (nw * nh < minA) { nw = Math.floor(Math.sqrt(minA * (w / h))) + 1; nh = Math.floor(Math.sqrt(minA * (h / w))) + 1; }
  if (nw * nh > maxA) { nw = Math.floor(Math.sqrt(maxA * (w / h))); nh = Math.floor(Math.sqrt(maxA * (h / w))); }
  return `${Math.max(16, nw)}x${Math.max(16, nh)}`;
}
async function genSeedream(key, images, prompt, size, n, model) {
  if (!key) return [null, localizeError("缺少火山方舟 ARK API Key")];
  if (!prompt) return [null, localizeError("缺少提示词")];
  model = model || "doubao-seedream-5-0-lite-260128";
  const count = Math.max(1, Math.min(4, parseInt(n, 10) || 1));
  const headers = { "Authorization": "Bearer " + key, "Content-Type": "application/json" };
  try {
    const out = [];
    for (let i = 0; i < count; i++) {
      const payload = { model, prompt, size: seedreamValidSize(size, model), output_format: "png", response_format: "b64_json", watermark: false };
      if (images && images.length) payload.image = images.length > 1 ? images : images[0];
      const r = await httpReq({ url: "https://ark.cn-beijing.volces.com/api/v3/images/generations", method: "POST", headers, body: JSON.stringify(payload), timeout: 240000 });
      let j; try { j = JSON.parse(r.body.toString("utf-8")); } catch (e) { return [null, "Seedream 返回非 JSON（可能鉴权失败或网络不通）：HTTP " + r.status]; }
      const items = j.data || [];
      if (!items.length) {
        if (j.error) { const er = j.error; return [null, "Seedream 错误：" + (er.message || JSON.stringify(er))]; }
        if (j.message) return [null, "Seedream 错误(" + (j.code || "") + ")：" + j.message];
        return [null, "Seedream 未返回图片（可能 Key 无效/额度不足/提示词被拒）"];
      }
      let got = false;
      for (const it of items) {
        if (it.b64_json) { out.push("data:image/png;base64," + it.b64_json); got = true; }
        else if (it.url) { try { out.push(await downloadDataUrl(it.url)); got = true; } catch (e) {} }
      }
      if (!got) return [null, "Seedream 未返回图片数据"];
    }
    if (!out.length) return [null, "Seedream 未返回图片"];
    return [out, null];
  } catch (e) { return [null, "Seedream 请求失败：" + e.message]; }
}

async function genQwen(key, images, prompt, size, n, model) {
  if (!key) return [null, "缺少阿里云百炼 API Key"];
  model = model || "qwen-image-3.0";
  const count = Math.max(1, Math.min(4, parseInt(n, 10) || 1));
  const dsize = (size || "1024x1536").replace("x", "*");
  const headers = { "Authorization": "Bearer " + key, "Content-Type": "application/json" };
  const base = "https://dashscope.aliyuncs.com/api/v1/services/aigc";
  const isV3 = model.indexOf("qwen-image-3.0") === 0;
  const raws = (images || []).map(b64ToRaw).filter(Boolean);
  try {
    if (isV3) {
      const content = [];
      for (const raw of raws) { const [, mime] = detectFmt(raw); content.push({ image: `data:${mime};base64,${raw.toString("base64")}` }); }
      content.push({ text: prompt });
      const payload = { model, input: { messages: [{ role: "user", content }] }, parameters: { size: dsize, n: count, prompt_extend: true } };
      const r = await httpReq({ url: base + "/multimodal-generation/generation", method: "POST", headers, body: JSON.stringify(payload), timeout: 240000 });
      const j = JSON.parse(r.body.toString("utf-8"));
      let taskId = (j.output || {}).task_id;
      if (taskId) { for (let i = 0; i < 40; i++) { await new Promise(res => setTimeout(res, 3000)); const rr = await httpReq({ url: "https://dashscope.aliyuncs.com/api/v1/tasks/" + taskId, method: "GET", headers, timeout: 15000 }); j = JSON.parse(rr.body.toString("utf-8")); const st = (j.output || {}).task_status; if (st === "SUCCEEDED" || st === "FAILED") break; } }
      return qwenExtract(j, key, model);
    }
    if (raws.length) return [null, "该千问版本仅支持文生图；图生图/参考图请把版本换成「3.0 标准版」或「3.0 旗舰 Pro」。"];
    const payload = { model, input: { prompt }, parameters: { size: dsize, n: count, watermark: false } };
    const r = await httpReq({ url: base + "/text2image/image-synthesis", method: "POST", headers, body: JSON.stringify(payload), timeout: 60000 });
    const j = JSON.parse(r.body.toString("utf-8"));
    const taskId = (j.output || {}).task_id;
    if (!taskId) return qwenExtract(j, key, model);
    for (let i = 0; i < 40; i++) { await new Promise(res => setTimeout(res, 3000)); const rr = await httpReq({ url: "https://dashscope.aliyuncs.com/api/v1/tasks/" + taskId, method: "GET", headers, timeout: 15000 }); j = JSON.parse(rr.body.toString("utf-8")); const st = (j.output || {}).task_status; if (st === "SUCCEEDED") break; if (st === "FAILED") return qwenExtract(j, key, model); }
    return qwenExtract(j, key, model);
  } catch (e) { return [null, "千问生图请求失败：" + e.message]; }
}
function qwenErr(j, model) {
  const code = String((j.code) || ((j.output || {}).code) || "");
  const msg = String((j.message) || ((j.output || {}).message) || "") + " " + String(j.error || "");
  if (msg.indexOf("InvalidApiKey") !== -1 || code.indexOf("401") !== -1) return "千问 API Key 无效，请检查是否复制正确（阿里云百炼控制台 → API-KEY）。";
  if (msg.indexOf("Throttling") !== -1 || msg.indexOf("FlowExceedLimit") !== -1 || msg.toLowerCase().indexOf("quota") !== -1 || msg.indexOf("额度") !== -1) return "千问调用被限流或额度不足（免费额度用完或需开通付费），请到百炼控制台查看用量。";
  if (msg.indexOf("Model.AccessDenied") !== -1 || msg.indexOf("NotActivated") !== -1 || msg.indexOf("未开通") !== -1) return "该千问模型未在你账号开通，请到百炼控制台开通对应模型服务。";
  if (msg.toLowerCase().indexOf("model does not exist") !== -1 || msg.indexOf("ModelNotFound") !== -1) return "该千问模型 ID 不存在或未对你开放（若用 3.0 标准版报错，可换「3.0 旗舰 Pro」版本再试）。";
  return "千问生图失败(" + code + ")：" + (msg.trim() || "未知错误");
}
async function qwenExtract(j, key, model) {
  const out = (j.output) || {};
  if (out.task_status === "FAILED") return [null, qwenErr(j, model)];
  if (!out && (j.code || j.message || j.error)) return [null, qwenErr(j, model)];
  const urls = [];
  for (const it of (out.results || [])) if (it.url) urls.push(it.url);
  for (const ch of (out.choices || [])) { const c = ((ch.message || {}).content) || []; for (const it of c) if (it && it.image) urls.push(it.image); }
  const extra = out.images || []; if (typeof extra === "string") urls.push(extra); else urls.push(...extra);
  if (!urls.length) return [null, "千问生图未返回图片（可能额度不足/提示词被拒）"];
  const imgs = [];
  for (const u of urls.slice(0, 4)) { try { imgs.push(await downloadDataUrl(u)); } catch (e) {} }
  if (!imgs.length) return [null, "千问生图结果下载失败"];
  return [imgs, null];
}

async function genImage(data) {
  const provider = (data.provider || "redfox").toLowerCase();
  const key = data.key || "";
  const images = data.images_b64 && data.images_b64.length ? data.images_b64 : (data.image_b64 ? [data.image_b64] : []);
  const prompt = data.prompt || "";
  const size = data.size || "1024x1536";
  const quality = data.quality || "high";
  const n = data.n || 1;
  const fidelity = data.fidelity || "";
  const endpoint = data.endpoint || "";
  const model = data.model || "";
  let imgs, err;
  switch (provider) {
    case "pollinations": [imgs, err] = await genPollinations(prompt, size, n); break;
    case "gemini": [imgs, err] = await genGemini(key, images, prompt, size, n, model); break;
    case "openai": [imgs, err] = await genOpenai(key, images, prompt, size, n); break;
    case "seedream": [imgs, err] = await genSeedream(key, images, prompt, size, n, model); break;
    case "qwen": [imgs, err] = await genQwen(key, images, prompt, size, n, model); break;
    case "nano": [imgs, err] = await genGemini(key, images, prompt, size, n, model || "gemini-3.1-flash-image"); break;
    case "custom": [imgs, err] = await genCustom(key, images, prompt, size, n, endpoint); break;
    default: [imgs, err] = await genRedfox(key, images, prompt, fidelity, size, quality, n);
  }
  return [imgs, localizeError(err)];
}

// ───────────────────────── 竞品分析（异步 job + 轮询，与云端一致） ─────────────────────────
const SCHEMA = `你是一位资深电商运营分析师，正在对一件真实的高销量商品做竞品拆解分析。你将看到该商品的【标题、主图、详情页截图、页面文本片段】。

[核心原则] 根据商品实际情况分析，有什么写什么，没有的不编造！
   ★ 如果该商品确实有明星/达人代言（标题或主图上有具体名字），请准确写出代言人姓名。
   ★ 如果该商品没有明星代言（大部分商品都没有），则绝对不要提明星，转而分析它真正的高销量原因：产品卖点、价格策略、包装设计、口感/成分/功效、使用场景、品牌信任度等。
   ★ 禁止把“明星代言”当成万能答案。

═══ 禁止事项 ═══
❌ 绝对禁止写：“信息有限”“基于可见内容推断”“可能”“推测”“大概”“或许”等任何推脱/模糊词汇
❌ 绝对禁止空话套话：“精准定位”“视觉营销”“品质优良”“精准把握用户需求”“吸引眼球”“提升转化”
❌ 绝对禁止无中生有：素材里没有明星/代言人就不要写明星代言
❌ 绝对禁止套用示例中的占位符

═══ 输出格式（严格 JSON，不要任何其他文字）═══
每个维度必须是「字符串数组」(JSON array of strings)，每条是一句独立的具体观察。

"summary": 高销量核心原因（2-3个具体因素，用“+”连接）。
"main_image": 主图吸引力拆解(3-5条)。每条必须回答“这张图凭什么让人想点击？”从人物/代言人、产品展示、色彩与背景、文字信息、整体构图角度写具体观察。
"title": 标题搜索策略拆解(3-5条)。逐条指出标题中的具体词语或结构（热搜词、痛点词、信任词、标题结构、emoji）。
"sku": SKU设计(2-4条)。有就分析，没有就写“当前素材未显示SKU信息”。
"detail": 详情页说服逻辑(3-5条)。分析首屏、叙事顺序、对比图、成分/参数/认证、用户评价、底部促单手段。
"marketing": 营销推广痕迹(2-4条)。代言人/达人、平台特征、促销标识、直播间/粉丝数等。
"price_strategy": 价格策略(2-3条)。具体价格、与竞品对比、促销/折扣。
"social_proof": 社会证明(2-3条)。销量数据、好评、达人背书、媒体曝光等。
"visual_style": 视觉风格(2-3条)。整体调性、配色、排版、与类目匹配的视觉语言。`;

const jobs = new Map();
const JOB_TTL = 600;
function cleanupJobs() {
  const now = Date.now();
  for (const [id, j] of jobs) if (now - (j.created_at || 0) > JOB_TTL * 1000) jobs.delete(id);
}

function buildMeta(data) {
  const meta = [];
  if (data.title) meta.push("商品标题：" + data.title);
  if (data.category) meta.push("类目：" + data.category);
  if (data.platform) meta.push("平台：" + data.platform);
  if (data.price) meta.push("价格区间：" + data.price);
  if (data.url) meta.push("商品链接：" + data.url);
  if (data.page_text) meta.push("【页面文本片段】\n" + data.page_text);
  return meta.join("\n") || "（未提供文字信息，仅凭主图分析）";
}
async function runAnalyze(data, jobId) {
  const setJob = (stage, pct, msg) => { const j = jobs.get(jobId); if (j) { j.status = "running"; j.progress = { stage, pct, msg }; } };
  try {
    const key = (data.zhitu_key || "").trim();
    if (!key) { const j = jobs.get(jobId); if (j) { j.status = "error"; j.error_msg = "未填写智谱 API Key（请在「API 设置」里填写「智谱 API Key」，竞品分析需要它）"; } return; }
    const metaTxt = buildMeta(data);
    setJob("prepare", 5, "已接收商品信息，准备分析…");
    let mainB64 = data.main_image || data.main_image_b64 || null;
    if (mainB64 && mainB64.indexOf(",") !== -1) mainB64 = mainB64.split(",", 2)[1];
    let messages, model;
    if (mainB64) {
      const raw = b64ToRaw(mainB64);
      const [, mime] = detectFmt(raw);
      const dataUrl = `data:${mime};base64,${raw.toString("base64")}`;
      messages = [{ role: "user", content: [
        { type: "text", text: "你是资深电商运营分析师。下面是一件高销量商品落地页的【主图】，请分析高销量原因，重点拆主图构图/配色/拍摄角度、标题关键词策略、SKU 矩阵设计、详情页叙事逻辑。\n商品信息：\n" + metaTxt + "\n\n" + SCHEMA },
        { type: "image_url", image_url: { url: dataUrl } }
      ] }];
      model = "glm-4v-flash";
    } else {
      messages = [{ role: "user", content: "你是资深电商运营分析师。下面是一件高销量商品的文字信息，请分析高销量原因，重点拆主图套路、标题关键词策略、SKU 矩阵设计、详情页叙事逻辑。\n商品信息：\n" + metaTxt + "\n\n" + SCHEMA }];
      model = "glm-4-flash";
    }
    setJob("analyze", 40, "🔍 AI 正在深度分析 标题 + 主图 + 详情页…");
    const content = await callZhipu(key, model, messages, 70000);
    const [text, err] = content;
    if (err) { const j = jobs.get(jobId); if (j) { j.status = "error"; j.error_msg = err; } return; }
    const obj = extractJson(text);
    if (!obj || typeof obj !== "object") { const j = jobs.get(jobId); if (j) { j.status = "error"; j.error_msg = "模型未返回有效 JSON：" + String(text).slice(0, 200); } return; }
    const result = {
      summary: String(obj.summary || "").trim(),
      main_image: asList(obj.main_image),
      title: asList(obj.title),
      sku: asList(obj.sku),
      detail: asList(obj.detail),
      marketing: asList(obj.marketing),
      price_strategy: asList(obj.price_strategy),
      social_proof: asList(obj.social_proof),
      visual_style: asList(obj.visual_style)
    };
    const j = jobs.get(jobId);
    if (j) { j.status = "done"; j.progress = { stage: "done", pct: 100, msg: "分析完成" }; j.result = result; }
  } catch (e) {
    const j = jobs.get(jobId); if (j) { j.status = "error"; j.error_msg = "分析异常：" + e.message; }
  }
}

// ───────────────────────── 提示词反推 ─────────────────────────
async function promptGen(data) {
  const key = (data.zhitu_key || "").trim();
  if (!key) return [null, "未填写智谱 API Key（请在「API 设置」里填写「智谱 API Key」，提示词生成需要它）"];
  const imageB64 = data.image_b64 || "";
  if (!imageB64) return [null, "请上传产品白底图"];
  const sellingPoints = (data.selling_points || "").trim();
  if (!sellingPoints) return [null, "请填写产品卖点/特点"];
  const styles = data.styles || ["详情页主图", "海报", "种草文", "场景图"];
  const raw = b64ToRaw(imageB64);
  const [, mime] = detectFmt(raw);
  const dataUrl = `data:${mime};base64,${raw.toString("base64")}`;
  const visionMessages = [{ role: "user", content: [
    { type: "image_url", image_url: { url: dataUrl } },
    { type: "text", text: "这是一张电商产品白底图。请用中文简洁分析以下方面（每项1-2句话）：\n1. 产品类型和核心外观特征\n2. 颜色、材质、质感\n3. 适合的目标人群和使用场景\n4. 拍摄角度和构图特点\n5. 适合的视觉风格方向（如：极简、清新、奢华、科技感等）\n请直接输出分析结果，不要加标题前缀。" }
  ] }];
  const [analysis, err1] = await callZhipu(key, "glm-4v-flash", visionMessages, 60000);
  if (err1) return [null, "产品图分析失败：" + err1];
  const styleList = styles.join("、");
  const genMessages = [
    { role: "system", content: "你是生图Agent提示词专家。根据产品分析结果和用户提供的卖点，为指定风格/场景分别生成专业的AI绘画提示词（英文prompt为主，中文说明为辅）。每条提示词要具体到：场景描述、光线、色调、构图、氛围、产品摆放方式。输出必须为严格JSON数组格式，每个元素包含 style(风格名)、prompt(英文生图提示词)、description(中文说明)。" },
    { role: "user", content: `【产品视觉分析】\n${analysis}\n\n【用户提供的卖点】\n${sellingPoints}\n\n【需要生成的提示词风格】\n${styleList}\n\n请为以上每种风格各生成1条高质量AI生图提示词。直接输出JSON数组，不要加其他文字。` }
  ];
  const [resultText, err2] = await callZhipu(key, "glm-4-flash", genMessages, 70000);
  if (err2) return [null, "提示词生成失败：" + err2];
  let prompts = extractJson(resultText);
  if (!Array.isArray(prompts) || !prompts.length) prompts = [{ style: styles[0], prompt: resultText, description: resultText.slice(0, 80) }];
  return [{ prompts, product_analysis: analysis, count: prompts.length }, null];
}

// ───────────────────────── 抓取商品页 ─────────────────────────
function detectPlatform(url) {
  if (/taobao\.com|tmall\.com/.test(url)) return "淘宝/天猫";
  if (/jd\.com/.test(url)) return "京东";
  if (/pinduoduo\.com|yangkeduo\.com/.test(url)) return "拼多多";
  if (/douyin\.com|jinritemai\.com/.test(url)) return "抖音电商";
  if (/1688\.com/.test(url)) return "1688";
  if (/amazon/.test(url)) return "Amazon";
  if (/xiaohongshu\.com|xhslink\.com/.test(url)) return "小红书";
  return "其他";
}
async function fetchProduct(data) {
  const url = (data.url || "").trim();
  if (!url || !/^https?:\/\//.test(url)) return [null, "链接格式不正确（需以 http/https 开头）"];
  const headers = { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept-Language": "zh-CN,zh;q=0.9" };
  let html;
  try { const r = await httpReq({ url, method: "GET", headers, timeout: 15000 }); html = r.body.toString("utf-8"); }
  catch (e) { return [null, "抓取商品页失败：" + e.message]; }
  if (!html) return [null, "抓取商品页返回空"];
  const meta = (prop) => { const m = html.match(new RegExp(`<meta[^>]+(?:property|name)=["']${prop}["'][^>]+content=["']([^"']+)`, "i")); return m ? m[1].trim() : ""; };
  let title = meta("og:title") || meta("twitter:title");
  if (!title) { const m = html.match(/<title[^>]*>([^<]+)<\/title>/i); if (m) title = m[1].trim(); }
  if (title) title = title.split(/\s*[-|–·]\s*(?:淘宝网|淘宝|天猫|京东|JD|京东商城|拼多多|抖音|Amazon|亚马逊|1688|唯品会|苏宁易购|苏宁)\s*$/)[0].trim();
  let imgUrl = meta("og:image:secure_url") || meta("og:image") || meta("twitter:image") || meta("image");
  if (!imgUrl) { const m = html.match(/"image"\s*:\s*"((?:https?:)?\/\/[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"/i); if (m) imgUrl = m[1].trim(); }
  let imageDataUrl = "";
  if (imgUrl && /^https?:\/\//.test(imgUrl)) { try { imageDataUrl = await downloadDataUrl(imgUrl, 15000); } catch (e) { imageDataUrl = ""; } }
  const desc = meta("og:description") || meta("description") || meta("twitter:description");
  let pageText = "";
  if (title) pageText += "页面标题: " + title + "\n";
  if (desc) pageText += "描述: " + desc + "\n";
  const priceM = html.match(/itemprop=["']price["'][^>]+content=["']([^"']+)/i) || html.match(/["']price["']\s*:\s*["']?([\d.]+)/);
  const price = priceM ? priceM[1].trim() : "";
  const platform = detectPlatform(url);
  return [{ title: title || "", image_data_url: imageDataUrl, price, platform, page_text: pageText.slice(0, 1500), detail_image_urls: [], note: imageDataUrl ? "" : "主图未能自动抓取，可手动粘贴主图链接或上传" }, null];
}

// ───────────────────────── HTTP 服务 ─────────────────────────
function sendJson(res, code, obj, cors) {
  const b = Buffer.from(JSON.stringify(obj, null, 0), "utf-8");
  if (cors !== false) setCors(res);
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Content-Length": b.length });
  res.end(b);
}
function setCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Headers", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
}

function startLocalProxy() {
  const server = http.createServer(async (req, res) => {
    if (req.method === "OPTIONS") { setCors(res); res.writeHead(204); res.end(); return; }
    const u = new URL(req.url, `http://${HOST}:${PORT}`);
    const path = u.pathname;
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      try {
        const data = body ? JSON.parse(body) : {};
        if (path === "/api/providers") { sendJson(res, 200, { providers: PROVIDERS }); return; }
        if (path === "/api/gen") {
          const [imgs, err] = await genImage(data);
          if (err) sendJson(res, 200, { error: err }); else sendJson(res, 200, { images: imgs });
          return;
        }
        if (path === "/api/analyze-competitor") {
          cleanupJobs();
          const jobId = "aj_" + crypto.randomBytes(6).toString("hex");
          jobs.set(jobId, { status: "running", progress: { stage: "prepare", pct: 5, msg: "任务已创建，正在启动分析…" }, created_at: Date.now() });
          runAnalyze(data, jobId);
          sendJson(res, 202, { job_id: jobId, msg: "分析任务已启动，请轮询 /api/analysis-status 查进度" });
          return;
        }
        if (path === "/api/analysis-status") {
          const jobId = u.searchParams.get("job_id") || "";
          const j = jobs.get(jobId);
          if (!j) { sendJson(res, 404, { error: "任务不存在或已过期（结果保留 10 分钟）", status: "expired" }); return; }
          const resp = { status: j.status, progress: j.progress || {} };
          if (j.status === "done" && j.result) resp.result = j.result;
          else if (j.status === "error") resp.error = j.error_msg || "未知错误";
          sendJson(res, 200, resp);
          return;
        }
        if (path === "/api/fetch-product") {
          const [meta, err] = await fetchProduct(data);
          if (err) sendJson(res, 200, { error: err }); else sendJson(res, 200, meta);
          return;
        }
        if (path === "/api/prompt-gen") {
          const [r, err] = await promptGen(data);
          if (err) sendJson(res, 200, { error: err }); else sendJson(res, 200, r);
          return;
        }
        sendJson(res, 404, { error: "未找到接口：" + path });
      } catch (e) {
        sendJson(res, 500, { error: "本地代理内部错误：" + e.message });
      }
    });
  });
  server.listen(PORT, HOST, () => {
    console.log(`[local-proxy] 已启动：http://${HOST}:${PORT}（离线本地代理，买家自配 Key）`);
  });
  server.on("error", (e) => console.error("[local-proxy] 启动失败：", e.message));
  return server;
}

module.exports = { startLocalProxy, PORT, HOST };
