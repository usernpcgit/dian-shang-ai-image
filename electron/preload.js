"use strict";
// 桌面端 preload：当前前端(standalone.html)是纯 HTTP 应用，无需额外原生桥接。
// 保留最小安全接口，便于将来扩展（如调用原生对话框/文件系统）。
const { contextBridge } = require("electron");
contextBridge.exposeInMainWorld("electronAPI", {
  isDesktop: true,
  platform: process.platform,
});
// 桌面端：预注入本地 master token，使前端访问码门禁自动通过（前端零改动）。
// 须与 proxy.py 桌面端放行逻辑一致：本机请求被当作总钥匙对待。
try {
  const TOKEN_KEY = "wb_access_token";
  if (!localStorage.getItem(TOKEN_KEY)) {
    localStorage.setItem(TOKEN_KEY, "desktop-local-secret");
  }
} catch (e) { /* file:// 等无 localStorage 环境忽略 */ }
