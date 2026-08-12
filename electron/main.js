"use strict";
// 生图Agent · 桌面端（离线真买断版）
// 架构：前端 standalone.html + 本地代理 local-proxy.js 全部打包进 App。
//   买家用自己的 API Key（或 Pollinations 免 Key）直连各服务商，卖家零成本、工具离线可用。
//   更新通道走 GitHub Releases（与卖家业务服务器解耦，见 update-check.js）。
const { app, BrowserWindow } = require("electron");
const path = require("path");
const { checkForUpdates } = require("./update-check");
const { startLocalProxy } = require("./local-proxy");

// 启动本地代理（监听 127.0.0.1:8765，离线生图/竞品分析转发）
let proxyServer = null;
try {
  proxyServer = startLocalProxy();
} catch (e) {
  console.error("[main] 本地代理启动失败：", e);
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 640,
    title: "生图Agent",
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  // 离线模式：加载打包进 App 的本地前端（file:// 下，前端自动把 /api/* 指向 http://localhost:8765）
  const indexPath = path.join(__dirname, "standalone.html");
  win.loadFile(indexPath);

  // 本地前端无需冷启动等待，加载完即显示
  win.once("ready-to-show", () => win.show());

  win.webContents.on("did-fail-load", (_e, errorCode, errorDescription) => {
    console.warn("[load] 加载本地前端失败:", errorCode, errorDescription);
  });
}

app.whenReady().then(() => {
  createWindow();
  checkForUpdates();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on("before-quit", () => {
  if (proxyServer) { try { proxyServer.close(); } catch (e) {} }
});
