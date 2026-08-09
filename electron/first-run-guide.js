"use strict";
// 首次启动引导窗口：提示 macOS/Windows 侧载未签名时的手动放行步骤。
// guide.html 内部自行判断 localStorage，已看过则立即关闭（仅首次打扰）。
const { BrowserWindow } = require("electron");
const path = require("path");

function maybeShowGuide() {
  const w = new BrowserWindow({
    width: 560,
    height: 480,
    resizable: false,
    center: true,
    alwaysOnTop: true,
    title: "首次启动指引",
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  w.loadFile(path.join(__dirname, "guide.html"));
  w.once("closed", () => { /* 主窗口已在下层 */ });
}

module.exports = { maybeShowGuide };
