#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电商AI Image —— 本地小代理
只做一件事：在浏览器（standalone.html）和 redfox gpt-image-2 之间转发请求，
并补上 CORS 头，让纯前端页面能调用远程生图接口。不存任何业务数据。

用法：
  双击「启动.command」即可（它会启动本脚本并打开浏览器）。
  或手动： python3 proxy.py  （默认端口 8765，可用 WB_PORT 环境变量改）

接口：
  GET  /             -> 返回 standalone.html
  GET  /health       -> ok
  POST /api/gen      -> 图生图：白底产品图 + 提示词 -> gpt-image-2 -> 返回 base64 图数组
       body(JSON): {image_b64, prompt, key, fidelity?, size?, quality?, n?}
       返回: {"images":["data:image/png;base64,....", ...]} 或 {"error":"原因"}
"""
import os, sys, json, base64, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

PORT = int(os.environ.get("PORT") or os.environ.get("WB_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "standalone.html")

SUBMIT_URL = "https://redfox.hk/story/api/parseWork/imageGen/submitSkill"
RESULT_URL = "https://redfox.hk/story/api/parseWork/imageGen/result"
UPLOAD_URL = "https://redfox.hk/story/api/parseWork/imageGen/uploadImage"
SOURCE = "GPT image2-SkillHub"
POLL = 3
MAX_TRY = 80

# 绕过系统/环境变量里的 http 代理，直连 redfox（否则会被公司代理拦截）
S = requests.Session()
S.trust_env = False


def detect_fmt(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return "png"


def gen_image(key, image_b64, prompt, fidelity, size, quality, n=1):
    if not key:
        return None, "缺少 redfox API Key"
    if not image_b64:
        return None, "缺少白底产品图"
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(image_b64)
    except Exception as e:
        return None, "图片解码失败：" + str(e)
    fmt = detect_fmt(raw)
    try:
        up = S.post(
            UPLOAD_URL,
            files={"file": ("product." + fmt, raw, "image/" + fmt)},
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
            SUBMIT_URL,
            json={"prompt": prompt, "source": SOURCE, "operation": "edit",
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
                RESULT_URL,
                json={"taskId": task_id},
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                timeout=15,
            )
            rj = rr.json()
        except Exception as e:
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
                for p in paths[: int(n) if str(n).isdigit() else 1]:
                    d = S.get(p, timeout=120)
                    out.append("data:image/png;base64," + base64.b64encode(d.content).decode())
                return out, None
            except Exception as e:
                return None, "下载生成图失败：" + str(e)
        elif st == "failed":
            return None, "生图失败：" + str((rj.get("data") or {}).get("failReason", "未知原因"))
    return None, "等待超时（约 %d 秒）" % (POLL * MAX_TRY)


class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(HTML, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self._cors()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_response(404)
                self.end_headers()
            return
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(b"ok")
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
        image_b64 = data.get("image_b64", "")
        prompt = data.get("prompt", "")
        key = data.get("key", "")
        fidelity = data.get("fidelity") or ""
        size = data.get("size") or "1024x1536"
        quality = data.get("quality") or "high"
        n = data.get("n") or 1
        try:
            imgs, err = gen_image(key, image_b64, prompt, fidelity, size, quality, n)
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
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    try:
        lan = socket.gethostbyname(socket.gethostname())
    except Exception:
        lan = "本机局域网IP"
    print("电商AI Image 已启动（页面 + 生图代理一体）")
    print("  本机打开  : http://localhost:%d/" % PORT)
    print("  手机/同网络: http://%s:%d/   （手机浏览器打开即可直接用 AI 生图）" % (lan, PORT))
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
