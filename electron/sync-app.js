"use strict";
// 把桌面端需要的业务文件同步进 electron/app/，并刻意排除网页版专属文件（落地页/发码页等）。
// 用法：node sync-app.js   （开发前先跑一次，或 npm run sync）
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, ".."); // dian-shang-ai-image/
const APP_DIR = path.join(__dirname, "app");

// 桌面端只打包这些（落地页 landing.html / 发码页 gencode.html / 部署用 render.yaml / gentoken.py 等刻意排除）
const INCLUDE = ["standalone.html", "proxy.py", "access.py", "assets"];
// 不应进入 app/ 的网页版/仓库文件
const EXCLUDE = new Set([
  "landing.html", "gencode.html", "render.yaml", "gentoken.py",
  "上线前操作清单.md", "venv", "__pycache__", "proxy.log", ".git", "electron",
  "node_modules", ".env",
]);

if (!fs.existsSync(APP_DIR)) fs.mkdirSync(APP_DIR, { recursive: true });

function copyRecursive(src, dst) {
  const st = fs.statSync(src);
  if (st.isDirectory()) {
    fs.mkdirSync(dst, { recursive: true });
    for (const name of fs.readdirSync(src)) {
      if (EXCLUDE.has(name)) continue;
      copyRecursive(path.join(src, name), path.join(dst, name));
    }
  } else {
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.copyFileSync(src, dst);
  }
}

for (const name of INCLUDE) {
  const src = path.join(ROOT, name);
  if (!fs.existsSync(src)) { console.warn("跳过(不存在):", name); continue; }
  copyRecursive(src, path.join(APP_DIR, name));
  console.log("已同步:", name);
}
console.log("完成。桌面端 app/ 目录已就绪（落地页等网页版文件已排除）。");
