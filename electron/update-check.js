"use strict";
// 轻量更新检测（不依赖签名）：对比 GitHub Releases latest 版本号，有新版本则弹窗引导去下载页。
// 不做自动下载——未签名二进制下载后仍会被 Gatekeeper/SmartScreen 拦截，手动重装更稳妥。
const { dialog, shell } = require("electron");
const https = require("https");

const OWNER = "usernpcgit";
const REPO = "dian-shang-ai-image";

function currentVersion() {
  try { return require("./package.json").version; } catch (e) { return null; }
}

function fetchLatest(cb) {
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/releases/latest`;
  const req = https.get(
    url,
    { headers: { "User-Agent": "dianshang-ai-updater", "Accept": "application/vnd.github+json" } },
    (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        if (res.statusCode !== 200) { cb(new Error("HTTP " + res.statusCode)); return; }
        try { const j = JSON.parse(data); cb(null, j.tag_name, j.html_url); }
        catch (e) { cb(e); }
      });
    }
  );
  req.on("error", (e) => cb(e));
  req.setTimeout(5000, () => req.destroy(new Error("timeout")));
}

function cmp(a, b) {
  const pa = String(a).replace(/^v/, "").split(".").map(Number);
  const pb = String(b).replace(/^v/, "").split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, const_y = pb[i] || 0;
    if (x > const_y) return 1;
    if (x < const_y) return -1;
  }
  return 0;
}

function checkForUpdates() {
  const cur = currentVersion();
  if (!cur) return;
  fetchLatest((err, tag, url) => {
    if (err || !tag) return; // 离线/限流均静默，不打扰用户
    if (cmp(tag, cur) > 0) {
      dialog.showMessageBox({
        type: "info",
        title: "发现新版本",
        message: `当前 v${cur}，已有新版本 ${tag}`,
        detail: "免费侧载版本需手动下载安装。点击「去下载」打开发布页。",
        buttons: ["去下载", "稍后"],
        defaultId: 0,
        cancelId: 1,
      }).then(({ response }) => {
        if (response === 0) {
          shell.openExternal(url || `https://github.com/${OWNER}/${REPO}/releases/latest`);
        }
      });
    }
  });
}

module.exports = { checkForUpdates };
