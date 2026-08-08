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
import os, sys, json, base64, time, io, hashlib, re, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# 允许本地用 .env 文件提供环境变量（如 ACCESS_SECRET），避免每次手动 export、重启即失效。
# 仅当环境变量尚未设置时才从 .env 读取；Render 等平台通过真实环境变量注入时不受影响。
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), "r", encoding="utf-8") as _envf:
        for _envline in _envf:
            _envline = _envline.strip()
            if not _envline or _envline.startswith("#") or "=" not in _envline:
                continue
            _envk, _envv = _envline.split("=", 1)
            _envk, _envv = _envk.strip(), _envv.strip().strip('"').strip("'")
            if _envk and _envk not in os.environ:
                os.environ[_envk] = _envv
except FileNotFoundError:
    pass

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

# 智谱 GLM API Key（竞品分析 / 视觉模型用）。
# 优先取环境变量 ZHIPU_API_KEY（云端 Render 控制台配置最安全）；未设置时回退到下方内置值。
# 注意：内置 Key 会随代码提交进版本库，仅适合个人/可信部署；公开仓库请勿提交真实 Key。
ZHIPU_API_KEY = (os.environ.get("ZHIPU_API_KEY") or "4cbff98c2b5745aa9905fdb135128e85.VzamDNEFfNMn6wzi").strip()

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


# ─────────── 竞品智能分析（智谱 GLM：文本 + 视觉） ───────────
# 复用电商 AI Image 已配置的 ZHIPU_API_KEY（与 model-eyes 同源）。
# 有主图 -> GLM-4V 多模态真「看」图；无主图 -> GLM 文本模型拆标题/SKU/详情页。
# 返回固定 schema：{summary, main_image[], title[], sku[], detail[]}，与前端卡片渲染字段一一对应。
ZHIPU_API = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def _call_zhipu(key, model, messages, timeout=70):
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": 0.6, "max_tokens": 1024}
    try:
        r = S.post(ZHIPU_API, headers=headers, json=payload, timeout=timeout)
    except Exception as e:
        return None, "调用智谱 API 失败：" + str(e)
    if r.status_code != 200:
        return None, localize_error("智谱 API 返回 %d：%s" % (r.status_code, r.text[:300]))
    try:
        j = r.json()
        return j["choices"][0]["message"]["content"], None
    except Exception as e:
        return None, "解析智谱返回失败：" + str(e)


def _extract_json(text):
    if not text:
        return None
    s = text.strip()
    # 去掉可能的 ```json … ``` 围栏
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    a = s.find("{"); b = s.rfind("}")
    if a != -1 and b != -1 and b > a:
        try:
            return json.loads(s[a:b + 1])
        except Exception:
            return None
    return None


