const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

// afterSign 钩子：用 codesign 对 .app 做 ad-hoc 重签，带上 disable-library-validation 权限。
// 原因：electron-builder 在无 Developer ID 证书时可能跳过签名或只做 linker-level ad-hoc，
// 不含 entitlements → 启动时 Electron 框架(Apple Team ID) 与 app(ad-hoc, 无 Team ID) Team ID 不匹配 → 崩溃。

const appPath = process.env.APP_PATH; // electron-builder 提供的变量
if (!appPath || !fs.existsSync(appPath)) {
  console.log('[afterSign] APP_PATH 未设置或不存在，跳过');
  process.exit(0);
}

const entitlementsPath = path.join(__dirname, 'entitlements.mac.plist');
if (!fs.existsSync(entitlementsPath)) {
  console.log('[afterSign] entitlements.mac.plist 不存在，跳过重签');
  process.exit(0);
}

console.log(`[afterSign] 开始 ad-hoc 重签: ${appPath}`);

try {
  // 1. 签内部 dylib
  const fwPath = path.join(appPath, 'Contents/Frameworks/Electron Framework.framework');
  const dylibs = execSync(`find "${fwPath}" -type f -name "*.dylib"`).toString().trim().split('\n').filter(Boolean);
  for (const dylib of dylibs) {
    try { execSync(`codesign --force --sign - "${dylib}"`, { stdio: 'pipe' }); } catch (e) {}
  }

  // 2. 签 frameworks
  const frameworksDir = path.join(appPath, 'Contents/Frameworks');
  if (fs.existsSync(frameworksDir)) {
    for (const fw of fs.readdirSync(frameworksDir)) {
      if (fw.endsWith('.framework')) {
        try { execSync(`codesign --force --sign - "${path.join(frameworksDir, fw)}"`, { stdio: 'pipe' }); } catch (e) {}
      }
    }
  }

  // 3. 签主 app（带 entitlements）
  execSync(`codesign --force --sign - --entitlements "${entitlementsPath}" "${appPath}"`, { stdio: 'inherit' });

  console.log('[afterSign] ✅ ad-hoc 重签完成');
} catch (err) {
  console.error('[afterSign] ❌ 重签失败:', err.message);
  process.exit(1);
}
