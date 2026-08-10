"use strict";
// 桌面端 preload：纯网页应用无需原生桥接，仅暴露最小安全标识。
// 云端自己管访问码门禁，桌面侧不再注入本地 token。
const { contextBridge } = require("electron");
contextBridge.exposeInMainWorld("electronAPI", {
  isDesktop: true,
  platform: process.platform,
});
