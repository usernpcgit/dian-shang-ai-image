"use strict";
// 桌面端 preload：纯网页应用无需原生桥接，仅暴露最小安全标识。
// 云端自己管访问码门禁，桌面侧不再注入本地 token。
const { contextBridge } = require("electron");
const os = require("os");
const crypto = require("crypto");

// 计算稳定的设备指纹：主机名 + 平台 + 架构 + 用户名 + 首个非回环 MAC。
// 同一台机器（同一用户）保持不变；换机器后变化，使「设备绑定授权证书」无法跨设备使用。
function computeDeviceId() {
  let mac = "";
  try {
    const ifaces = os.networkInterfaces();
    outer:
    for (const k in ifaces) {
      for (const i of ifaces[k]) {
        if (!i.internal && i.mac && i.mac !== "00:00:00:00:00:00") { mac = i.mac; break outer; }
      }
    }
  } catch (e) { mac = ""; }
  const user = (function () { try { return os.userInfo().username; } catch (e) { return ""; } })();
  const seed = [os.hostname(), os.platform(), os.arch(), user, mac].join("|");
  return "D-" + crypto.createHash("sha256").update(seed).update("dianshang-ai-image").digest("hex").slice(0, 24);
}

contextBridge.exposeInMainWorld("electronAPI", {
  isDesktop: true,
  platform: process.platform,
  deviceId: computeDeviceId(),
  // 桌面端买断制密钥：前端随请求自动附加 X-Desktop + 此密钥，后端信任放行（无需访问码）。
  // 与 proxy.py 的 DESKTOP_KEY 默认值保持一致；如要轮换，两端需同步更新。
  desktopKey: "dian-shang-desktop-buyout-9Kx2mP7qR4tV8wY1z",
});