def _as_list(v):
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, str):
                s = x.strip()
                if s:
                    out.append(s)
            elif isinstance(x, dict):
                # 模型偶尔把数组元素写成嵌套对象，压成可读短句兜底
                s = "；".join("%s：%s" % (k, val) for k, val in x.items() if val not in (None, ""))
                if s:
                    out.append(s)
            else:
                s = str(x).strip()
                if s:
                    out.append(s)
        return out
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def analyze_competitor(data):
    key = (data.get("zhitu_key") or ZHIPU_API_KEY).strip()
    if not key:
        return None, "未配置 ZHIPU_API_KEY（请在运行 proxy.py 的环境里设置智谱 API Key，与 model-eyes 同源）"
    title = (data.get("title") or "").strip()
    category = (data.get("category") or "").strip()
    platform = (data.get("platform") or "").strip()
    price = (data.get("price") or "").strip()
    url = (data.get("url") or "").strip()
    page_text = (data.get("page_text") or "").strip()
    main_b64 = data.get("main_image") or data.get("main_image_b64") or None
    if main_b64 and "," in main_b64:
        main_b64 = main_b64.split(",", 1)[1]

    meta = []
    if title: meta.append("商品标题：" + title)
    if category: meta.append("类目：" + category)
    if platform: meta.append("平台：" + platform)
    if price: meta.append("价格区间：" + price)
    if url: meta.append("商品链接：" + url)
    if page_text: meta.append("【页面文本片段（用于推断真实标题/卖点；若上方未给标题，请据此提取）】\n" + page_text)
    meta_txt = "\n".join(meta) if meta else "（未提供文字信息，仅凭主图分析）"
    # 预提取代言人：从标题/页面文字中用正则匹配【XXX代言】【XXX推荐】【XXX同款】    _spokesperson = ""    import re as _re    for _src in filter(None, [title, page_text]):        _m = _re.search(r"【([^】]{2,8})(?:代言|推荐|同款|助力|站台)】", _src)        if _m:            _spokesperson = _m.group(1).strip()            break    if _spokesperson:        meta_txt += "\n\n⚠️ 【系统已确认代言人】" + _spokesperson + "（来自标题/页面文字中的【XXX代言】标记，分析时必须使用此名字，禁止替换为其他名字或占位符）"

    schema = (
        '你是一位资深电商运营分析师，正在对一件真实的高销量商品做竞品拆解分析。你将看到该商品的【标题、主图、详情页截图、页面文本片段】。\n\n[核心原则] 根据商品实际情况分析，有什么写什么，没有的不编造！\n   ★ 如果该商品确实有明星/达人代言（标题或主图上有具体名字），请准确写出代言人姓名（优先从标题/主图文字提取，其次看面部特征判断）。\n   ★ 如果该商品没有明星代言（大部分商品都没有），则绝对不要提明星，转而分析它真正的高销量原因：产品卖点、价格策略、包装设计、口感/成分/功效、使用场景、品牌信任度等。\n   ★ 禁止把“明星代言”当成万能答案——泡面卖得好可能因为便宜好吃，洗面奶卖得好可能因为成分好，衣服卖得好可能因为版型好！\n\n═══ 禁止事项（违反即视为输出不合格）═══\n❌ 绝对禁止写：“信息有限”“基于可见内容推断”“可能”“推测”“大概”“或许”等任何推脱/模糊词汇\n❌ 绝对禁止空话套话：“精准定位”“视觉营销”“品质优良”“精准把握用户需求”“吸引眼球”“提升转化”（这些话放在任何商品上都成立，等于没说）\n❌ 绝对禁止无中生有：如果素材里没有明星、没有代言人、没有“XX推荐”字样，就不要写明星代言！\n❌ 绝对禁止套用示例中的占位符：示例里的名称都是示例，你必须根据当前商品的真实信息输出。\n✅ 每一条都必须包含该商品特有的、可验证的具体信息——要么是你从图片中看到的某个具体元素，要么是从标题/页面文字中提取的具体关键词/数据\n\n═══ 输出格式（严格 JSON，不要任何其他文字）══\n  【格式铁律】每个维度必须是「字符串数组」(JSON array of strings)，每条是一句独立的具体观察；不要把 a/b/c 各要点写成嵌套对象（如 {"a":...}），而是把每个要点分别作为数组里的一句独立字符串元素。\n\n  "summary": 高销量核心原因（2-3个具体因素，用“+”连接）。\n  要求：必须写出你从素材中观察到的具体因素，根据商品实际情况判断。\n  有代言人的例子：“蔡徐坤明星代言吸引年轻粉丝+玻尿酸成分主打深层清洁+抖音达人矩阵种草”\n  无代言人的例子：“大分量10袋装性价比高+刀削面非油炸更健康+今麦郎品牌国民认知度+抖音低价冲动消费场景”\n  坏例子：“信息有限，基于可见内容推断为KOL推荐+用户口碑传播”（太笼统，没有任何具体信息）\n\n  "main_image": 主图吸引力拆解(3-5条)。每条必须回答“这张图凭什么让人想点击？”\n  请从以下角度逐一检查并写出具体观察：\n  a) 人物/代言人：图里有没有人？有的话是谁（明星/达人/普通人）？什么姿态/表情？没有就跳过。\n  b) 产品展示：产品怎么拍的？特写/全景/使用场景/烹饪过程/成品图？大小和位置？\n  c) 色彩与背景：主色调是什么？背景干净还是丰富？色彩传递什么情绪？（暖色开胃？冷色专业？）\n  d) 文字信息：图上有没有叠加文字？写了什么？（促销标签？产品名？代言人名？）\n  e) 整体构图：视觉焦点在哪里？一眼看到什么？\n  好例子（食品类）：“主图居中展示一碗热气腾腾的面条成品图，红油色泽诱人激发食欲；左上角叠加‘10袋超值装’白色大字突出性价比；背景干净木质桌面突出食物本身”\n  好例子（美妆类）：“主图左侧明星半身像手持产品；产品居中放大展示质地特写；深色背景突出产品和人物”\n  坏例子：“主图采用专业构图，色彩搭配合理，吸引用户注意”（没有任何具体观察）\n\n  "title": 标题搜索策略拆解(3-5条)。每条必须指出标题中的具体词语或结构。\n  请检查：\n  a) 具体包含了哪些热搜词/高频搜索词？（逐个列出来）\n  b) 用了哪些痛点词或卖点词？（如“非油炸”“0脂”“大份量”“去黑头”“收缩毛孔”等——根据实际类目）\n  c) 用了哪些信任词/背书词？（如“医用级”“XX认证”“明星同款”“国民品牌”等）\n  d) 标题结构是怎样的？[品牌]+[核心卖点]+[适用人群]+[功效]？\n  e) 有没有用emoji/特殊符号增加点击率？\n  好例子：“标题包含两个高搜索量痛点词（如“去黑头”“收缩毛孔”）；前置品牌名建立认知；末尾适用人群词打消顾虑降低决策成本”\n  坏例子：“标题布局合理，关键词覆盖面广，有利于SEO优化”（没有指出任何一个具体词）\n\n  "sku": SKU设计(2-4条)。如果有SKU信息就具体分析，没有就写“当前素材未显示SKU信息”。\n\n  "detail": 详情页说服逻辑(3-5条)。重点分析“详情页怎么一步步说服用户下单”。\n  a) 首屏展示了什么？是不是直接击中目标用户的最大痛点？\n  b) 详情页的叙事顺序是什么？（痛点→产品→原理→效果→促销？）\n  c) 有没有对比图（使用前/后）？具体对比什么？\n  d) 有没有展示成分/参数/认证？具体是什么？\n  e) 有没有用户评价/好评截图？评价在强调什么？\n  f) 底部有什么促单手段？（限时/限量/赠品/保障承诺？）\n  好例子：“详情页首屏直接展示产品质地特写+核心成分表，立刻建立专业感；第3屏放入使用前后效果对比图，直观展示核心功效改善；底部放置销量数据截图+退换保障承诺”\n  坏例子：“详情页设计专业，逻辑清晰，有效传达产品价值”（没有具体描述任何一屏的内容）\n\n  "marketing": 营销推广痕迹(2-4条)。从素材中寻找营销和推广线索。\n  请检查（按优先级排序，有什么写什么）：\n  a) 代言人/达人：如果有明确的明星或达人代言，写出名字和身份。如果没有，不要编造！\n  b) 平台特征：抖音/淘宝/京东等平台的特色功能是否在使用？（短视频、直播、话题标签、达人推荐等）\n  c) 促销标识：是否有“限时”“特价”“秒杀”“买赠”等促销标签？\n  d) 其他：直播间截图、粉丝数、点赞数、活动标识等。\n  好例子（有代言）：“主图出镜者为蔡徐坤，青年歌手形象手持产品；配合抖音电商短视频+直播带货矩阵”\n  好例子（无代言）：“商品来自抖音电商平台，利用短视频场景化展示（面条烹饪过程）；标题突出‘超值装’主打性价比路线，匹配平台低价冲动消费人群”\n  坏例子：“推测有明星代言”（没有证据不要瞎猜）\n\n  "price_strategy": 价格策略(2-3条)。\n  如果能看到价格信息：具体价格是多少？与竞品相比如何？有没有促销/折扣？\n  如果看不到价格：写“当前素材未显示价格信息”。\n  好例子：“售价49.9元10袋，折合单袋约5元，低于线下超市均价（约8-12元/袋）；采用多规格装提升客单价”\n  坏例子：“价格具有竞争力”（没有写出具体价格或策略）\n\n  "social_proof": 信任构建线索(2-4条)。从素材中寻找建立消费者信任的元素。\n  请找：销量数字/排名、认证标志/质检报告、用户评价数量和内容、品牌背书/联名、退换货保障承诺、权威媒体/达人推荐等。\n  如果确实找不到，写“当前素材未发现明显信任构建痕迹”。\n  好例子：“月销10万+位列类目TOP3；展示SGS检测报告；评论区大量‘用了两周明显见效’真实反馈”\n  坏例子：“具有良好的社会信誉度”（空话）\n\n  "visual_style": 视觉风格与品类契合度(2-3条)。\n  分析整体视觉风格是否匹配商品定位和目标人群：\n  a) 风格定位：高端奢华/平价亲民/科技感/国潮风/家庭温馨等？\n  b) 配色方案：主色调是什么？为什么选这个颜色？（红色促食欲？蓝色显专业？绿色显健康？）\n  c) 与类目惯例对比：是跟随主流还是差异化？效果如何？\n  好例子（食品）：“采用暖橙色主色调激发食欲和购买欲；大号字体突出‘10袋装’分量感；风格定位平价亲民，匹配目标用户的实惠心理”\n  好例子（美妆）：“采用简洁黑白灰配色凸显高级感；产品特写占据画面70%突出核心成分技术；风格定位偏高端专业，建立成分党用户信任”\n  坏例子：“视觉风格符合产品定位”（没有说清楚到底什么风格、怎么符合的）\n\n═══ 一致性检查（输出前必做）═══\n① 各维度之间不能前后矛盾：如果main_image写了“明星A出镜”，title就不能写成“博主推荐”；如果main_image说“无人出镜”，marketing就不能写成“明星代言”；发现矛盾必须统一。\n② 图片文字检查：主图上是否有叠加文字？如果有，必须提取具体内容写入对应维度——图片上的文字是最直接证据！')
    has_text = bool(title) or bool(page_text)
    if main_b64:
        # 有主图 -> 视觉模型「真看」主图（标题文字一并喂进去），同步分析主图+标题；通常 40–70 秒
        try:
            img_fmt = detect_fmt(base64.b64decode(main_b64))[1]
        except Exception:
            img_fmt = "image/jpeg"
        data_url = "data:" + img_fmt + ";base64," + main_b64
        text = ("你是资深电商运营分析师。下面是一件高销量商品的【主图】，"
                "请分析它高销量的原因，重点拆：主图构图/配色/拍摄角度、标题关键词策略、SKU 矩阵设计、详情页叙事逻辑。\n"
                "商品信息：\n" + meta_txt + "\n\n" + schema)
        messages = [{"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": data_url}}
        ]}]
        content, err = _call_zhipu(key, "glm-4v-flash", messages)
    else:
        # 有标题/页面文本 → 用更快的文本模型（约 10–25 秒），避免视觉模型 40–70 秒的慢速
        text = ("你是资深电商运营分析师。下面是一件高销量商品的文字信息，"
                "请分析它高销量的原因，重点拆：主图套路、标题关键词策略、SKU 矩阵设计、详情页叙事逻辑。\n"
                "商品信息：\n" + meta_txt + "\n\n" + schema)
        messages = [{"role": "user", "content": text}]
        content, err = _call_zhipu(key, "glm-4-flash", messages)
    if err:
        return None, err
    obj = _extract_json(content)
    if not obj or not isinstance(obj, dict):
        return None, "模型未返回有效 JSON：" + str(content)[:200]
    result = {
        "summary": str(obj.get("summary") or "").strip(),
        "main_image": _as_list(obj.get("main_image")),
        "title": _as_list(obj.get("title")),
        "sku": _as_list(obj.get("sku")),
        "detail": _as_list(obj.get("detail")),
        "marketing": _as_list(obj.get("marketing")),
        "price_strategy": _as_list(obj.get("price_strategy")),
        "social_proof": _as_list(obj.get("social_proof")),
        "visual_style": _as_list(obj.get("visual_style")),
    }
    if not result["summary"] and not (result["main_image"] or result["title"] or result["sku"] or result["detail"]
                                       or result["marketing"] or result["price_strategy"] or result["social_proof"]
                                       or result["visual_style"]):
        return None, "模型返回内容为空"
    return result, None


