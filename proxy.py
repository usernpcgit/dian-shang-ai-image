#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电商AI Image —— 本地小代理（多服务商版）
在浏览器（standalone.html）和各家生图接口之间转发请求，并补 CORS 头。

支持的服务商（provider）：
  pollinations  —— 免 Key、免注册，直接出图（仅文生图，图生图能力弱，适合快速试）
  gemini        —— Google Gemini（Nano Banana）免费层，支持图生图/文生图，需 AI Studio Key
  openai        —— OpenAI gpt-image-2，新号有试用金，支持图生图/文生图
  redfox        —— redfox.hk 转发 gpt-image-2（原默认）
  custom        —— 自部署模型 / 任意 OpenAI 兼容端点（付费或自建入口）

接口：
  GET  /              -> 返回 standalone.html
  GET  /health        -> ok
  GET  /api/providers -> 返回可选服务商及配置（前端下拉框用）
  POST /api/gen       -> 生图：{provider, key, image_b64, prompt, size, quality, n, fidelity, endpoint?}
                         返回 {"images":["data:image/png;base64,..."]} 或 {"error":"原因"}
"""
import os, json, base64, time, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

PORT = int(os.environ.get("PORT") or os.environ.get("WB_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(HERE, "assets")

def _resolve(name):
    for _p in [os.path.join(HERE, name), os.path.join(os.getcwd(), name), os.path.join("/opt/render/project/src", name)]:
        if os.path.exists(_p):
            return _p
    return os.path.join(HERE, name)

LANDING = _resolve("landing.html")   # 根路径：营销落地页
HTML = _resolve("standalone.html")   # /tool：工具页

POLL = 3
MAX_TRY = 80

# 绕过系统/环境变量里的 http 代理，直连（否则会被公司代理拦截）
S = requests.Session()
S.trust_env = False


# ─────────── 服务商静态配置（前端下拉框用） ───────────
PROVIDERS = [
    {
        "id": "pollinations",
        "name": "Pollinations（免 Key 免费）",
        "needsKey": False,
        "imageEdit": False,
        "desc": "完全免费、免注册、免 Key，直接出图。适合快速试效果；仅文生图，图生图能力弱，且不稳定（无 SLA）。",
        "getKey": "无需 Key",
    },
    {
        "id": "gemini",
        "name": "Gemini（Google 免费层）",
        "needsKey": True,
        "imageEdit": True,
        "desc": "Google AI Studio 免费层（Nano Banana），每日约 500 张，支持图生图。需去 aistudio.google.com 拿 Key。免费层带隐形水印、商业用途受限。",
        "getKey": "https://aistudio.google.com/apikey",
    },
    {
        "id": "openai",
        "name": "OpenAI（gpt-image-2）",
        "needsKey": True,
        "imageEdit": True,
        "desc": "OpenAI 官方 gpt-image-2，新号有约 $5 试用金，支持图生图/文生图。需能访问 api.openai.com。",
        "getKey": "https://platform.openai.com/api-keys",
    },
    {
        "id": "redfox",
        "name": "redfox.hk（gpt-image-2 转发）",
        "needsKey": True,
        "imageEdit": True,
        "desc": "redfox.hk 转发 gpt-image-2，国内访问友好，需注册 Key（现多为付费）。",
        "getKey": "https://redfox.hk/settings/api-keys?source=skillhub",
    },
    {
        "id": "seedream",
        "name": "Seedream 5.0（字节火山）",
        "needsKey": True,
        "imageEdit": True,
        "desc": "字节 Seedream 5.0（火山方舟），官网只有 Pro 和 Lite 两种。Pro 质量高但额度紧；Lite 额度宽松，但要求图片总像素不低于 3686400（约 2048×2048）。",
        "getKey": "https://console.volcengine.com/ark/region:cn-beijing/apikey",
        "models": [
            {"id": "doubao-seedream-5-0-lite-260128", "name": "Lite版（额度宽松，默认）"},
            {"id": "doubao-seedream-5-0-pro-260628", "name": "Pro版（质量更高，额度紧）"},
        ],
    },
    {
        "id": "custom",
        "name": "自部署 / 自定义端点",
        "needsKey": True,
        "imageEdit": True,
        "desc": "对接你自己部署的模型（如本地 ComfyUI、自建 OpenAI 兼容服务、或任意充值付费的兼容端点）。需填 Base URL。",
        "getKey": "取决于你的部署；留空或填对应鉴权 Token",
    },
]


def detect_fmt(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "jpeg", "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "png", "image/png"


def b64_to_bytes(image_b64):
    if not image_b64:
        return None
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    return base64.b64decode(image_b64)


def localize_error(msg):
    """把常见英文模型报错翻译成中文，避免用户看不懂。"""
    if not msg:
        return msg
    m = str(msg)
    # Seedream / 火山方舟
    if "image size must be at least 3686400 pixels" in m:
        return "你选的尺寸太小：Seedream Lite 要求图片总像素不低于 3686400（例如 2048×2048、1664×2496），请在内容面板选更大画幅。"
    if "The parameter `size` specified in the request is not valid" in m:
        return "图片尺寸参数不符合模型要求，请换更大的画幅（如 2048×2048 或 1664×2496）。"
    if "inference limit" in m and "Safe Experience Mode" in m:
        return "该模型当前额度已用完或被「安全体验模式」暂停。请切换 Pro/Lite 版本，或去火山方舟控制台关闭安全体验模式/开通付费额度。"
    if "inference limit" in m:
        return "该模型当前额度已用完，请切换其他模型版本或去控制台开通更多额度。"
    if "Model Activation" in m or "Safe Experience Mode" in m:
        return "模型未激活或被安全体验模式限制，请去火山方舟控制台激活模型或关闭安全体验模式。"
    # OpenAI
    if "Billing hard limit has been reached" in m or "exceeded your current quota" in m or "insufficient_quota" in m:
        return "你的 OpenAI 账号余额不足或免费额度已用完，请充值或换其他服务商。"
    if "Invalid API Key" in m or "Incorrect API key" in m:
        return "API Key 无效，请检查是否复制正确。"
    if "content_policy_violation" in m or "safety system" in m:
        return "提示词触发了内容安全过滤，请修改提示词后重试。"
    # Gemini
    if "API key not valid" in m:
        return "Gemini API Key 无效，请检查是否复制正确。"
    if "permission" in m and "Gemini" in m:
        return "Gemini Key 没有调用该模型的权限，请确认已开启 Gemini API 权限。"
    return msg


# ─────────── 各服务商实现 ───────────

def gen_pollinations(prompt, size, n):
    """免 Key 文生图。size 形如 1024x1536 -> 取宽高。"""
    try:
        w, h = (size or "1024x1536").split("x")
    except Exception:
        w, h = "1024", "1536"
    # 去除提示词里的中文标点风险，做 url 转义
    from urllib.parse import quote
    safe = quote(prompt[:400], safe="")
    try:
        out = []
        count = max(1, min(4, int(n) if str(n).isdigit() else 1))
        for _ in range(count):
            url = f"https://image.pollinations.ai/prompt/{safe}?width={w}&height={h}&nologo=true"
            r = S.get(url, timeout=120)
            out.append("data:image/jpeg;base64," + base64.b64encode(r.content).decode())
        return out, None
    except Exception as e:
        return None, "Pollinations 生图失败：" + str(e)


def gen_gemini(key, images, prompt, size, n):
    """Google Gemini（Nano Banana）。支持图生图（传 images 列表）与文生图。images[0] 为主图，其余为参考图。"""
    if not key:
        return None, "缺少 Gemini API Key"
    model = "gemini-2.5-flash-image"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    try:
        count = max(1, min(4, int(n) if str(n).isdigit() else 1))
    except Exception:
        count = 1
    parts = []
    for im in (images or []):
        raw = b64_to_bytes(im)
        if not raw:
            continue
        _, mime = detect_fmt(raw)
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode()}})
    parts.append({"text": prompt})
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "3:4"}},
    }
    try:
        r = S.post(url, json=payload, timeout=240)
        j = r.json()
    except Exception as e:
        return None, "Gemini 请求失败（超时/网络）：" + str(e)
    if "error" in j:
        return None, "Gemini 错误：" + str(j["error"].get("message", j["error"]))
    try:
        parts = (j.get("candidates", [{}])[0].get("content", {}).get("parts", []))
        out = []
        for p in parts:
            if "inline_data" in p:
                b = p["inline_data"]["data"]
                mime = p["inline_data"].get("mime_type", "image/png")
                out.append("data:%s;base64,%s" % (mime, b))
                if len(out) >= count:
                    break
        if not out:
            return None, "Gemini 未返回图片（可能触发安全过滤或提示词被拒）"
        return out, None
    except Exception as e:
        return None, "Gemini 结果解析失败：" + str(e)


def gen_openai(key, images, prompt, size, n):
    """OpenAI gpt-image-2。图生图走 /v1/images/edits（支持多图），文生图走 /v1/images/generations。"""
    if not key:
        return None, "缺少 OpenAI API Key"
    try:
        count = max(1, min(4, int(n) if str(n).isdigit() else 1))
    except Exception:
        count = 1
    if images:
        files = []
        for idx, im in enumerate(images):
            raw = b64_to_bytes(im)
            if not raw:
                continue
            fmt, mime = detect_fmt(raw)
            files.append(("image", ("ref%d.%s" % (idx, fmt), raw, mime)))
        if not files:
            return None, "白底产品图/参考图解码失败"
        try:
            r = S.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": f"Bearer {key}"},
                files=files,
                data={"model": "gpt-image-2", "prompt": prompt, "n": count, "size": size or "1024x1536"},
                timeout=240,
            )
            j = r.json()
        except Exception as e:
            return None, "OpenAI 请求失败（超时/网络）：" + str(e)
    else:
        try:
            r = S.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": "gpt-image-2", "prompt": prompt, "n": count, "size": size or "1024x1536", "quality": "high"},
                timeout=240,
            )
            j = r.json()
        except Exception as e:
            return None, "OpenAI 请求失败（超时/网络）：" + str(e)
    if "error" in j:
        return None, "OpenAI 错误：" + str(j["error"].get("message", j["error"]))
    out = []
    for it in j.get("data", []):
        if it.get("b64_json"):
            out.append("data:image/png;base64," + it["b64_json"])
        elif it.get("url"):
            try:
                d = S.get(it["url"], timeout=120)
                out.append("data:image/png;base64," + base64.b64encode(d.content).decode())
            except Exception:
                pass
    if not out:
        return None, "OpenAI 未返回图片"
    return out, None


def gen_redfox(key, images, prompt, fidelity, size, quality, n):
    """redfox.hk 转发 gpt-image-2（原逻辑）。仅用主图（images[0]），参考图暂不支持。"""
    if not key:
        return None, "缺少 redfox API Key"
    image_b64 = (images or [None])[0]
    if not image_b64:
        return None, "缺少白底产品图（redfox 为图生图，需上传底图）"
    try:
        raw = b64_to_bytes(image_b64)
    except Exception as e:
        return None, "图片解码失败：" + str(e)
    fmt, mime = detect_fmt(raw)
    try:
        up = S.post(
            "https://redfox.hk/story/api/parseWork/imageGen/uploadImage",
            files={"file": ("product." + fmt, raw, mime)},
            data={"format": fmt},
            headers={"X-API-KEY": key},
            timeout=60,
        )
        upj = up.json()
    except Exception as e:
        return None, "上传产品图失败：" + str(e)
    if not str(upj.get("code", "")).startswith("2"):
        return None, "上传失败(" + str(upj.get("code")) + ")：" + str(upj.get("msg", ""))
    image_url = (upj.get("data") or {}).get("imageUrl")
    if not image_url:
        return None, "上传成功但未返回图片地址"
    params = {
        "modelName": "gpt-image-2",
        "n": max(1, min(10, int(n) if str(n).isdigit() else 1)),
        "size": size or "1024x1536",
        "quality": quality or "high",
        "outputFormat": "png",
        "background": "auto",
        "outputCompression": 0,
    }
    if fidelity:
        params["inputFidelity"] = fidelity
    try:
        r = S.post(
            "https://redfox.hk/story/api/parseWork/imageGen/submitSkill",
            json={"prompt": prompt, "source": "GPT image2-SkillHub", "operation": "edit",
                  "images": [{"url": image_url}], "parameters": params},
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            timeout=30,
        )
        sj = r.json()
    except Exception as e:
        return None, "提交生图任务失败：" + str(e)
    if not str(sj.get("code", "")).startswith("2"):
        return None, "提交失败(" + str(sj.get("code")) + ")：" + str(sj.get("msg", ""))
    task_id = (sj.get("data") or {}).get("taskId")
    if not task_id:
        return None, "接口未返回 taskId"
    for _ in range(MAX_TRY):
        time.sleep(POLL)
        try:
            rr = S.post(
                "https://redfox.hk/story/api/parseWork/imageGen/result",
                json={"taskId": task_id},
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                timeout=15,
            )
            rj = rr.json()
        except Exception:
            continue
        if not str(rj.get("code", "")).startswith("2"):
            return None, "查询结果失败：" + str(rj.get("msg", ""))
        st = (rj.get("data") or {}).get("status")
        if st == "success":
            paths = (rj.get("data") or {}).get("imagePaths") or []
            if not paths:
                return None, "生图成功但未返回图片"
            try:
                out = []
                for p in paths[: max(1, min(4, int(n) if str(n).isdigit() else 1))]:
                    d = S.get(p, timeout=120)
                    out.append("data:image/png;base64," + base64.b64encode(d.content).decode())
                return out, None
            except Exception as e:
                return None, "下载生成图失败：" + str(e)
        elif st == "failed":
            return None, "生图失败：" + str((rj.get("data") or {}).get("failReason", "未知原因"))
    return None, "等待超时（约 %d 秒）" % (POLL * MAX_TRY)


def gen_custom(key, images, prompt, size, n, endpoint):
    """自部署 / 任意 OpenAI 兼容端点。endpoint 形如 https://your.host/v1/images/generations"""
    if not endpoint:
        return None, "自定义端点需填写 Base URL"
    try:
        count = max(1, min(4, int(n) if str(n).isdigit() else 1))
    except Exception:
        count = 1
    if images:
        files = []
        for idx, im in enumerate(images):
            raw = b64_to_bytes(im)
            if not raw:
                continue
            fmt, mime = detect_fmt(raw)
            files.append(("image", ("ref%d.%s" % (idx, fmt), raw, mime)))
        if not files:
            return None, "白底产品图/参考图解码失败"
        try:
            r = S.post(
                endpoint,
                headers={"Authorization": f"Bearer {key}"} if key else {},
                files=files,
                data={"prompt": prompt, "n": count, "size": size or "1024x1536"},
                timeout=240,
            )
            j = r.json()
        except Exception as e:
            return None, "自定义端点请求失败（超时/网络）：" + str(e)
    else:
        try:
            r = S.post(
                endpoint,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else {"Content-Type": "application/json"},
                json={"prompt": prompt, "n": count, "size": size or "1024x1536"},
                timeout=240,
            )
            j = r.json()
        except Exception as e:
            return None, "自定义端点请求失败（超时/网络）：" + str(e)
    if "error" in j:
        return None, "自定义端点错误：" + str(j["error"].get("message", j["error"]))
    out = []
    for it in j.get("data", []):
        if it.get("b64_json"):
            out.append("data:image/png;base64," + it["b64_json"])
        elif it.get("url"):
            try:
                d = S.get(it["url"], timeout=120)
                out.append("data:image/png;base64," + base64.b64encode(d.content).decode())
            except Exception:
                pass
    if not out:
        return None, "自定义端点未返回图片"
    return out, None


