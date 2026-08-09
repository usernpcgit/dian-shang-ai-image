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

// ── 代理二进制路径解析 ──────────────────────────────────────────────
// 打包后优先从 extraResources（resources/proxy-bin/）找，
// 兼容旧路径 resources/app/proxy-bin/；开发模式返回 null（走 Python）。
function proxyBinaryPath() {
  if (!app.isPackaged) return null;
  // 路径 A：extraResources（推荐，在 asar 外，二进制不会被遗漏）
  const extraResPath = path.join(process.resourcesPath, "proxy-bin");
  const binName = process.platform === "win32" ? "proxy-bin.exe" : "proxy-bin";
  const pathA = path.join(extraResPath, binName);
  if (fs.existsSync(pathA)) return pathA;
  // 路径 B：兼容旧版 app/ 内嵌（asar 内）
  const legacyPath = path.join(process.resourcesPath, "app", "proxy-bin");
  const pathB = path.join(legacyPath, binName);
  if (fs.existsSync(pathB)) return pathB;
  // 都不存在返回 null，由 startProxy 回退到 Python
  return null;
}

// ── Python 解释器查找 ───────────────────────────────────────────────
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
    try { fs.accessSync(c, fs.constants.X_OK); return c; } catch (e) { /* next */ }
  }
  return process.platform === "win32" ? "python" : "python3";
}

// ── macOS 扩展属性清除 ──────────────────────────────────────────────
// 未签名 App 的内部二进制可能被 macOS 打上 quarantine / macl 等属性，
// 导致 Gatekeeper 拦截或 spawn ENOENT。递归清除目标目录下所有扩展属性。
function clearMacAttrs(targetPath) {
  if (process.platform !== "darwin") return;
  try {
    // -r 递归；清掉所有属性（不限于 quarantine）
    execSync("xattr -cr " + JSON.stringify(targetPath), { stdio: "pipe" });
    console.log("[mac] 已清除扩展属性:", targetPath);
  } catch (e) {
    console.warn("[mac] 清除扩展属性失败（非致命）:", e.message.slice(0, 120));
  }
}

// ── 启动代理进程 ────────────────────────────────────────────────────
function startProxy() {
  const env = Object.assign({}, process.env, {
    DESKTOP_MODE: "1",
    ACCESS_SECRET: "desktop-local-secret",
    WB_PORT: String(PORT),
    PYTHONUNBUFFERED: "1",
  });

  const bin = proxyBinaryPath();

  // 方案一：用 PyInstaller 打包的单文件二进制（首选）
  if (bin) {
    console.log("[proxy] 使用二进制:", bin);
    clearMacAttrs(bin);           // macOS：清掉所有 xattr
    clearMacAttrs(path.dirname(bin)); // 连同目录也清一遍
    proxyProc = spawn(bin, [], { env, stdio: "inherit" });
  }
  // 方案二：回退到系统 Python + proxy.py（二进制缺失时）
  else {
    const py = findPython();
    const proxyPy = path.join(__dirname, "app", "proxy.py");

    // 检查 proxy.py 是否存在（打包后可能在 asar 内）
    if (!fs.existsSync(proxyPy)) {
      const msg =
        "找不到代理程序文件。\n\n" +
        "期望路径：" + proxyPy + "\n" +
        "二进制路径：" + (bin || "(无)") + "\n\n" +
        "安装包可能损坏，请重新下载安装。";
      dialog.showErrorBox("启动失败", msg);
      app.quit();
      return;
    }

    console.log("[proxy] 回退到 Python:", py, proxyPy);
    proxyProc = spawn(py, [proxyPy], { env, stdio: "inherit" });
  }

  proxyProc.on("exit", (code, sig) => {
    console.log("[proxy] exited code=%s sig=%s", code, sig);
  });

  proxyProc.on("error", (err) => {
    const detail =
      "无法启动内置代理。\n\n" +
      "错误：" + err.message + "\n" +
      "尝试的路径：" + (bin || "(使用Python回退)") + "\n\n" +
      "如反复出现此问题，请确认系统已安装 Python 3，或重新下载安装包。";
    dialog.showErrorBox("代理启动失败", detail);
  });
}

// ── 健康检查轮询 ────────────────────────────────────────────────────
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

// ── 创建主窗口 ──────────────────────────────────────────────────────
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
  // 外链一律用系统浏览器打开
  mainWin.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWin.on("closed", () => { mainWin = null; });
}

// ── 应用生命周期 ────────────────────────────────────────────────────
app.whenReady().then(() => {
  startProxy();
  waitForHealth((ok) => {
    if (!ok) {
      dialog.showErrorBox(
        "启动失败",
        "内置服务未能就绪（端口可能被占用或 Python 缺失）。\n" +
        "请确认端口 " + PORT + " 未被占用，或重新安装。"
      );
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