# ─────────── 竞品分析后台任务（轮询模式，彻底解决 Render 超时杀连接） ───────────
# 架构：POST /api/analyze-competitor → 立即返回 job_id + 启后台线程跑分析
#       GET /api/analysis-status?job_id=xxx  → 每 2-3 秒短请求查状态（Render 杀不了短连接）
# 比 SSE 长连接更可靠：Render 免费版对 >10s 的长连接会超时断开，但 <1s 的 GET 没问题。
import uuid

_analysis_jobs = {}   # job_id -> {"status": "running"|"done"|"error", "progress": {...}, "result"?}
_jobs_lock = threading.Lock()

_JOB_TTL = 600  # 任务结果保留 10 分钟，过期自动清理


def _cleanup_jobs():
    """清理超过 TTL 的旧任务，防止内存泄漏。"""
    now = time.time()
    with _jobs_lock:
        expired = [jid for jid, j in _analysis_jobs.items()
                   if now - j.get("created_at", 0) > _JOB_TTL]
        for jid in expired:
            del _analysis_jobs[jid]


def _run_analysis_job(data, job_id):
    """在后台线程中执行完整分析流程，通过写 _analysis_jobs[job_id] 推进度和结果。
    复用 _run_stream_analyze 的 emit 逻辑，但 emit 写入 jobs 字典而非 SSE 流。"""
    def emit(obj):
        with _jobs_lock:
            job = _analysis_jobs.get(job_id)
            if not job:
                return
            job["progress"] = obj
            if obj.get("stage") == "done":
                job["status"] = "done"
                job["result"] = obj.get("result")
            elif obj.get("stage") == "error":
                job["status"] = "error"
                job["error_msg"] = obj.get("msg", "未知错误")

    try:
        _run_stream_analyze(data, emit)
    except Exception as e:
        emit({"stage": "error", "msg": "分析异常：" + str(e)})