def seedream_valid_size(size, model):
    """根据 Pro/Lite 的像素范围，把用户选的尺寸自动缩放到合法范围。"""
    # Pro: [921600, 4624220]；Lite: [3686400, 16777216]
    if model == "doubao-seedream-5-0-pro-260628":
        min_area, max_area = 921600, 4624220
    else:
        min_area, max_area = 3686400, 16777216
    try:
        w, h = (size or "1024x1536").split("x")
        w, h = int(w), int(h)
    except Exception:
        w, h = 1024, 1536
    if w <= 0 or h <= 0:
        w, h = 1024, 1536
    area = w * h
    if area < min_area:
        scale = (min_area / area) ** 0.5
    elif area > max_area:
        scale = (max_area / area) ** 0.5
    else:
        return "%dx%d" % (w, h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    # 保证缩放后因取整仍满足范围
    if nw * nh < min_area:
        nw, nh = int((min_area * (w / h)) ** 0.5) + 1, int((min_area * (h / w)) ** 0.5) + 1
    if nw * nh > max_area:
        nw, nh = int((max_area * (w / h)) ** 0.5), int((max_area * (h / w)) ** 0.5)
    return "%dx%d" % (max(16, nw), max(16, nh))


def gen_seedream(key, images, prompt, size, n, model=None):
    """字节 Seedream 5.0（火山方舟）。OpenAI 兼容格式，支持图生图 + 多图参考（image 传数组）。"""
    if not key:
        return None, localize_error("缺少火山方舟 ARK API Key")
    if not prompt:
        return None, localize_error("缺少提示词")
    # 火山官网只有 Pro 和 Lite；默认 Lite（额度宽松），可选 Pro（质量更高）。
    model = model or "doubao-seedream-5-0-lite-260128"
    url = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    try:
        count = max(1, min(4, int(n) if str(n).isdigit() else 1))
    except Exception:
        count = 1
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    out = []
    try:
        for _ in range(count):
            payload = {
                "model": model,
                "prompt": prompt,
                "size": seedream_valid_size(size, model),
                "output_format": "png",
                "response_format": "b64_json",
                "watermark": False,
            }
            if images:
                # images[0] 为主图（白底/产品图），其余为参考图（风格/版式参考）
                payload["image"] = images if len(images) > 1 else images[0]
            r = S.post(url, headers=headers, json=payload, timeout=240)
            try:
                j = r.json()
            except Exception:
                return None, "Seedream 返回非 JSON（可能鉴权失败或网络不通）：HTTP %d" % r.status_code
            items = j.get("data") or []
            if not items:
                if "error" in j:
                    err = j["error"]
                    return None, "Seedream 错误：" + str(err.get("message", err) if isinstance(err, dict) else err)
                if j.get("message"):
                    return None, "Seedream 错误(" + str(j.get("code", "")) + ")：" + str(j.get("message"))
                return None, "Seedream 未返回图片（可能 Key 无效/额度不足/提示词被拒）"
            got = False
            for it in items:
                if it.get("b64_json"):
                    out.append("data:image/png;base64," + it["b64_json"])
                    got = True
                elif it.get("url"):
                    try:
                        d = S.get(it["url"], timeout=120)
                        out.append("data:image/png;base64," + base64.b64encode(d.content).decode())
                        got = True
                    except Exception:
                        pass
            if not got:
                return None, "Seedream 未返回图片数据"
    except Exception as e:
        return None, "Seedream 请求失败：" + str(e)
    if not out:
        return None, "Seedream 未返回图片"
    return out, None


def gen_image(data):
    provider = (data.get("provider") or "redfox").lower()
    key = data.get("key", "")
    # 支持数组 images_b64（含主图+参考图）；兼容旧单字段 image_b64
    images = data.get("images_b64") or []
    if not images and data.get("image_b64"):
        images = [data["image_b64"]]
    prompt = data.get("prompt", "")
    size = data.get("size") or "1024x1536"
    quality = data.get("quality") or "high"
    n = data.get("n") or 1
    fidelity = data.get("fidelity") or ""
    endpoint = data.get("endpoint") or ""
    model = data.get("model") or ""

    def run():
        if provider == "pollinations":
            if not prompt:
                return None, "缺少提示词"
            return gen_pollinations(prompt, size, n)
        if provider == "gemini":
            if not prompt:
                return None, "缺少提示词"
            return gen_gemini(key, images, prompt, size, n)
        if provider == "openai":
            if not prompt:
                return None, "缺少提示词"
            return gen_openai(key, images, prompt, size, n)
        if provider == "seedream":
            if not prompt:
                return None, "缺少提示词"
            return gen_seedream(key, images, prompt, size, n, model)
        if provider == "custom":
            if not prompt:
                return None, "缺少提示词"
            return gen_custom(key, images, prompt, size, n, endpoint)
        # 默认 redfox
        return gen_redfox(key, images, prompt, fidelity, size, quality, n)

    imgs, err = run()
    return imgs, localize_error(err)


class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _serve_file(self, path, ctype="text/html; charset=utf-8"):
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_response(404)
            self.end_headers()

    def _serve_static(self, rel):
        # 防目录穿越：只允许 assets/ 下的普通文件名
        name = os.path.basename(rel)
        ext = os.path.splitext(name)[1].lower()
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
            ".ico": "image/x-icon", ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        self._serve_file(os.path.join(ASSETS_DIR, name), mime)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_file(LANDING)
            return
        if self.path == "/tool":
            self._serve_file(HTML)
            return
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/api/providers":
            self._send_json(200, {"providers": PROVIDERS})
            return
        if self.path.startswith("/assets/"):
            self._serve_static(self.path[len("/assets/"):])
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/api/gen":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln)
            data = json.loads(body or b"{}")
        except Exception as e:
            self._send_json(400, {"error": "请求解析失败：" + str(e)})
            return
        try:
            imgs, err = gen_image(data)
        except Exception as e:
            self._send_json(200, {"error": "代理内部错误：" + str(e)})
            return
        if err:
            self._send_json(200, {"error": err})
        else:
            self._send_json(200, {"images": imgs})

    def _send_json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    import socket
    print("[启动] PORT=%s, HTML=%s, exists=%s" % (PORT, HTML, os.path.exists(HTML)))
    print("[启动] cwd=%s, files=%s" % (os.getcwd(), os.listdir(os.getcwd())[:10]))
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    try:
        lan = socket.gethostbyname(socket.gethostname())
    except Exception:
        lan = "本机局域网IP"
    print("电商AI Image 已启动（页面 + 多服务商生图代理一体）")
    print("  本机打开  : http://localhost:%d/" % PORT)
    print("  手机/同网络: http://%s:%d/   （手机浏览器打开即可直接用 AI 生图）" % (lan, PORT))
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
