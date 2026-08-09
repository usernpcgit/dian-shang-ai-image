"use strict";
// 电商AI生图 —— Electron 主进程（免费侧载优先版）
// 职责：拉起 Python 代理子进程(proxy-bin 或 python proxy.py) → 健康检查 → 加载 /tool → 首次引导 + 更新检测
const { app, BrowserWindow, shell, dialog } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");
const { spawn, execSync } = require("child_process");

const PORT = parseInt(process.env.WB_PORT || "8765", 10);
let proxyProc = null;
let mainWin = null;

// 打包后：resources/app/proxy-bin/proxy-bin[.exe]；开发模式：系统/venv python 跑 app/proxy.py
function proxyBinaryPath() {
  if (app.isPackaged) {
    const base = path.join(process.resourcesPath, "app", "proxy-bin");
    return process.platform === "win32" ? path.join(base, "proxy-bin.exe") : path.join(base, "proxy-bin");
  }
  return null;
}

function findPython() {
  const cand = [];
  if (process.platform !== "win32") {
    cand.push(path.join(__dirname, "app", "venv", "bin", "python"));
    cand.push("python3");
  } else {
    cand.push(path.join(__dirname, "app", "venv", "Scripts", "python.exe"));
    cand.push("python");
  }
  for (const c of cand) {
    try { fs.accessSync(c, fs.constants.X_OK); return c; } catch (e) { /* try next */ }
  }
  return process.platform === "win32" ? "python" : "python3";
}

function startProxy() {
  const env = Object.assign({}, process.env, {
    DESKTOP_MODE: "1",                 // 告知 proxy.py：本地单机模式（绑定 127.0.0.1 + localhost 免门禁）
    ACCESS_SECRET: "desktop-local-secret",
    WB_PORT: String(PORT),
    PYTHONUNBUFFERED: "1",
  });
  const bin = proxyBinaryPath();
  if (bin && process.platform === "darwin") {
    // 免费侧载：App 未公证，内部二进制可能仍带 quarantine 被 Gatekeeper 拦截。
    // 用户已右键打开信任本 App，这里顺便清掉内部二进制的隔离标记。
    try { execSync("xattr -d com.apple.quarantine " + JSON.stringify(bin) + " 2>/dev/null || true"); } catch (e) {}
  }
  if (bin) {
    proxyProc = spawn(bin, [], { env, stdio: "inherit" });
  } else {
    const py = findPython();
    const proxyPy = path.join(__dirname, "app", "proxy.py");
    proxyProc = spawn(py, [proxyPy], { env, stdio: "inherit" });
  }
  proxyProc.on("exit", (code, sig) => {
    console.log("[proxy] exited code=%s sig=%s", code, sig);
  });
  proxyProc.on("error", (err) => {
    dialog.showErrorBox("代理启动失败", "无法启动内置 Python 代理：\n" + err.message);
  });
}

function waitForHealth(cb) {
  let n = 0;
  const tryOnce = () => {
    const req = http.get(
      { host: "127.0.0.1", port: PORT, path: "/health", timeout: 1500 },
      (res) => {
        if (res.statusCode === 200) { cb(true); }
        else { if (n++ < 60) setTimeout(tryOnce, 400); else cb(false); }
      }
    );
    req.on("error", () => { if (n++ < 60) setTimeout(tryOnce, 400); else cb(false); });
    req.on("timeout", () => { req.destroy(); if (n++ < 60) setTimeout(tryOnce, 400); else cb(false); });
  };
  tryOnce();
}

function createWindow() {
  mainWin = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 980,
    minHeight: 640,
    backgroundColor: "#0b0a14",
    title: "电商AI生图",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWin.loadURL(`http://127.0.0.1:${PORT}/tool`);
  // 外链（如文档、发布页）一律用系统浏览器打开，不在应用内弹
  mainWin.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWin.on("closed", () => { mainWin = null; });
}

app.whenReady().then(() => {
  startProxy();
  waitForHealth((ok) => {
    if (!ok) {
      dialog.showErrorBox("启动失败", "内置服务未能就绪（端口可能被占用或 Python 缺失）。\n请确认未占用端口 " + PORT + "，或重新安装。");
      app.quit();
      return;
    }
    createWindow();
    require("./first-run-guide").maybeShowGuide();
    require("./update-check").checkForUpdates();
  });
});

function killProxy() {
  if (proxyProc) {
    try { proxyProc.kill("SIGTERM"); } catch (e) { /* ignore */ }
    proxyProc = null;
  }
}

app.on("window-all-closed", () => {
  killProxy();
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", killProxy);
app.on("quit", killProxy);
