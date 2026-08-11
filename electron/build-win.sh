#!/bin/sh
# 离线版 Windows 构建：electron-builder 打包未签名 app，再压成便携 zip（免安装，双击即用）。
# 注：Windows 端不强制签名，便携 zip 即可分发；如需安装器可后续补 nsis。
set -e
cd "$(dirname "$0")"

VER=$(node -p "require('./package.json').version")
APP_NAME=$(node -p "require('./package.json').build.productName")
ZIP="dist/${APP_NAME}-${VER}-win.zip"

npm run copy-html
echo "==> packaging win (--dir)"
npx electron-builder --win --x64 --dir --publish never

rm -f "$ZIP"
cd dist && zip -r -q "../$ZIP" "win-unpacked" && cd ..
echo "==> 产物: $ZIP"