def _run_stream_analyze(data, emit):
    """emit(dict) 推进度事件：{stage, pct, msg, elapsed?, result?}。"""
    key = (data.get("zhitu_key") or ZHIPU_API_KEY).strip()
    if not key:
        emit({"stage": "error", "msg": "未配置 ZHIPU_API_KEY（请在运行 proxy.py 的环境里设置智谱 API Key，与 model-eyes 同源）"})
        return

    title = (data.get("title") or "").strip()
    category = (data.get("category") or "").strip()
    platform = (data.get("platform") or "").strip()
    price = (data.get("price") or "").strip()
    url = (data.get("url") or "").strip()
    page_text = (data.get("page_text") or "").strip()

    meta = []
    if title: meta.append("商品标题：" + title)
    if category: meta.append("类目：" + category)
    if platform: meta.append("平台：" + platform)
    if price: meta.append("价格区间：" + price)
    if url: meta.append("商品链接：" + url)
    if page_text: meta.append("【页面文本片段（用于推断真实标题/卖点；若上方未给标题，请据此提取）】\n" + page_text)
    meta_txt = "\n".join(meta) if meta else "（未提供文字信息，仅凭主图+详情图分析）"
    # 预提取代言人：从标题/页面文字中用正则匹配【XXX代言】【XXX推荐】【XXX同款】    _spokesperson = ""    import re as _re    for _src in filter(None, [title, page_text]):        _m = _re.search(r"【([^】]{2,8})(?:代言|推荐|同款|助力|站台)】", _src)        if _m:            _spokesperson = _m.group(1).strip()            break    if _spokesperson:        meta_txt += "\n\n⚠️ 【系统已确认代言人】" + _spokesperson + "（来自标题/页面文字中的【XXX代言】标记，分析时必须使用此名字，禁止替换为其他名字或占位符）"

    schema = (
        '你是一位资深电商运营分析师，正在对一件真实的高销量商品做竞品拆解分析。你将看到该商品的【标题、主图、详情页截图、页面文本片段】。\n\n[核心原则] 根据商品实际情况分析，有什么写什么，没有的不编造！\n   ★ 如果该商品确实有明星/达人代言（标题或主图上有具体名字），请准确写出代言人姓名（优先从标题/主图文字提取，其次看面部特征判断）。\n   ★ 如果该商品没有明星代言（大部分商品都没有），则绝对不要提明星，转而分析它真正的高销量原因：产品卖点、价格策略、包装设计、口感/成分/功效、使用场景、品牌信任度等。\n   ★ 禁止把“明星代言”当成万能答案——泡面卖得好可能因为便宜好吃，洗面奶卖得好可能因为成分好，衣服卖得好可能因为版型好！\n\n═══ 禁止事项（违反即视为输出不合格）═══\n❌ 绝对禁止写：“信息有限”“基于可见内容推断”“可能”“推测”“大概”“或许”等任何推脱/模糊词汇\n❌ 绝对禁止空话套话：“精准定位”“视觉营销”“品质优良”“精准把握用户需求”“吸引眼球”“提升转化”（这些话放在任何商品上都成立，等于没说）\n❌ 绝对禁止无中生有：如果素材里没有明星、没有代言人、没有“XX推荐”字样，就不要写明星代言！\n❌ 绝对禁止套用示例中的占位符：示例里的名称都是示例，你必须根据当前商品的真实信息输出。\n✅ 每一条都必须包含该商品特有的、可验证的具体信息——要么是你从图片中看到的某个具体元素，要么是从标题/页面文字中提取的具体关键词/数据\n\n═══ 输出格式（严格 JSON，不要任何其他文字）══\n  【格式铁律】每个维度必须是「字符串数组」(JSON array of strings)，每条是一句独立的具体观察；不要把 a/b/c 各要点写成嵌套对象（如 {"a":...}），而是把每个要点分别作为数组里的一句独立字符串元素。\n\n  "summary": 高销量核心原因（2-3个具体因素，用“+”连接）。\n  要求：必须写出你从素材中观察到的具体因素，根据商品实际情况判断。\n  有代言人的例子：“蔡徐坤明星代言吸引年轻粉丝+玻尿酸成分主打深层清洁+抖音达人矩阵种草”\n  无代言人的例子：“大分量10袋装性价比高+刀削面非油炸更健康+今麦郎品牌国民认知度+抖音低价冲动消费场景”\n  坏例子：“信息有限，基于可见内容推断为KOL推荐+用户口碑传播”（太笼统，没有任何具体信息）\n\n  "main_image": 主图吸引力拆解(3-5条)。每条必须回答“这张图凭什么让人想点击？”\n  请从以下角度逐一检查并写出具体观察：\n  a) 人物/代言人：图里有没有人？有的话是谁（明星/达人/普通人）？什么姿态/表情？没有就跳过。\n  b) 产品展示：产品怎么拍的？特写/全景/使用场景/烹饪过程/成品图？大小和位置？\n  c) 色彩与背景：主色调是什么？背景干净还是丰富？色彩传递什么情绪？（暖色开胃？冷色专业？）\n  d) 文字信息：图上有没有叠加文字？写了什么？（促销标签？产品名？代言人名？）\n  e) 整体构图：视觉焦点在哪里？一眼看到什么？\n  好例子（食品类）：“主图居中展示一碗热气腾腾的面条成品图，红油色泽诱人激发食欲；左上角叠加‘10袋超值装’白色大字突出性价比；背景干净木质桌面突出食物本身”\n  好例子（美妆类）：“主图左侧明星半身像手持产品；产品居中放大展示质地特写；深色背景突出产品和人物”\n  坏例子：“主图采用专业构图，色彩搭配合理，吸引用户注意”（没有任何具体观察）\n\n  "title": 标题搜索策略拆解(3-5条)。每条必须指出标题中的具体词语或结构。\n  请检查：\n  a) 具体包含了哪些热搜词/高频搜索词？（逐个列出来）\n  b) 用了哪些痛点词或卖点词？（如“非油炸”“0脂”“大份量”“去黑头”“收缩毛孔”等——根据实际类目）\n  c) 用了哪些信任词/背书词？（如“医用级”“XX认证”“明星同款”“国民品牌”等）\n  d) 标题结构是怎样的？[品牌]+[核心卖点]+[适用人群]+[功效]？\n  e) 有没有用emoji/特殊符号增加点击率？\n  好例子：“标题包含两个高搜索量痛点词（如“去黑头”“收缩毛孔”）；前置品牌名建立认知；末尾适用人群词打消顾虑降低决策成本”\n  坏例子：“标题布局合理，关键词覆盖面广，有利于SEO优化”（没有指出任何一个具体词）\n\n  "sku": SKU设计(2-4条)。如果有SKU信息就具体分析，没有就写“当前素材未显示SKU信息”。\n\n  "detail": 详情页说服逻辑(3-5条)。重点分析“详情页怎么一步步说服用户下单”。\n  a) 首屏展示了什么？是不是直接击中目标用户的最大痛点？\n  b) 详情页的叙事顺序是什么？（痛点→产品→原理→效果→促销？）\n  c) 有没有对比图（使用前/后）？具体对比什么？\n  d) 有没有展示成分/参数/认证？具体是什么？\n  e) 有没有用户评价/好评截图？评价在强调什么？\n  f) 底部有什么促单手段？（限时/限量/赠品/保障承诺？）\n  好例子：“详情页首屏直接展示产品质地特写+核心成分表，立刻建立专业感；第3屏放入使用前后效果对比图，直观展示核心功效改善；底部放置销量数据截图+退换保障承诺”\n  坏例子：“详情页设计专业，逻辑清晰，有效传达产品价值”（没有具体描述任何一屏的内容）\n\n  "marketing": 营销推广痕迹(2-4条)。从素材中寻找营销和推广线索。\n  请检查（按优先级排序，有什么写什么）：\n  a) 代言人/达人：如果有明确的明星或达人代言，写出名字和身份。如果没有，不要编造！\n  b) 平台特征：抖音/淘宝/京东等平台的特色功能是否在使用？（短视频、直播、话题标签、达人推荐等）\n  c) 促销标识：是否有“限时”“特价”“秒杀”“买赠”等促销标签？\n  d) 其他：直播间截图、粉丝数、点赞数、活动标识等。\n  好例子（有代言）：“主图出镜者为蔡徐坤，青年歌手形象手持产品；配合抖音电商短视频+直播带货矩阵”\n  好例子（无代言）：“商品来自抖音电商平台，利用短视频场景化展示（面条烹饪过程）；标题突出‘超值装’主打性价比路线，匹配平台低价冲动消费人群”\n  坏例子：“推测有明星代言”（没有证据不要瞎猜）\n\n  "price_strategy": 价格策略(2-3条)。\n  如果能看到价格信息：具体价格是多少？与竞品相比如何？有没有促销/折扣？\n  如果看不到价格：写“当前素材未显示价格信息”。\n  好例子：“售价49.9元10袋，折合单袋约5元，低于线下超市均价（约8-12元/袋）；采用多规格装提升客单价”\n  坏例子：“价格具有竞争力”（没有写出具体价格或策略）\n\n  "social_proof": 信任构建线索(2-4条)。从素材中寻找建立消费者信任的元素。\n  请找：销量数字/排名、认证标志/质检报告、用户评价数量和内容、品牌背书/联名、退换货保障承诺、权威媒体/达人推荐等。\n  如果确实找不到，写“当前素材未发现明显信任构建痕迹”。\n  好例子：“月销10万+位列类目TOP3；展示SGS检测报告；评论区大量‘用了两周明显见效’真实反馈”\n  坏例子：“具有良好的社会信誉度”（空话）\n\n  "visual_style": 视觉风格与品类契合度(2-3条)。\n  分析整体视觉风格是否匹配商品定位和目标人群：\n  a) 风格定位：高端奢华/平价亲民/科技感/国潮风/家庭温馨等？\n  b) 配色方案：主色调是什么？为什么选这个颜色？（红色促食欲？蓝色显专业？绿色显健康？）\n  c) 与类目惯例对比：是跟随主流还是差异化？效果如何？\n  好例子（食品）：“采用暖橙色主色调激发食欲和购买欲；大号字体突出‘10袋装’分量感；风格定位平价亲民，匹配目标用户的实惠心理”\n  好例子（美妆）：“采用简洁黑白灰配色凸显高级感；产品特写占据画面70%突出核心成分技术；风格定位偏高端专业，建立成分党用户信任”\n  坏例子：“视觉风格符合产品定位”（没有说清楚到底什么风格、怎么符合的）\n\n═══ 一致性检查（输出前必做）═══\n① 各维度之间不能前后矛盾：如果main_image写了“明星A出镜”，title就不能写成“博主推荐”；如果main_image说“无人出镜”，marketing就不能写成“明星代言”；发现矛盾必须统一。\n② 图片文字检查：主图上是否有叠加文字？如果有，必须提取具体内容写入对应维度——图片上的文字是最直接证据！')
    # 收集视觉素材：主图(data URL) + 详情页图(URL 列表，分析阶段并行下载)
    main_b64 = data.get("main_image") or data.get("main_image_b64") or None
    detail_urls = data.get("detail_image_urls") or []
    if not isinstance(detail_urls, list):
        detail_urls = []
    detail_urls = [u for u in detail_urls if isinstance(u, str) and u.startswith(("http://", "https://", "//"))][:3]

    emit({"stage": "prepare", "pct": 5, "msg": "已接收商品信息，准备同步分析 标题 + 主图 + 详情页…"})

    images = []  # 元素为 (fmt, pure_b64)
    if main_b64:
        fmt, b64 = _to_b64_and_fmt(main_b64)
        if fmt and b64:
            images.append((fmt, b64))

    total = len(detail_urls)
    if total:
        emit({"stage": "download", "pct": 15, "msg": "正在下载主图 + %d 张详情页图…" % total})
        done = [0]
        # 下载阶段也启动心跳（每 3s），防止 Render/代理在多图并行下载时因无数据而杀连接
        dl_stop = threading.Event()
        def _dl_beat():
            c = 0
            while not dl_stop.wait(3):
                c += 3
                emit({"stage": "download", "pct": min(35, 15 + int(20 * done[0] / max(1, total))),
                      "msg": "下载中… 已等待 %d 秒 (%d/%d)" % (c, done[0], total)})
        dl_t = threading.Thread(target=_dl_beat, daemon=True)
        dl_t.start()
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_download_image_data_url, u, _IMG_HEADERS, 5): u for u in detail_urls}
            for fu in as_completed(futs):
                du = fu.result()
                if du:
                    fmt, b64 = _to_b64_and_fmt(du)
                    if fmt and b64:
                        images.append((fmt, b64))
                done[0] += 1
                emit({"stage": "download", "pct": 15 + int(20 * done[0] / max(1, total)),
                      "msg": "已下载主图 + 详情图 %d/%d" % (done[0], total)})
        dl_stop.set()
        dl_t.join()

    has_img = len(images) > 0

    if has_img:
        # 有图：视觉模型「真看」主图+详情图，标题文字一并喂进去 -> 同步分析三者
        vision_text = ("你是资深电商运营分析师。下面是一件高销量商品落地页的【标题文字 + 主图 + 详情页图】，请综合分析它高销量的原因。\n"
                       "请重点拆解三块，并分别落到对应输出字段：\n"
                       "1) 标题关键词布局/搜索命中/卖点排序 -> title 字段；\n"
                       "2) 主图构图/配色/拍摄角度/留白/光影 -> main_image 字段；\n"
                       "3) 详情页首屏信任/叙事顺序/对比图/促单逻辑/卖点图设计 -> detail 字段。\n"
                       "商品信息：\n" + meta_txt + "\n\n" + schema)
        content = [{"type": "text", "text": vision_text}]
        for fmt, b64 in images:
            content.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (fmt, b64)}})
        messages = [{"role": "user", "content": content}]
        model = "glm-4v-flash"
    else:
        # 完全没图才退回文本模型
        text = ("你是资深电商运营分析师。下面是一件高销量商品的文字信息（未抓到主图/详情图），"
                "请分析它高销量的原因，重点拆：主图套路、标题关键词策略、SKU 矩阵设计、详情页叙事逻辑。\n"
                "商品信息：\n" + meta_txt + "\n\n" + schema)
        messages = [{"role": "user", "content": text}]
        model = "glm-4-flash"

    emit({"stage": "analyze", "pct": 40, "msg": "🔍 AI 正在同步分析 标题 + 主图 + 详情页…"})
    # 心跳：分析期间每 3 秒推一次已等待秒数，让进度条真实流动，不再像卡死
    stop = threading.Event()
    elapsed = [0]
    def _beat():
        while not stop.wait(3):
            elapsed[0] += 3
            emit({"stage": "analyze", "pct": min(92, 45 + elapsed[0] * 1.2),
                  "elapsed": elapsed[0], "msg": "🔍 AI 深度分析中… 已等待 %d 秒" % elapsed[0]})
    beat = threading.Thread(target=_beat, daemon=True)
    beat.start()
    content, err = _call_zhipu(key, model, messages, timeout=60)
    stop.set()
    beat.join()

    if err:
        emit({"stage": "error", "msg": err})
        return
    obj = _extract_json(content)
    if not obj or not isinstance(obj, dict):
        emit({"stage": "error", "msg": "模型未返回有效 JSON：" + str(content)[:200]})
        return
    result = {
        "summary": str(obj.get("summary") or "").strip(),
        "main_image": _as_list(obj.get("main_image")),
        "title": _as_list(obj.get("title")),
        "sku": _as_list(obj.get("sku")),
        "detail": _as_list(obj.get("detail")),
        "marketing": _as_list(obj.get("marketing")),
        "price_strategy": _as_list(obj.get("price_strategy")),
        "social_proof": _as_list(obj.get("social_proof")),
        "visual_style": _as_list(obj.get("visual_style")),
    }
    if not result["summary"] and not (result["main_image"] or result["title"] or result["sku"] or result["detail"]
                                       or result["marketing"] or result["price_strategy"] or result["social_proof"]
                                       or result["visual_style"]):
        emit({"stage": "error", "msg": "模型返回内容为空"})
        return
    emit({"stage": "done", "pct": 100, "result": result})


