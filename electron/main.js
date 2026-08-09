"use strict";
// 电商AI生图 —— Electron 主进程（免费侧载优先版 v3）
//
// 架构决策（v3 重要变更）：
//   默认启动路径 = 系统 python3 + proxy.py（零编译型二进制）
//   原因：macOS Gatekeeper 会递归检查 .app 内所有可执行文件，
//         PyInstaller 打包的未签名 proxy-bin 是被拦截的根因。
//   fallback 路径 = 内嵌 proxy-bin（仅当系统 Python 完全不可用时）
//
const { app, BrowserWindow, shell, dialog } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");
const { spawn, execSync, execFile } = require("child_process");

const PORT = parseInt(process.env.WB_PORT || "8765", 10);
let proxyProc = null;
let mainWin = null;

// ── 路径常量 ──────────────────────────────────────────────────────
// extraResources 中的文件在 process.resourcesPath 下（asar 外）
// 开发模式下指向 electron/app/
function resPath(...segments) {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, ...segments);
  }
  return path.join(__dirname, "app", ...segments);
}

const PROXY_PY_PATH = resPath("proxy.py");
const PROXY_BIN_DIR = resPath("proxy-bin");

// ── Python 解释器查找 ───────────────────────────────────────────────
// 返回 { cmd: string, version: string | null }
function findPython() {
  const candidates = process.platform === "win32"
    ? ["python", "python3", path.join(resPath(), "venv", "Scripts", "python.exe")]
    : ["/usr/bin/python3", "/usr/local/bin/python3", "python3",
       path.join(resPath(), "venv", "bin", "python")];

  for (const cmd of candidates) {
    try {
      const result = execSync(cmd + " --version 2>&1", {
        encoding: "utf8", timeout: 3000, stdio: ["pipe", "pipe", "pipe"]
      });
      const ver = (result.match(/Python (\d+\.\d+)/) || [])[1] || null;
      // 需要 Python 3.8+（proxy.py 用了 walrus operator 等语法）
      if (ver && parseFloat(ver) >= 3.8) {
        return { cmd, version: ver };
      }
    } catch (e) { /* 不存在或不可执行 */ }
  }
  return null;
}

// ── 检测 Python 是否有 requests 模块 ────────────────────────────────
function checkPythonDeps(pyCmd) {
  try {
    execSync(pyCmd + ' -c "import requests; print(requests.__version__)"', {
      encoding: "utf8", timeout: 5000, stdio: ["pipe", "pipe", "pipe"]
    });
    return true;
  } catch (e) {
    return false;
  }
}

// ── 自动安装 Python 依赖 ────────────────────────────────────────────
// 仅安装到用户空间（--user），不需要 sudo
function installPythonDeps(pyCmd) {
  try {
    console.log("[deps] 正在安装缺失的 Python 依赖...");
    const pip = process.platform === "win32" ? pyCmd.replace(/python\.?/, "pip") : "pip3";
    execSync(pip + ' install --user requests 2>&1', {
      encoding: "utf8", timeout: 60000, stdio: "inherit"
    });
    console.log("[deps] ✅ 依赖安装完成");
    return true;
  } catch (e) {
    console.warn("[deps] ⚠️ 自动安装失败:", e.message.slice(0, 150));
    return false;
  }
}

// ── proxy-bin 查找（fallback 用） ───────────────────────────────────
function findProxyBin() {
  if (!app.isPackaged) return null;
  const binName = process.platform === "win32" ? "proxy-bin.exe" : "proxy-bin";
  // extraResources 路径
  const p1 = path.join(PROXY_BIN_DIR, binName);
  if (fs.existsSync(p1)) return p1;
  // 兼容旧 asar 内路径
  const p2 = path.join(process.resourcesPath, "app", "proxy-bin", binName);
  if (fs.existsSync(p2)) return p2;
  return null;
}

// ── macOS：启动前清除扩展属性 ──────────────────────────────────────
// 必须在 spawn 任何二进制之前调用
function clearMacAttrs(targetPath) {
  if (process.platform !== "darwin") return;
  try {
    execSync('xattr -cr ' + JSON.stringify(targetPath), {
      stdio: "pipe", timeout: 10000
    });
    console.log("[mac] 已清除属性:", targetPath);
  } catch (e) {
    // 非致命
  }
}

