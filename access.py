#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动态访问码（测试阶段分享保护）

设计要点：
- 基于时间的不可伪造访问码：到期时间戳 + 最多人数 + HMAC-SHA256 签名（取前 8 字节）。
- 离线即可校验（无数据库、无状态），天然支持多实例/重启。
- 访问码格式很短、全大写+数字、分三段，方便人工抄写：
      PART1-PART2-PART3
      PART1 = 到期时间戳的 base36（大写）
      PART2 = 最多可用人数（十进制）
      PART3 = HMAC-SHA256(secret, "PART1.PART2") 前 8 字节的 base32（13 字符，防伪造）
  例：K3F9ZQ2-1-XQ7KMP9BCD
- 修改 ACCESS_SECRET 会让所有已发访问码立即失效 —— 这本身就是一键吊销开关。
- 兼容旧版长码（base32 包裹的 JSON 格式），已发出的旧码在过期前依然可用。
"""
import os, time, json, base64, hmac, hashlib, re

# 单一真相源：proxy.py 与 gentoken.py 必须共用同一个密钥。
# 部署前请改成你自己的随机长字符串；生产环境推荐用环境变量 ACCESS_SECRET 注入（避免写进代码库）。
ACCESS_SECRET = os.environ.get("ACCESS_SECRET", "test-access-secret-change-me-2026")

_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _b32_encode(raw: bytes) -> str:
    """base32 编码并按 4 位分组加横线（旧码 fallback 用）。"""
    s = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def _b32_decode(code: str) -> bytes:
    s = code.strip().upper().replace("-", "").replace(" ", "")
    pad = (-len(s)) % 8
    return base64.b32decode(s + "=" * pad)


def _b36_encode(n: int) -> str:
    if n <= 0:
        return "0"
    s = ""
    while n > 0:
        s = _B36[n % 36] + s
        n //= 36
    return s


def _b36_decode(s: str) -> int:
    return int(s, 36)


def _sig8(secret: str, msg: str) -> str:
    """HMAC-SHA256 取前 8 字节 → base32 大写无填充（13 字符）。"""
    dig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()[:8]
    return base64.b32encode(dig).decode("ascii").rstrip("=")


# 新短码正则：PART1(base36)-PART2(数字)-PART3(base32 13位)
_NEW_RE = re.compile(r"^([0-9A-Z]{4,10})-([0-9]{1,3})-([A-Z2-7]{13})$")


def make_access_token(exp_hours: int = 168, note: str = "", max_uses: int = 1) -> str:
    """生成一个有时效的动态访问码。默认 168 小时 = 7 天。
    note 仅为发放记录，不再写进码里（缩短长度）；max_uses 为该码最多可被解锁的人数（默认 1）。"""
    exp = int(time.time() + exp_hours * 3600)
    mu = int(max_uses)
    p1 = _b36_encode(exp)
    p2 = str(mu)
    p3 = _sig8(ACCESS_SECRET, p1 + "." + p2)
    return f"{p1}-{p2}-{p3}"


def verify_access_token(code: str):
    """校验访问码。返回 (ok:bool, info:dict)。

    ok=True 时 info 含 exp / remaining_hours / note / v / mu；
    ok=False 时 info 含 error（格式错误 / 签名无效 / 已过期 / 校验失败）。
    同时兼容新短码与旧版长码。
    """
    try:
        c = code.strip().replace(" ", "")
        # —— 新短码 ——
        m = _NEW_RE.match(c)
        if m:
            p1, p2, p3 = m.group(1), m.group(2), m.group(3)
            exp = _b36_decode(p1)
            mu = int(p2)
            expect = _sig8(ACCESS_SECRET, p1 + "." + p2)
            if not hmac.compare_digest(expect, p3):
                return False, {"error": "签名无效（密钥不符或码被篡改）"}
            return _build_ok(exp, mu, "")
        # —— 旧长码 fallback ——
        try:
            raw = _b32_decode(c)
            txt = raw.decode("ascii")
        except Exception:
            return False, {"error": "格式错误"}
        p_b64, _, sig = txt.rpartition(".")
        if not p_b64 or not sig:
            return False, {"error": "格式错误"}
        expect = hmac.new(ACCESS_SECRET.encode("utf-8"), p_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return False, {"error": "签名无效（密钥不符或码被篡改）"}
        pad = (-len(p_b64)) % 4
        payload = json.loads(base64.urlsafe_b64decode(p_b64 + "=" * pad).decode("utf-8"))
        exp = int(payload.get("exp", 0))
        mu = payload.get("mu", None)  # 旧码无 mu 字段 -> None 表示不限次数
        return _build_ok(exp, mu, payload.get("note", ""))
    except Exception as e:
        return False, {"error": "校验失败：" + str(e)}


def _build_ok(exp: int, mu, note: str):
    now = int(time.time())
    remaining = exp - now
    if remaining <= 0:
        return False, {"error": "访问码已过期", "exp": exp, "remaining_hours": 0}
    return True, {"exp": exp, "remaining_hours": round(remaining / 3600, 1),
                  "note": note, "v": 2 if isinstance(mu, int) else 1, "mu": mu}


def resolve_credential(cred):
    """统一校验入口：接受「访问码」或「总钥匙（=ACCESS_SECRET）」。

    返回 (ok:bool, info:dict, kind:str)。
    - kind == 'code'   ：普通访问码，有时效/人数限制；
    - kind == 'master' ：总钥匙，永久解锁、无限制；
    - kind == None     ：校验失败（info 含 error）。

    优先按访问码校验；若不匹配再判断是否等于总钥匙（恒定时间比较，避免时序侧信道）。
    """
    ok, info = verify_access_token(cred)
    if ok:
        return True, info, "code"
    c = (cred or "").strip()
    if c and hmac.compare_digest(c, ACCESS_SECRET):
        return True, {"exp": None, "remaining_hours": None,
                      "note": "总钥匙(永久)", "mu": None, "master": True}, "master"
    return False, info, None

