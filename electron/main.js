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
// 返回 { cmd, version, hasRequests } —— 优先挑「版本达标且已装 requests」的解释器
function pythonHasRequests(cmd) {
  try {
    execSync(cmd + ' -c "import requests"', {
      encoding: "utf8", timeout: 5000, stdio: ["pipe", "pipe", "pipe"]
    });
    return true;
  } catch (e) {
    return false;
  }
}

function findPython() {
  const candidates = process.platform === "win32"
    ? ["python", "python3", path.join(resPath(), "venv", "Scripts", "python.exe")]
    : ["/usr/bin/python3", "/usr/local/bin/python3", "python3",
       path.join(resPath(), "venv", "bin", "python")];

  let fallback = null;
  for (const cmd of candidates) {
    try {
      const result = execSync(cmd + " --version 2>&1", {
        encoding: "utf8", timeout: 3000, stdio: ["pipe", "pipe", "pipe"]
      });
      const ver = (result.match(/Python (\d+\.\d+)/) || [])[1] || null;
      // 需要 Python 3.8+（proxy.py 用了 walrus operator 等语法）
      if (ver && parseFloat(ver) >= 3.8) {
        const hasRequests = pythonHasRequests(cmd);
        if (hasRequests) {
          return { cmd, version: ver, hasRequests: true };
        }
        if (!fallback) fallback = { cmd, version: ver, hasRequests: false };
      }
    } catch (e) { /* 不存在或不可执行 */ }
  }
  // 退而求其次：返回第一个版本达标但缺 requests 的解释器（稍后尝试自动安装）
  return fallback;
}

// ── 确保 requests 可用（失败则不要启动必崩的后端） ───────────────────
// 策略：python -m pip install →（PEP668 外部管理时）加 --break-system-packages
//       →（pip 缺失时）python -m ensurepip 引导后再装
function ensureRequests(pyCmd) {
  if (pythonHasRequests(pyCmd)) return true;

  console.log("[deps] 缺少 requests，尝试自动安装...");
  const tries = [
    pyCmd + " -m pip install --user requests",
    pyCmd + " -m pip install --user --break-system-packages requests",
  ];
  for (const cmd of tries) {
    try {
      execSync(cmd, { encoding: "utf8", timeout: 120000, stdio: "inherit" });
      if (pythonHasRequests(pyCmd)) {
        console.log("[deps] ✅ requests 安装完成");
        return true;
      }
    } catch (e) { /* 换下一种方式 */ }
  }

  // pip 可能未引导（如仅装了命令行工具 python）：用 ensurepip 拉起 pip 再装
  try {
    execSync(pyCmd + " -m ensurepip --user --upgrade", {
      encoding: "utf8", timeout: 120000, stdio: "inherit"
    });
    for (const cmd of tries) {
      try {
        execSync(cmd, { encoding: "utf8", timeout: 120000, stdio: "inherit" });
        if (pythonHasRequests(pyCmd)) {
          console.log("[deps] ✅ requests 安装完成（经 ensurepip）");
          return true;
        }
      } catch (e) { /* 换下一种方式 */ }
    }
  } catch (e) {
    console.warn("[deps] ⚠️ ensurepip 失败:", e.message.slice(0, 120));
  }

  console.warn("[deps] ⚠️ 无法自动安装 requests");
  return false;
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
      console.log(`[proxy] 🚀 使用 Python ${pyInfo.version} (${pyInfo.cmd})`);
      console.log(`[proxy]    脚本: ${PROXY_PY_PATH}`);

      // 依赖就绪直接启动；缺 requests 则尝试自动安装，装不上就不启动（避免必崩）
      if (pyInfo.hasRequests || ensureRequests(pyInfo.cmd)) {
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
        console.warn("[proxy] ⚠️ 无法准备 Python 运行环境，转 fallback/报错");
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


  showLaunchHelp(bin ? "proxy-failed" : "no-runtime");
}

// ── 启动失败时的用户指引 ──────────────────────────────────────────
function showLaunchHelp(reason) {
  const reasonText = {
    "no-runtime": "未找到可用的 Python 3.8+，后端无法运行。",
    "proxy-failed": "已尝试 Python 与内嵌二进制，后端均未能启动。",
  }[reason] || "启动失败。";

  const detail =
    reasonText + "\n\n" +
    "── macOS ──\n" +
    "1) 若是「无法验证开发者 / 已损坏」：清一次隔离标记再开\n" +
    "   终端执行：xattr -dr com.apple.quarantine \"/Applications/电商AI生图.app\"\n" +
    "   （App 在桌面就把路径改成 ~/Desktop/电商AI生图.app）\n" +
    "2) 或：右键 App →「打开」→ 再点「打开」即可放行（仅需一次）\n" +
    "3) 后端起不来：装 Python 3.8+（python.org 下载）\n" +
    "   应用会自动装所需模块；仍失败就重开终端重试。\n\n" +
    "── Windows ──\n" +
    "1) SmartScreen 拦截时点「更多信息」→「仍要运行」\n" +
    "2) 后端起不来：装 Python 3.8+ 并勾选 Add to PATH\n" +
    "   （若杀软误删 proxy-bin.exe，请允许或加白名单）\n\n" +
    "技术细节：\n" +
    "  proxy.py: " + PROXY_PY_PATH + " (" + (fs.existsSync(PROXY_PY_PATH) ? "存在" : "缺失") + ")\n" +
    "  二进制: " + (bin || "(无)");

  dialog.showErrorBox("无法启动 - 需要一点手动设置", detail);
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