// ── 启动代理进程（核心逻辑） ────────────────────────────────────────
function startProxy() {
  const env = Object.assign({}, process.env, {
    DESKTOP_MODE: "1",
    ACCESS_SECRET: "desktop-local-secret",
    WB_PORT: String(PORT),
    PYTHONUNBUFFERED: "1",
  });

  // ── macOS 预处理：清除 resources 目录属性 ──
  if (app.isPackaged && process.platform === "darwin") {
    clearMacAttrs(process.resourcesPath);
  }

  // ════════════════════════════════════════════════
  //  方案一（首选）：系统 Python + proxy.py
  //  零二进制 → Gatekeeper 不会因为未签名可执行文件拦截
  // ════════════════════════════════════════════════
  if (fs.existsSync(PROXY_PY_PATH)) {
    const pyInfo = findPython();
    if (pyInfo) {
      // 检查依赖
      if (!checkPythonDeps(pyInfo.cmd)) {
        console.log("[proxy] 缺少 requests 依赖，尝试自动安装...");
        installPythonDeps(pyInfo.cmd);
        // 再验证一次
        if (!checkPythonDeps(pyInfo.cmd)) {
          console.warn("[proxy] 依赖仍不可用，将尝试 fallback");
        } else {
          console.log("[proxy] ✅ 依赖就绪");
        }
      }

      console.log(`[proxy] 🚀 使用 Python ${pyInfo.version} (${pyInfo.cmd})`);
      console.log(`[proxy]    脚本: ${PROXY_PY_PATH}`);

      try {
        proxyProc = spawn(pyInfo.cmd, [PROXY_PY_PATH], {
          env, stdio: "inherit", detached: false
        });
        bindProxyEvents(proxyProc, `Python (${pyInfo.cmd})`);
        return; // ✅ 成功走 Python 路径
      } catch (e) {
        console.error("[proxy] Python 启动异常:", e.message);
        // 继续尝试 fallback
      }
    } else {
      console.log("[proxy] 未找到可用的 Python 3.8+");
    }
  } else {
    console.log("[proxy] proxy.py 不存在:", PROXY_PY_PATH);
  }

  // ════════════════════════════════════════════════
  //  方案二（fallback）：PyInstaller 打包的二进制
  //  仅当 Python 完全不可用时使用
  // ══════════════════════════════════════════════
  const bin = findProxyBin();
  if (bin) {
    console.log("[proxy] 🔄 Fallback 到二进制:", bin);
    clearMacAttrs(bin);

    try {
      proxyProc = spawn(bin, [], {
        env, stdio: "inherit", detached: false
      });
      bindProxyEvents(proxyProc, `Binary (${bin})`);
      return;
    } catch (e) {
      console.error("[proxy] 二进制启动也失败:", e.message);
    }
  }

  // ════════════════════════════════════════════════
  //  所有方案都失败 → 报错退出
  // ══════════════════════════════════════════════
  const detail =
    "无法启动内置代理服务。\n\n" +
    "已尝试以下方式均失败：\n" +
    "  • 系统 Python + proxy.py（" + (findPython() ? "找到 Python 但启动失败" : "未找到 Python 3.8+") + "）\n" +
    "  • 内嵌二进制（" + (bin ? "找到但启动失败" : "不存在") + "）\n\n" +
    "建议：\n" +
    "  Mac: 确认终端运行 python3 --version 能看到 3.8+\n" +
    "  Win: 安装 Python 3.8+ 并勾选 \"Add to PATH\"\n\n" +
    "技术细节：\n" +
    "  proxy.py 路径: " + PROXY_PY_PATH + " (" + (fs.existsSync(PROXY_PY_PATH) ? "存在" : "不存在") + ")\n" +
    "  二进制路径: " + (bin || "(无)") + "\n";

  dialog.showErrorBox("启动失败 - 无法启动代理", detail);
  app.quit();
}

// ── 代理进程事件绑定 ────────────────────────────────────────────────
function bindProxyEvents(proc, label) {
  proc.on("exit", (code, sig) => {
    console.log(`[${label}] exited code=${code} sig=${sig}`);
  });

  proc.on("error", (err) => {
    const msg =
      `代理进程 (${label}) 启动失败。\n\n` +
      `错误: ${err.message}\n\n` +
      `如反复出现此问题请确认：\n` +
      `  • Mac: 已安装 Python 3.8+ (python3 --version)\n` +
      `  • Win: 已安装 Python 3.8+ 并加入 PATH`;
    dialog.showErrorBox("代理启动失败", msg);
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