# ─────────── 商品链接自动抓取（服务端代抓，绕过浏览器 CORS/反爬） ───────────
# 只贴链接 -> 自动识别平台、抓标题/主图/价格，能抓多少抓多少；抓不到也不阻塞，前端可手动补。
def _decode_html(r):
    """稳健解码商品页 HTML：优先用响应声明的 charset，否则依次尝试 utf-8 / apparent / GBK/GB18030。
    避免 requests 默认按 Latin-1 误解码中文页面，导致标题/描述全变乱码、AI 也读不懂。"""
    ct = r.headers.get("Content-Type", "")
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    encs = []
    if m:
        encs.append(m.group(1).strip().lower())
    encs += ["utf-8", (r.apparent_encoding or "gbk").lower(), "gbk", "gb18030"]
    for e in encs:
        try:
            return r.content.decode(e)
        except Exception:
            continue
    return r.content.decode("utf-8", errors="replace")


def _detect_platform_from_url(url):
    u = (url or "").lower()
    if "taobao.com" in u or "tmall.com" in u:
        return "taobao"
    if "jd.com" in u or "jingdong.com" in u:
        return "jd"
    if "pinduoduo.com" in u or "pdd.com" in u or "yangkeduo" in u:
        return "pdd"
    if "douyin.com" in u or "iesdouyin.com" in u or "tiktok.com" in u:
        return "douyin"
    if "1688.com" in u:
        return "1688"
    if "amazon" in u:
        return "amazon"
    return "other"


def _download_image_data_url(url, headers, timeout=8):
    """把图片 URL 下载成 data URL；失败返回 None。用于分析时并行抓取主图/详情图。"""
    try:
        if url.startswith("//"):
            url = "https:" + url
        ir = S.get(url, headers=headers, timeout=timeout)
        if ir.status_code == 200 and ir.content:
            fmt = detect_fmt(ir.content)[1]
            return "data:%s;base64,%s" % (fmt, base64.b64encode(ir.content).decode("ascii"))
    except Exception:
        return None
    return None


_IMG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.google.com/",
}


def _to_b64_and_fmt(v):
    """把 data URL 或裸 base64 转成 (mime, pure_b64)。"""
    if not v:
        return None, None
    if "," in v:
        hdr, b = v.split(",", 1)
        fmt = "image/png"
        mm = re.match(r"data:([\w/+]+)", hdr)
        if mm:
            fmt = mm.group(1)
        return fmt, b
    try:
        raw = base64.b64decode(v)
    except Exception:
        return None, None
    return detect_fmt(raw)[1], v


