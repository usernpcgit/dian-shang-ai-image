"use strict";
// Mac 构建后钩子（afterSign）：对未签名 app 做 ad-hoc 签名。
//
// 为什么这样做（而非 --deep 全量重签）：
//   • 只用 `codesign --force --sign - <app>`（不带 --deep），只重签外层主可执行文件、
//     改变其哈希，使 macOS XProtect 不再命中「未签名 Electron」的恶意软件特征。
//   • 同时保留 Electron Framework 的 Apple 原始签名（--deep 会把它覆盖成 ad-hoc，
//     反而被 XProtect 判为「签名被篡改」→ 整个 app 被移到废纸篓）。
//   • ad-hoc 签名后 Gatekeeper 仍会弹「无法验证开发者」，但右键→打开即可放行（仅需一次）。
const { execSync } = require("child_process");
const path = require("path");

module.exports = async function afterSign(context) {
  if (context.electronPlatformName !== "darwin") return;
  const appName = context.packager.appInfo.productFilename + ".app";
  const appPath = path.join(context.appOutDir, appName);
  try {
    execSync(`codesign --force --sign - "${appPath}"`, { stdio: "inherit" });
    console.log("[afterSign] ✅ 已 ad-hoc 签名:", appPath);
  } catch (e) {
    console.warn(
      "[afterSign] ⚠️ codesign 失败（退回未签名，仍可能被 XProtect/Gatekeeper 拦）:",
      e.message.slice(0, 160)
    );
  }
};
