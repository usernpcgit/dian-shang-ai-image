#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动态访问码（测试阶段分享保护）

设计要点：
- 基于时间的一次性不可伪造访问码：payload(含过期时间戳) + HMAC-SHA256 签名，再 base32 编码成易读码。
- 离线即可校验（无数据库、无状态），天然支持多实例/重启。
- 生成：gentoken.py（卖家/管理员用）；校验：proxy.py 的 /api/verify 与 /api/gen 门禁共用本模块。
- 修改 ACCESS_SECRET 会让所有已发访问码立即失效 —— 这本身就是一键吊销开关。

说明：本实现采用「基于时间」的时效性（用户需求里的其中一种）。若以后需要「单次有效」，
可在此基础上追加一个服务端已用码集合（文件/Redis）做一次性消费，但会引入状态，测试阶段暂不需要。
"""
import os, time, json, base64, hmac, hashlib

# 单一真相源：proxy.py 与 gentoken.py 必须共用同一个密钥。
# 部署前请改成你自己的随机长字符串；生产环境推荐用环境变量 ACCESS_SECRET 注入（避免写进代码库）。
ACCESS_SECRET = os.environ.get("ACCESS_SECRET", "test-access-secret-change-me-2026")


def _b32_encode(raw: bytes) -> str:
    """base32 编码并按 4 位分组加横线，方便人工抄写/口述。"""
    s = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def _b32_decode(code: str) -> bytes:
    s = code.strip().upper().replace("-", "").replace(" ", "")
    pad = (-len(s)) % 8
    return base64.b32decode(s + "=" * pad)


def make_access_token(exp_hours: int = 168, note: str = "", max_uses: int = 1) -> str:
    """生成一个有时效的动态访问码。默认 168 小时 = 7 天。note 为备注（如发放对象/渠道）；
    max_uses 为该码最多可被解锁的人数/设备数（默认 1 = 只能一个人用；旧码无此字段则视为不限）。"""
    payload = {"v": 1, "exp": int(time.time() + exp_hours * 3600), "note": note[:40], "mu": int(max_uses)}
    p_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(ACCESS_SECRET.encode("utf-8"), p_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return _b32_encode((p_b64 + "." + sig).encode("utf-8"))


def verify_access_token(code: str):
    """校验访问码。返回 (ok:bool, info:dict)。

    ok=True 时 info 含 exp / remaining_hours / note；
    ok=False 时 info 含 error（格式错误 / 签名无效 / 已过期 / 校验失败）。
    """
    try:
        try:
            raw = _b32_decode(code)
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
        now = int(time.time())
        remaining = exp - now
        if remaining <= 0:
            return False, {"error": "访问码已过期", "exp": exp, "remaining_hours": 0}
        mu = payload.get("mu", None)  # 旧码无 mu 字段 -> None 表示不限次数
        return True, {"exp": exp, "remaining_hours": round(remaining / 3600, 1),
                      "note": payload.get("note", ""), "v": payload.get("v", 1), "mu": mu}
    except Exception as e:
        return False, {"error": "校验失败：" + str(e)}
