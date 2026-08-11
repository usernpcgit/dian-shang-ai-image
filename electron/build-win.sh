#!/bin/sh
# 离线版 Windows 构建：electron-builder 打包未签名 app，再压成便携 zip（免安装，双击即用）。
# 压缩：Windows 上用 PowerShell 的 Compress-Archive（原生可用）；其他平台回退 python3。
set -e
cd "$(dirname "$0")"

VER=$(node -p "require('./package.json').version")
APP_NAME=$(node -p "require('./package.json').build.productName")
# 用 ASCII 文件名，规避 Windows 下非 ASCII glob 问题
ZIP="dist/dianshang-ai-image-win-${VER}.zip"

npm run copy-html
echo "==> packaging win (--dir)"
npx electron-builder --win --x64 --dir --publish never
echo "==> win-unpacked 已生成，内容预览："
ls -la dist/win-unpacked | head

rm -f "$ZIP"
if command -v powershell.exe >/dev/null 2>&1; then
  echo "==> 用 PowerShell Compress-Archive 压缩"
  powershell.exe -NoProfile -Command "Compress-Archive -Path 'dist/win-unpacked' -DestinationPath '$ZIP' -Force"
else
  echo "==> 用 python3 压缩"
  python3 - "$ZIP" <<'PY'
import shutil, sys, os
zip_path = sys.argv[1]
base = zip_path[:-4] if zip_path.endswith('.zip') else zip_path
shutil.make_archive(base, 'zip', 'dist', 'win-unpacked')
print('==> 产物:', zip_path, os.path.getsize(zip_path), '字节')
PY
fi
ls -la "$ZIP"
