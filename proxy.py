#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电商AI Image —— 本地小代理（多服务商版）
在浏览器（standalone.html）和各家生图接口之间转发请求，并补 CORS 头。

支持的服务商（provider）：
  pollinations  —— 免 Key、免注册，直接出图（仅文生图，图生图能力弱，适合快速试）
  gemini        —— Google Gemini（Nano Banana）免费层，支持图生图/文生图，需 AI Studio Key
  qwen          —— 阿里千问生图（百炼 DashScope）：3.0 标准/旗舰 Pro + 老版 plus/max，需百炼 API Key
  nano          —— Google Nano Banana 2（Gemini 3.1 Flash Image）家族：标准/Lite/Pro，需 AI Studio Key
  openai        —— OpenAI gpt-image-2，新号有试用金，支持图生图/文生图
  redfox        —— redfox.hk 转发 gpt-image-2（原默认）
  custom        —— 自部署模型 / 任意 OpenAI 兼容端点（付费或自建入口）

接口：
  GET  /              -> 返回 standalone.html（营销落地页）
  GET  /health        -> ok
  GET  /api/providers -> 返回可选服务商及配置（前端下拉框用）
  POST /api/verify    -> 静默校验动态访问码（不消耗次数）：{code} -> {valid, exp?, remaining_hours?, note?, mu?, uses_left?, error?}
  POST /api/unlock    -> 正式解锁（消耗一次使用名额）：{code} -> {valid, exp?, remaining_hours?, note?, mu?, uses_left?, error?}
  POST /api/gen       -> 生图（【需持有效动态访问码】）：{provider, key, image_b64, prompt, size, quality, n, fidelity, endpoint?}
                         请求头需带 Authorization: Bearer <码> 或 X-Access-Token: <码>
                         无码/无效/过期 -> 401 {"error":"访问码无效或已过期..."}
                         成功 -> {"images":["data:image/png;base64,..."]} 或 {"error":"原因"}
