#!/bin/sh
# 离线版 Mac 构建：electron-builder 仅负责打包未签名 app，
# 再用 codesign 做 ad-hoc 重签（带 disable-library-validation 权限），最后用 hdiutil 打成 dmg。
# 原因：electron-builder 24 在本机/部分环境会因“找不到 Developer ID”跳过签名从而连 dmg 目标都不构建，
#       手动签名 + hdiutil 更可靠，且能确保买家 Mac 上能正常启动（避免 Team ID 不匹配崩溃）。
set -e
cd "$(dirname "$0")"

VER=$(node -p "require('./package.json').version")
APP_NAME=$(node -p "require('./package.json').build.productName")
# 用 ASCII 文件名，规避 macOS runner 上传 artifact 时中文 NFD 编码到 Linux 解包被损坏（曾变成 AI.-0.2.8.dmg）
DMG="dist/dianshang-ai-image-mac-${VER}.dmg"
ZIP="dist/dianshang-ai-image-mac-${VER}.zip"
ENT="entitlements.mac.plist"

npm run copy-html
echo "==> packaging app (--dir)"
npx electron-builder --mac --arm64 --dir --publish never

APP=$(ls -d dist/mac-arm64/*.app | head -1)
echo "==> ad-hoc 重签: $APP"
# 1. 内部 dylib
find "$APP/Contents/Frameworks/Electron Framework.framework" -type f -name "*.dylib" -exec codesign --force --sign - {} \; 2>/dev/null || true
# 2. frameworks
for fw in "$APP/Contents/Frameworks"/*.framework; do
  [ -e "$fw" ] && codesign --force --sign - "$fw" 2>/dev/null || true
done
# 3. 主 app（带 entitlements）
codesign --force --sign - --entitlements "$ENT" "$APP"
codesign -v "$APP" && echo "==> 签名校验通过"

# 4. dmg
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

# 5. zip
rm -f "$ZIP"
cd dist && zip -r -q "../$ZIP" "mac-arm64/${APP_NAME}.app" && cd ..

echo "==> 产物: $DMG , $ZIP"
