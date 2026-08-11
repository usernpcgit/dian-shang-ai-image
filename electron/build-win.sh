#!/bin/sh
# 离线版 Windows 构建：electron-builder 打包未签名 app，再压成便携 zip（免安装，双击即用）。
# 用 Python 的 shutil.make_archive 压缩（跨平台，不依赖外部 zip 命令），文件名用 ASCII 避免编码问题。
set -e
cd "$(dirname "$0")"

VER=$(node -p "require('./package.json').version")
APP_NAME=$(node -p "require('./package.json').build.productName")
# 用 ASCII 文件名，规避 Windows 下非 ASCII glob 问题
ZIP="dist/dianshang-ai-image-win-${VER}.zip"

npm run copy-html
echo "==> packaging win (--dir)"
npx electron-builder --win --x64 --dir --publish never

rm -f "$ZIP"
python3 - "$VER" "$APP_NAME" "$ZIP" <<'PY'
import shutil, sys, os
ver, app_name, zip_path = sys.argv[1], sys.argv[2], sys.argv[3]
src = os.path.join("dist", "win-unpacked")
if not os.path.isdir(src):
    raise SystemExit("win-unpacked 未生成，构建失败")
# make_archive 会在 zip_path 基础上加 .zip，这里先去掉再交给它
base = zip_path[:-4] if zip_path.endswith(".zip") else zip_path
shutil.make_archive(base, "zip", "dist", "win-unpacked")
print("==> 产物:", zip_path, "大小", os.path.getsize(zip_path), "字节")
PY