def _extract_detail_images(html, base_url):
    """从落地页 HTML 里尽量挖出「详情页/描述图」URL（落地页链接点进去就是商品详情页，详情图是 AI 分析重点）。
    优先取懒加载属性(data-src/data-original/data-lazy-src/data-actualsrc)，过滤图标/logo/1x1/动图，最多 6 张。"""
    cands = []
    for m in re.finditer(r"<img\b[^>]*>", html, re.I):
        tag = m.group(0)
        src = None
        for attr in ("data-src", "data-original", "data-lazy-src", "data-actualsrc", "src"):
            am = re.search(r"%s\s*=\s*[\"']([^\"']+)" % attr, tag, re.I)
            if am and am.group(1).strip():
                src = am.group(1).strip()
                break
        if not src:
            continue
        s = src.lower()
        if s.startswith("data:"):
            continue
        if any(k in s for k in ("icon", "logo", "avatar", "sprite", "tracker",
                                 "pixel", "blank", "btn", "arrow", "emoji", ".svg", ".gif", "1x1")):
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            try:
                src = urljoin(base_url, src)
            except Exception:
                continue
        elif src.startswith("http"):
            pass
        else:
            continue
        score = 0
        if any(k in s for k in ("detail", "desc", "product", "item", "goods",
                                 "content", "banner", "descimg", "album")):
            score += 3
        if re.search(r"[_\-](\d{3,4})[_\-x]", s):  # 大图文件名常带尺寸
            score += 1
        cands.append((score, src))
    seen = set()
    out = []
    for sc, u in sorted(cands, key=lambda x: -x[0]):
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out[:6]


