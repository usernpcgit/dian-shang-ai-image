"use strict";
// 电商AI生图 · 桌面端（纯 Electron 轻壳）
// 设计原则：桌面端不捆绑 Python。AI 生图 / 竞品分析的后端跑在云端（Render），
// 桌面 app 只负责用原生窗口加载云端网页端 /tool，所有 /api/* 调用同域直达云端。
// 这样彻底规避了「未签名 Electron 内嵌 Python 解释器 → XProtect / 签名冲突」这一整类问题。
const { app, BrowserWindow } = require("electron");
const path = require("path");
const { checkForUpdates } = require("./update-check");

// 云端网页端地址。换成你自己 Render 部署的 /tool 链接即可
// （默认取自 render.yaml 的 service name: self-ai-image）。
const CLOUD_APP_URL =
  process.env.CLOUD_APP_URL || "https://self-ai-image.onrender.com/tool";

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 640,
    title: "电商AI生图",
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  win.loadURL(CLOUD_APP_URL);

  // 云端免费实例首次会冷启动（约数十秒），加载完再显示，避免白屏误会。
  win.once("ready-to-show", () => win.show());

  win.webContents.on("did-fail-load", (_e, errorCode, errorDescription) => {
    console.warn("[load] 加载云端失败:", errorCode, errorDescription);
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