"""
import os, sys, json, base64, time, io, hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

# 动态访问码（测试阶段分享保护）：签发/校验逻辑放在 access.py，与 gentoken.py 共用同一密钥。
try:
    from access import verify_access_token, resolve_credential
except Exception:
    # 兜底：即便 access.py 缺失也不让整个代理崩溃，只是所有访问码都会被拒。
    def verify_access_token(code):
        return False, {"error": "access 模块缺失（请联系管理员）"}
    def resolve_credential(cred):
        ok, info = verify_access_token(cred)
        return ok, info, ("code" if ok else None)

PORT = int(os.environ.get("PORT") or os.environ.get("WB_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(HERE, "assets")

# 访问码使用次数记录（实现「每串码限定使用人数」）。
# 【默认不落盘】仅进程内计数，重启即清零——这是专为「本地版解锁问题」修的：
#   本地磁盘是持久的，以前一串码解锁一次后 used_codes.json 永久记 1，导致本机/换设备再解就被拒；
#   Render 磁盘本就是临时的，重启会清，所以线上暴露不出，本地一测就中招。
# 需要跨重启持久计数（如自建持久磁盘部署）时，设 PERSIST_USAGE=1 启用落盘。
# 要手动清空计数：python proxy.py --reset-usage  （或删 used_codes.json）
# 彻底吊销全部已发码：改 ACCESS_SECRET 即可一键作废。
USAGE_FILE = os.path.join(HERE, "used_codes.json")
PERSIST_USAGE = os.environ.get("PERSIST_USAGE") == "1"
_usage = {}
def _code_key(code):
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()
def _load_usage():
    global _usage
    _usage = {}
    if not PERSIST_USAGE:
        return  # 默认不落盘：本地每次启动都是干净计数，避免“解锁一次就永久失效”
    try:
        if os.path.exists(USAGE_FILE):
            _usage = json.load(open(USAGE_FILE, encoding="utf-8"))
    except Exception:
        _usage = {}
def _save_usage():
    if not PERSIST_USAGE:
        return  # 默认不落盘
    try:
        json.dump(_usage, open(USAGE_FILE, "w", encoding="utf-8"))
    except Exception:
        pass
_load_usage()

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
        "id": "qwen",
        "name": "千问生图（阿里百炼）",
        "needsKey": True,
        "imageEdit": True,
        "desc": "阿里千问生图（百炼/DashScope）。3.0 系支持文生图+图生图/参考图，付费：标准版 0.18元/张、旗舰 Pro 0.25元/张起；老版 plus/max 便宜、新用户开通百炼有免费额度（如 100 张/90 天，控制台可查）。需阿里云百炼 API Key。",
        "getKey": "https://bailian.console.aliyun.com",
        "models": [
            {"id": "qwen-image-3.0", "name": "3.0 标准版（0.18元/张，默认）"},
            {"id": "qwen-image-3.0-pro", "name": "3.0 旗舰 Pro（0.25元/张，排版/画质更强）"},
            {"id": "qwen-image-plus", "name": "老版 plus（便宜，新用户有免费额度）"},
            {"id": "qwen-image-max", "name": "老版 max（画质高，免费额度少）"},
        ],
    },
    {
        "id": "nano",
        "name": "Nano Banana 2（Gemini 3.1 Flash）",
        "needsKey": True,
        "imageEdit": True,
        "desc": "谷歌 Nano Banana 2（Gemini 3.1 Flash Image）家族：标准约$0.067/张、Lite 极速约$0.034/张、Pro 旗舰约$0.09/张。注意：走 API 是按张付费、无免费额度；免费只在 Gemini 应用 / AI Studio 网页（有每日上限）。需 AI Studio Key。",
        "getKey": "https://aistudio.google.com/apikey",
        "models": [
            {"id": "gemini-3.1-flash-image", "name": "Nano Banana 2 标准（约$0.067/张，默认）"},
            {"id": "gemini-3.1-flash-lite-image", "name": "Nano Banana 2 Lite（约$0.034/张，极速）"},
            {"id": "gemini-3-pro-image", "name": "Nano Banana Pro（约$0.09/张，画质顶配）"},
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
    # 千问 / 百炼
    if "InvalidApiKey" in m:
        return "千问 API Key 无效，请检查是否复制正确（阿里云百炼控制台 → API-KEY）。"
    if "Throttling" in m or "FlowExceedLimit" in m:
        return "千问调用被限流或免费额度已用完，请到百炼控制台查看用量或开通付费。"
    if "Model.AccessDenied" in m or "NotActivated" in m or "未开通" in m:
        return "该千问模型未在你账号开通，请到百炼控制台开通对应模型服务。"
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


def _gemini_ratio(size, default="3:4"):
    """把工具里的 WxH 尺寸转成 Gemini 的 aspectRatio；转不了就用默认 3:4。"""
    try:
        w, h = (size or "1024x1536").split("x")
        w, h = int(w), int(h)
        r = w / float(h)
    except Exception:
        return default
    for target, val in [("1:1", 1.0), ("3:4", 0.75), ("4:3", 1.3333), ("2:3", 0.6667), ("3:2", 1.5)]:
        if abs(r - val) < 0.05:
            return target
    return default


def gen_gemini(key, images, prompt, size, n, model=None):
    """Google Gemini（Nano Banana 家族）。支持图生图（传 images 列表）与文生图。images[0] 为主图，其余为参考图。"""
    if not key:
        return None, "缺少 Gemini/Nano Banana API Key"
    model = model or "gemini-2.5-flash-image"
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
        "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": _gemini_ratio(size)}},
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


def _qwen_err(j, model):
    """把百炼/DashScope 常见报错翻译成人话。"""
    code = str(j.get("code") or (j.get("output") or {}).get("code") or "")
    msg = str(j.get("message") or (j.get("output") or {}).get("message") or "") + " " + str(j.get("error", ""))
    if "InvalidApiKey" in msg or "InvalidApiKey" in code or "401" in code:
        return "千问 API Key 无效，请检查是否复制正确（阿里云百炼控制台 → API-KEY）。"
    if "Throttling" in msg or "FlowExceedLimit" in msg or "限流" in msg or "quota" in msg.lower() or "额度" in msg:
        return "千问调用被限流或额度不足（免费额度用完或需开通付费），请到百炼控制台查看用量。"
    if "Model.AccessDenied" in msg or "NotActivated" in msg or "未开通" in msg or "not activated" in msg.lower():
        return "该千问模型未在你账号开通，请到百炼控制台开通对应模型服务。"
    if "model does not exist" in msg.lower() or "ModelNotFound" in msg or "not found" in msg.lower():
        return "该千问模型 ID 不存在或未对你开放（若用 3.0 标准版报错，可换「3.0 旗舰 Pro」版本再试）。"
    return "千问生图失败(" + code + ")：" + (msg.strip() or "未知错误")


def _qwen_extract(j):
    """从百炼响应里取图片 URL（兼容同步/异步、老版/3.0 两种返回结构），下载成 base64。"""
    out = j.get("output") or {}
    if out.get("task_status") == "FAILED":
        return None, _qwen_err(j, j.get("model", ""))
    # 顶层直接报错（鉴权/参数/限流等，没有 output）：翻译成人话
    if not out and (j.get("code") or j.get("message") or j.get("error")):
        return None, _qwen_err(j, j.get("model", ""))
    urls = []
    for it in out.get("results") or []:            # 老版 text2image 异步任务结果
        if it.get("url"):
            urls.append(it["url"])
    for ch in out.get("choices") or []:            # 3.0 多模态对话结果
        c = (ch.get("message") or {}).get("content") or []
        for it in c:
            if isinstance(it, dict) and it.get("image"):
                urls.append(it["image"])
    extra = out.get("images") or []                # 兜底字段
    if isinstance(extra, str):
        urls.append(extra)
    else:
        urls.extend(extra)
    if not urls:
        return None, "千问生图未返回图片（可能额度不足/提示词被拒）"
    imgs = []
    for u in urls[:4]:
        try:
            d = S.get(u, timeout=120)
            imgs.append("data:image/png;base64," + base64.b64encode(d.content).decode())
        except Exception:
            continue
    if not imgs:
        return None, "千问生图结果下载失败"
    return imgs, None


def gen_qwen(key, images, prompt, size, n, model=None):
    """阿里千问生图（百炼 DashScope）。
    3.0 系走「多模态对话」接口（支持文生图 + 图生图/参考图，images[0] 为主图）；
    老版 qwen-image-plus/max 走 text2image 异步任务接口（仅文生图）。
    """
    if not key:
        return None, "缺少阿里云百炼 API Key"
    model = model or "qwen-image-3.0"
    try:
        count = max(1, min(4, int(n) if str(n).isdigit() else 1))
    except Exception:
        count = 1
    dsize = (size or "1024x1536").replace("x", "*")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    base = "https://dashscope.aliyuncs.com/api/v1/services/aigc"
    is_v3 = model.startswith("qwen-image-3.0")
    raws = [b64_to_bytes(im) for im in (images or [])]
    try:
        if is_v3:
            # 多模态对话接口：content 里可带 image（图生图/参考图）+ text（文生图时只有 text）
            content = []
            for raw in raws:
                if not raw:
                    continue
                _, mime = detect_fmt(raw)
                content.append({"image": "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())})
            content.append({"text": prompt})
            payload = {
                "model": model,
                "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": {"size": dsize, "n": count, "prompt_extend": True},
            }
            r = S.post(base + "/multimodal-generation/generation", headers=headers, json=payload, timeout=240)
            j = r.json()
            task_id = (j.get("output") or {}).get("task_id")
            if task_id:  # 异步返回：轮询任务
                for _ in range(MAX_TRY):
                    time.sleep(POLL)
                    try:
                        rr = S.get("https://dashscope.aliyuncs.com/api/v1/tasks/" + task_id, headers=headers, timeout=15)
                        j = rr.json()
                    except Exception:
                        continue
                    st = (j.get("output") or {}).get("task_status")
                    if st in ("SUCCEEDED", "FAILED"):
                        break
            return _qwen_extract(j)
        # 老版：text2image 异步任务接口（仅文生图）
        if any(raws):
            return None, "该千问版本仅支持文生图；图生图/参考图请把版本换成「3.0 标准版」或「3.0 旗舰 Pro」。"
        payload = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": {"size": dsize, "n": count, "watermark": False},
        }
        r = S.post(base + "/text2image/image-synthesis", headers=headers, json=payload, timeout=60)
        j = r.json()
        task_id = (j.get("output") or {}).get("task_id")
        if not task_id:
            return None, _qwen_err(j, model)
        for _ in range(MAX_TRY):
            time.sleep(POLL)
            try:
                rr = S.get("https://dashscope.aliyuncs.com/api/v1/tasks/" + task_id, headers=headers, timeout=15)
                j = rr.json()
            except Exception:
                continue
            st = (j.get("output") or {}).get("task_status")
            if st == "SUCCEEDED":
                break
            if st == "FAILED":
                return None, _qwen_err(j, model)
        return _qwen_extract(j)
    except Exception as e:
        return None, "千问生图请求失败：" + str(e)


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
        if provider == "qwen":
            if not prompt:
                return None, "缺少提示词"
            return gen_qwen(key, images, prompt, size, n, model)
        if provider == "nano":
            if not prompt:
                return None, "缺少提示词"
            return gen_gemini(key, images, prompt, size, n, model or "gemini-3.1-flash-image")
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
        if self.path == "/api/verify":
            self._handle_verify()
            return
        if self.path == "/api/unlock":
            self._handle_unlock()
            return
        if self.path != "/api/gen":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        # —— 动态访问码门禁：未持有效码者不得生图（硬拦截，绕过前端门禁也无效）——
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln)
            data = json.loads(body or b"{}")
        except Exception as e:
            self._send_json(400, {"error": "请求解析失败：" + str(e)})
            return
        token = (self.headers.get("Authorization") or "").replace("Bearer ", "", 1).strip()
        if not token:
            token = (self.headers.get("X-Access-Token") or "").strip()
        ok, info, kind = resolve_credential(token)
        if not ok:
            self._send_json(401, {"error": "访问码无效或已过期，请先在页面输入正确的动态访问码。（" + info.get("error", "") + "）"})
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

    def _enrich_err(self, info):
        """把「签名无效」等笼统错误补上可操作的排查提示（针对密钥不一致场景）。"""
        err = (info or {}).get("error", "")
        if "签名无效" in err:
            err += "（请确认 gentoken.py 与运行 proxy.py 的服务端使用【同一 ACCESS_SECRET】；"
            err += "本地发码若没设 ACCESS_SECRET，而服务端设了自定义密钥，就会对不上）"
        return err

    def _handle_verify(self):
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln)
            data = json.loads(body or b"{}")
        except Exception:
            self._send_json(400, {"error": "请求解析失败"})
            return
        code = (data.get("code") or "").strip()
        ok, info, kind = resolve_credential(code)
        if ok:
            if kind == "master":
                # 总钥匙：永久解锁，不消耗次数
                self._send_json(200, {"valid": True, "master": True, "exp": None,
                                      "remaining_hours": None, "note": "总钥匙(永久)",
                                      "mu": None, "uses_left": None})
                return
            mu = info.get("mu", None)
            left = (mu - _usage.get(_code_key(code), 0)) if mu is not None else None
            self._send_json(200, {"valid": True, "exp": info.get("exp"),
                                  "remaining_hours": info.get("remaining_hours"),
                                  "note": info.get("note"), "mu": mu, "uses_left": left})
        else:
            self._send_json(200, {"valid": False, "error": self._enrich_err(info)})

    def _handle_unlock(self):
        # 正式解锁：校验通过且未超使用上限时，消耗一次名额（落盘持久化）。
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln)
            data = json.loads(body or b"{}")
        except Exception:
            self._send_json(400, {"error": "请求解析失败"})
            return
        code = (data.get("code") or "").strip()
        ok, info, kind = resolve_credential(code)
        if not ok:
            self._send_json(200, {"valid": False, "error": self._enrich_err(info)})
            return
        if kind == "master":
            # 总钥匙：永久解锁，不消耗次数、不计数
            self._send_json(200, {"valid": True, "master": True, "exp": None,
                                  "remaining_hours": None, "note": "总钥匙(永久)",
                                  "mu": None, "uses_left": None})
            return
        mu = info.get("mu", None)
        if mu is not None:
            key = _code_key(code)
            used = _usage.get(key, 0)
            if used >= mu:
                self._send_json(200, {"valid": False,
                                      "error": "该访问码已达使用上限（%d 人/次），如需更多请向发放人申请新码" % mu})
                return
            _usage[key] = used + 1
            _save_usage()
        left = (mu - _usage.get(_code_key(code), 0)) if mu is not None else None
        self._send_json(200, {"valid": True, "exp": info.get("exp"),
                              "remaining_hours": info.get("remaining_hours"),
                              "note": info.get("note"), "mu": mu, "uses_left": left})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    import socket
    # —— 维护命令：清空访问码使用计数（解决本地“解锁一次就永久失效”）——
    if "--reset-usage" in sys.argv:
        try:
            if os.path.exists(USAGE_FILE):
                os.remove(USAGE_FILE)
                print("[reset-usage] 已清空 %s，所有访问码重新可用（限当前落盘计数）" % USAGE_FILE)
            else:
                print("[reset-usage] 没有可清空的计数文件（%s 不存在）" % USAGE_FILE)
        except Exception as e:
            print("[reset-usage] 清空失败：%s" % e)
        sys.exit(0)
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