def fetch_product(url):
    if not url or not url.startswith(("http://", "https://")):
        return None, "链接格式不正确（需以 http/https 开头）"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        r = S.get(url, headers=headers, timeout=15, allow_redirects=True)
    except Exception as e:
        return None, "抓取商品页失败：" + str(e)
    if r.status_code != 200:
        return None, "抓取商品页返回 %d" % r.status_code
    html = _decode_html(r)

    def _meta(prop):
        m = re.search(r'<meta[^>]+property=["\']%s["\'][^>]+content=["\']([^"\']+)' % re.escape(prop), html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+name=["\']%s["\'][^>]+content=["\']([^"\']+)' % re.escape(prop), html, re.I)
        return m.group(1).strip() if m else ""

    def _ld_name():
        """从 JSON-LD 结构化数据里抽 name/headline（很多电商页的商品名藏在这里）。"""
        names = []
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            raw = m.group(1).strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                # 个别站点 JSON 不标准，粗取 "name":"..." / "headline":"..."
                for nm in re.findall(r'"(?:name|headline|title)"\s*:\s*"([^"]{2,120})"', raw):
                    names.append(nm)
                continue
            def walk(o):
                if isinstance(o, dict):
                    for k in ("name", "headline", "title"):
                        v = o.get(k)
                        if isinstance(v, str) and v.strip():
                            names.append(v.strip())
                    ile = o.get("itemListElement")
                    if isinstance(ile, list):
                        for it in ile:
                            if isinstance(it, dict) and isinstance(it.get("name"), str):
                                names.append(it["name"].strip())
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(obj)
        # 去重保序
        seen = set(); out = []
        for n in names:
            if n not in seen:
                seen.add(n); out.append(n)
        return out

    # 抖音/抖音电商短链（v.douyin.com 跳转到 haohuo.jinritemai.com）：SPA 静态 HTML 没有标题，
    # 但实际标题藏在 URL 的 goods_detail 参数里（JSON 字符串，外层+内层都是 URL 编码）
    _douyin_title = ""
    if "douyin.com" in url or "jinritemai.com" in url:
        try:
            from urllib.parse import urlparse, parse_qs, unquote
            _qs = parse_qs(urlparse(url).query)
            _gd = _qs.get("goods_detail", [""])[0]
            if _gd:
                _decoded = unquote(unquote(_gd))
                import json as _json
                _dj = _json.loads(_decoded)
                _douyin_title = (_dj.get("title") or "").strip()
        except Exception:
            pass

    title = (_meta("og:title") or _meta("twitter:title") or _douyin_title or "").strip()
    if not title:
        ld = _ld_name()
        if ld:
            title = ld[0]
    if not title:
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        if m:
            title = m.group(1).strip()
    # 清理：去掉常见的站点后缀，避免把「- 淘宝网」当成商品名
    if title:
        title = re.split(r"\s*[-|–·]\s*(?:淘宝网|淘宝|天猫|京东|JD|京东商城|拼多多|抖音|Amazon|亚马逊|1688|唯品会|苏宁易购|苏宁)\s*$", title)[0].strip()
    # ── 主图提取（优先级：meta 标签 > JSON-LD > 内联脚本 > 首张大图） ──
    img_url = ""
    for prop in ("og:image:secure_url", "og:image", "twitter:image", "image"):
        v = _meta(prop)
        if v:
            img_url = v.strip()
            break

    # 备选 1：从 JSON-LD 的 image 字段提取（抖音/淘宝等常把商品主图藏在这里）
    if not img_url:
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            raw = m.group(1).strip()
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            def _walk_img(o):
                if isinstance(o, dict):
                    # Product/image 字段
                    for k in ("image", "thumbnailUrl", "photo", "logo"):
                        v = o.get(k)
                        if isinstance(v, str) and v.startswith(("http://", "https://", "//")):
                            return v.strip()
                        if isinstance(v, list) and v and isinstance(v[0], str) and v[0].startswith(("http://", "https://", "//")):
                            return v[0].strip()
                    for val in o.values():
                        r = _walk_img(val)
                        if r: return r
                elif isinstance(o, list):
                    for item in o:
                        r = _walk_img(item)
                        if r: return r
                return None
            found = _walk_img(obj)
            if found:
                img_url = found
                break

    # 备选 2：从 SPA 内联脚本数据中提取（抖音 __pace_f__ / RENDER_DATA / NEXT_DATA 等）
    if not img_url:
        # 抖音商品页常见：window.__RENDER_DATA__ 或 <script> 里的 productImage / cover 图片
        patterns = [
            r'"(?:productImage|cover|thumb|mainImage|imageUrl|image)"\s*:\s*"((?:https?:)?//[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
            r'"(?:img|pic|src)"\s*:\s*"((?:https?:)?//[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
            r'(?:cover|productImage|mainImg)\s*[=:]\s*["\']((?:https?:)?//[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.I)
            if m:
                candidate = m.group(1).strip()
                # 过滤掉明显不是主图的 URL（图标、logo、1x1 像素等）
                if not any(x in candidate.lower() for x in ("icon", "logo", "avatar", "1x1", "placeholder")):
                    img_url = candidate
                    break

    # 备选 3：页面中第一张面积较大的 <img>（排除 icon/logo/追踪像素）
    if not img_url:
        img_candidates = []
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I):
            src = m.group(1).strip()
            if src.startswith(("http://", "https://", "//")):
                # 简单启发：URL 含产品相关关键词或图片尺寸较大
                w_match = re.search(r'width[=:]["\']?(\d{3,})', m.group(0), re.I)
                h_match = re.search(r'height[=:]["\']?(\d{3,})', m.group(0), re.I)
                score = 0
                if w_match: score += int(w_match.group(1))
                if h_match: score += int(h_match.group(1))
                if any(k in src.lower() for k in ("product", "goods", "item", "cover", "main", "thumb")):
                    score += 300
                if not any(k in src.lower() for k in ("icon", "logo", "avatar", "pixel", "tracking", "beacon")):
                    score += 100
                img_candidates.append((score, src))
        if img_candidates:
            img_candidates.sort(key=lambda x: -x[0])
            img_url = img_candidates[0][1]

    # 备选 4：oEmbed 接口（抖音/淘宝/京东等平台支持，返回缩略图 URL）
    if not img_url:
        # 常见平台的 oEmbed 端点
        oembed_endpoints = []
        if "douyin.com" in url or "iesdouyin.com" in url:
            oembed_endpoints.append("https://www.douyin.com/oembed?url=" + url)
            oembed_endpoints.append("https://www.iesdouyin.com/share/oembed?url=" + url)
        elif "haohuo.jinritemai.com" in url:
            oembed_endpoints.append("https://haohuo.jinritemai.com/oembed?url=" + url)
        elif "taobao.com" in url or "tmall.com" in url:
            oembed_endpoints.append("https://www.taobao.com/oembed?url=" + url)
            oembed_endpoints.append("https://libs.wres.cn/wapi/oembed?url=" + url)
        for oe_url in oembed_endpoints[:2]:  # 最多试 2 个端点，控制耗时
            try:
                oe_r = S.get(oe_url, headers=headers, timeout=5)
                if oe_r.status_code == 200:
                    oe_data = oe_r.json()
                    thumb = (oe_data.get("thumbnail_url") or oe_data.get("thumbnailUrl")
                             or oe_data.get("image") or "").strip()
                    if thumb and thumb.startswith(("http://", "https://", "//")):
                        img_url = thumb
                        break
            except Exception:
                continue

    # 备选 5：公共 meta 抓取服务（用第三方 API 获取页面的 OG 图片）
    if not img_url:
        # 使用免费的公共 meta 抓取 API（作为最后兜底，超时严格限制）
        meta_apis = [
            "https://api.microlink.io/?url=" + url,
            "https://jsonlink.io/api/extract?url=" + url,
        ]
        for api_url in meta_apis[:1]:  # 只试 1 个，控制总耗时
            try:
                mr = S.get(api_url, headers=headers, timeout=6)
                if mr.status_code == 200:
                    md = mr.json()
                    # microlink 格式: { image: { url: "..."} }
                    # jsonlink 格式: { images: ["..."] }
                    thumb = ""
                    if isinstance(md.get("image"), dict):
                        thumb = md["image"].get("url", "")
                    elif isinstance(md.get("images"), list) and md["images"]:
                        thumb = md["images"][0]
                    elif isinstance(md.get("image"), str):
                        thumb = md["image"]
                    if thumb and thumb.startswith(("http://", "https://", "//")):
                        img_url = thumb
                        break
            except Exception:
                continue
    price = _meta("og:price:amount") or _meta("product:price:amount")
    if not price:
        mp = re.search(r'itemprop=["\']price["\'][^>]+content=["\']([^"\']+)', html, re.I)
        if mp:
            price = mp.group(1).strip()
    else:
        price = price.strip()
    if not price:
        mp = re.search(r'["\']price["\']\s*:\s*["\']?([\d.]+)', html)
        if mp:
            price = mp.group(1).strip()
    platform = _detect_platform_from_url(url)

    # 拼一段「页面文本片段」给 AI 推断真实标题/卖点（反爬页静态 HTML 里常仍能挖到线索）
    desc = (_meta("og:description") or _meta("description") or _meta("twitter:description") or "").strip()
    h1m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    h1 = h1m.group(1).strip() if h1m else ""
    ld = _ld_name()
    bits = []
    if title:
        bits.append("页面标题: " + title)
    if desc:
        bits.append("描述: " + desc)
    if h1:
        bits.append("H1: " + h1)
    if ld:
        bits.append("结构化名: " + " | ".join(ld[:6]))
    if url:
        bits.append("链接: " + url)
    page_text = "\n".join(bits)[:1500]

    # 详情页图：落地页链接点进去就是商品详情页，详情图是 AI 同步分析的重点，尽量挖出来（仅返回 URL，下载放在分析阶段并行做并实时反馈进度）
    detail_urls = _extract_detail_images(html, url)

    image_data_url = None
    if img_url:
        try:
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            # 根据图片域名选择正确的 Referer，绕过防盗链
            img_headers = dict(headers)
            if "douyin" in img_url or "jinritemai" in img_url or "pstatp" in img_url:
                img_headers["Referer"] = "https://haohuo.jinritemai.com/"
            elif "taobao" in img_url or "tbcdn" in img_url or "alicdn" in img_url:
                img_headers["Referer"] = "https://www.taobao.com/"
            elif "jd" in img_url or "360buy" in img_url:
                img_headers["Referer"] = "https://item.jd.com/"
            elif "pinduoduo" in img_url or "yangkeduo" in img_url:
                img_headers["Referer"] = "https://mobile.yangkeduo.com/"
            else:
                img_headers["Referer"] = url
            # 图片下载仅作可选补充，超时压到 8s，避免拖慢整体抓取
            ir = S.get(img_url, headers=img_headers, timeout=8)
            if ir.status_code == 200 and ir.content:
                # 过滤掉过小的图片（可能是 icon 或占位图，< 2KB）
                if len(ir.content) > 2048:
                    fmt = detect_fmt(ir.content)[1]
                    image_data_url = "data:%s;base64,%s" % (fmt, base64.b64encode(ir.content).decode("ascii"))
        except Exception:
            image_data_url = None

    return {
        "title": title,
        "image_data_url": image_data_url,
        "detail_image_urls": detail_urls,
        "price": price,
        "platform": platform,
        "page_text": page_text,
        "note": "已尽力自动抓取；部分平台（淘宝/拼多多等）反爬较强，可能只拿到部分信息，可手动补。",
    }, None


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
            # 门禁/工具页禁止浏览器缓存：避免用户端长期缓存旧版 standalone.html，
            # 导致「点解锁没反应」等旧 bug 复现（旧页面门禁逻辑可能被改过）。
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
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
        if self.path.startswith("/api/analysis-status"):
            self._handle_analysis_status()
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
        if self.path == "/api/analyze-competitor":
            self._handle_analyze()
            return
        if self.path == "/api/fetch-product":
            self._handle_fetch_product()
            return
        if self.path == "/api/analyze-competitor-stream":
            self._handle_analyze_stream()
            return
        if self.path == "/api/prompt-gen":
            self._handle_prompt_gen()
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

    def _handle_analyze(self):
        # 竞品智能分析（轮询模式）：立即返回 job_id，后台线程跑分析。
        # 前端每 2-3 秒 GET /api/analysis-status 查状态，彻底避免 Render 超时杀长连接。
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln)
            data = json.loads(body or b"{}")
        except Exception:
            self._send_json(400, {"error": "请求解析失败"})
            return
        token = (self.headers.get("Authorization") or "").replace("Bearer ", "", 1).strip()
        if not token:
            token = (self.headers.get("X-Access-Token") or "").strip()
        ok, info, kind = resolve_credential(token)
        if not ok:
            self._send_json(401, {"error": "访问码无效或已过期，请先在页面输入正确的动态访问码。（" + info.get("error", "") + "）"})
            return

        # 清理过期任务
        _cleanup_jobs()

        job_id = "aj_" + uuid.uuid4().hex[:12]
        with _jobs_lock:
            _analysis_jobs[job_id] = {
                "status": "running",
                "progress": {"stage": "prepare", "pct": 5, "msg": "任务已创建，正在启动分析…"},
                "created_at": time.time(),
            }

        # 启动后台线程执行分析
        t = threading.Thread(target=_run_analysis_job, args=(data, job_id), daemon=True)
        t.start()

        # 立即返回 job_id（不等待分析完成）
        self._send_json(202, {"job_id": job_id, "msg": "分析任务已启动，请用 job_id 轮询 /api/analysis-status 查进度"})

    def _handle_analysis_status(self):
        """GET /api/analysis-status?job_id=xxx → 返回任务当前状态/进度/结果。短请求，不会被 Render 超时杀。"""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        job_id = (params.get("job_id") or [""])[0].strip()
        if not job_id:
            self._send_json(400, {"error": "缺少 job_id 参数"})
            return
        with _jobs_lock:
            job = _analysis_jobs.get(job_id)
        if not job:
            self._send_json(404, {"error": "任务不存在或已过期（结果保留 10 分钟）", "status": "expired"})
            return
        resp = {
            "status": job["status"],
            "progress": job.get("progress", {}),
        }
        if job["status"] == "done" and "result" in job:
            resp["result"] = job["result"]
        elif job["status"] == "error":
            resp["error"] = job.get("error_msg", "未知错误")
        self._send_json(200, resp)

    def _handle_analyze_stream(self):
        # 竞品分析实时进度流（SSE）：服务端并行下载主图+详情图，并推进度事件，前端进度条吃真实事件。
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln)
            data = json.loads(body or b"{}")
        except Exception:
            self._send_json(400, {"error": "请求解析失败"})
            return
        token = (self.headers.get("Authorization") or "").replace("Bearer ", "", 1).strip()
        if not token:
            token = (self.headers.get("X-Access-Token") or "").strip()
        ok, info, kind = resolve_credential(token)
        if not ok:
            self._send_json(401, {"error": "访问码无效或已过期，请先在页面输入正确的动态访问码。（" + info.get("error", "") + "）"})
            return
        # SSE 头：禁用代理缓冲（Render/nginx 会缓冲导致前端收不到实时进度），保持长连接流式推送
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        wfile = self.wfile

        def emit(obj):
            try:
                wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                wfile.flush()
            except Exception:
                pass

        try:
            _run_stream_analyze(data, emit)
        except Exception as e:
            emit({"stage": "error", "msg": "分析失败：" + str(e)})

    def _handle_prompt_gen(self):
        """AI 反推生图提示词：上传产品白底图 + 卖点 → GLM-4V 分析产品 → GLM-4 生成多条提示词"""
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln)
            data = json.loads(body or b"{}")
        except Exception:
            self._send_json(400, {"error": "请求解析失败"})
            return
        token = (self.headers.get("Authorization") or "").replace("Bearer ", "", 1).strip()
        if not token:
            token = (self.headers.get("X-Access-Token") or "").strip()
        ok, info, kind = resolve_credential(token)
        if not ok:
            self._send_json(401, {"error": "访问码无效或已过期，请先输入正确的动态访问码。（" + info.get("error", "") + "）"})
            return

        image_b64 = data.get("image_b64") or ""
        selling_points = (data.get("selling_points") or "").strip()
        styles = data.get("styles") or ["详情页主图", "海报", "种草文", "场景图"]

        if not image_b64:
            self._send_json(400, {"error": "请上传产品白底图"})
            return
        if not selling_points:
            self._send_json(400, {"error": "请填写产品卖点/特点"})
            return

        # Step 1: GLM-4V-Flash 分析产品视觉特征
        vision_messages = [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_b64}},
                {"type": "text", "text": (
                    "这是一张电商产品白底图。请用中文简洁分析以下方面（每项1-2句话）：\n"
                    "1. 产品类型和核心外观特征\n"
                    "2. 颜色、材质、质感\n"
                    "3. 适合的目标人群和使用场景\n"
                    "4. 拍摄角度和构图特点\n"
                    "5. 适合的视觉风格方向（如：极简、清新、奢华、科技感等）\n"
                    "请直接输出分析结果，不要加标题前缀。"
                )}
            ]}
        ]
        analysis, err = _call_zhipu(ZHIPU_API_KEY, "glm-4v-flash", vision_messages, timeout=60)
        if err:
            self._send_json(200, {"error": "产品图分析失败：" + err})
            return

        # Step 2: GLM-4-Flash 基于分析+卖点生成多条提示词
        style_list = "、".join(styles)
        gen_messages = [
            {"role": "system", "content": (
                "你是电商AI生图提示词专家。根据产品分析结果和用户提供的卖点，"
                "为指定风格/场景分别生成专业的AI绘画提示词（英文prompt为主，中文说明为辅）。"
                "每条提示词要具体到：场景描述、光线、色调、构图、氛围、产品摆放方式。"
                "输出必须为严格JSON数组格式，每个元素包含 style(风格名)、prompt(英文生图提示词)、description(中文说明)。"
            )},
            {"role": "user", "content": (
                f"【产品视觉分析】\n{analysis}\n\n"
                f"【用户提供的卖点】\n{selling_points}\n\n"
                f"【需要生成的提示词风格】\n{style_list}\n\n"
                "请为以上每种风格各生成1条高质量AI生图提示词。直接输出JSON数组，不要加其他文字。"
            )}
        ]
        # glm-4-flash max_tokens=1024，够用
        result_text, err2 = _call_zhipu(ZHIPU_API_KEY, "glm-4-flash", gen_messages, timeout=70)
        if err2:
            self._send_json(200, {"error": "提示词生成失败：" + err2})
            return

        prompts_data = _extract_json(result_text)
        if not isinstance(prompts_data, list) or not prompts_data:
            # 如果模型没返回严格JSON，尝试从文本中提取
            prompts_data = [{"style": s, "prompt": result_text, "description": result_text[:80]} for s in styles[:1]]

        self._send_json(200, {
            "prompts": prompts_data,
            "product_analysis": analysis,
            "count": len(prompts_data)
        })

    def _handle_fetch_product(self):
        # 商品链接自动抓取：服务端代抓，绕过浏览器 CORS/反爬；同样需访问码门禁。
        # 也支持 action=download_image：用户手动粘贴图片 URL 时，服务端代下载转 data URL
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln)
            data = json.loads(body or b"{}")
        except Exception:
            self._send_json(400, {"error": "请求解析失败"})
            return
        token = (self.headers.get("Authorization") or "").replace("Bearer ", "", 1).strip()
        if not token:
            token = (self.headers.get("X-Access-Token") or "").strip()
        ok, info, kind = resolve_credential(token)
        if not ok:
            self._send_json(401, {"error": "访问码无效或已过期，请先在页面输入正确的动态访问码。（" + info.get("error", "") + "）"})
            return
        # 图片 URL 下载（用户手动粘贴的图片链接）
        if data.get("action") == "download_image":
            img_url = (data.get("image_url") or "").strip()
            if not img_url or not img_url.startswith(("http://", "https://")):
                self._send_json(400, {"error": "图片 URL 格式不正确"})
                return
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            }
            # 按 Referer 策略下载
            if "douyin" in img_url or "jinritemai" in img_url:
                headers["Referer"] = "https://haohuo.jinritemai.com/"
            elif "taobao" in img_url or "alicdn" in img_url:
                headers["Referer"] = "https://www.taobao.com/"
            elif "jd" in img_url or "360buy" in img_url:
                headers["Referer"] = "https://item.jd.com/"
            else:
                headers["Referer"] = img_url
            try:
                ir = S.get(img_url, headers=headers, timeout=10)
                if ir.status_code == 200 and ir.content and len(ir.content) > 2048:
                    fmt = detect_fmt(ir.content)[1]
                    data_url = "data:%s;base64,%s" % (fmt, base64.b64encode(ir.content).decode("ascii"))
                    self._send_json(200, {"image_data_url": data_url})
                else:
                    self._send_json(200, {"error": "图片下载失败（返回空内容或非图片）"})
            except Exception as e:
                self._send_json(200, {"error": "图片下载失败：" + str(e)})
            return
        # 正常商品页抓取
        try:
            result, err = fetch_product(data.get("url", ""))
        except Exception as e:
            self._send_json(200, {"error": "抓取失败：" + str(e)})
            return
        if err:
            self._send_json(200, {"error": err})
        else:
            self._send_json(200, result)

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
