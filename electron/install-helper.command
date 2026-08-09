#!/bin/bash
# ============================================================
#  电商AI生图 - macOS 安装助手
#  用途：清除 Gatekeeper 隔离标记并启动应用
#  使用：双击此文件，或在终端中运行
# ============================================================

set -e

APP_NAME="电商AI生图.app"
# 搜索 App 的可能位置（按优先级）
SEARCH_PATHS=(
    "$HOME/Applications/$APP_NAME"
    "/Applications/$APP_NAME"
    "$HOME/Desktop/$APP_NAME"
    "$HOME/Downloads/$APP_NAME"
)

APP_PATH=""

for p in "${SEARCH_PATHS[@]}"; do
    if [ -d "$p" ]; then
        APP_PATH="$p"
        break
    fi
done

if [ -z "$APP_PATH" ]; then
    echo "❌ 未找到 $APP_NAME"
    echo ""
    echo "请先将应用放到以下位置之一，再运行本助手："
    echo "  • /Applications/"
    echo "  • ~/Applications/"
    echo "  • ~/Desktop/"
    echo "  • ~/Downloads/"
    echo ""
    echo "—— 也可以直接在终端手动放行（把路径换成你的实际位置）——"
    echo "  xattr -dr com.apple.quarantine \"/Applications/$APP_NAME\""
    echo "  open \"/Applications/$APP_NAME\""
    echo ""
    echo "—— 若本助手自身也打不开（\"来自身份不明的开发者\"），请右键本文件 →「打开」——"
    read -p "按回车键退出..." 
    exit 1
fi

echo "✅ 找到应用: $APP_PATH"
echo ""

# ── 步骤 1：清除所有扩展属性（quarantine / macl 等） ──
echo "🔧 正在清除隔离标记..."
xattr -cr "$APP_PATH"
echo "   ✅ 已清除所有扩展属性"

# ── 步骤 2：验证清除结果 ──
REMAINING=$(xattr -lr "$APP_PATH" 2>/dev/null | head -5)
if [ -n "$REMAINING" ]; then
    echo "⚠️ 仍有残留属性（可能需要管理员权限）："
    echo "$REMAINING"
    echo ""
    echo "尝试用 sudo 清除..."
    sudo xattr -cr "$APP_PATH" 2>/dev/null || true
fi

# ── 步骤 3：启动应用 ──
echo ""
echo "🚀 正在启动 $APP_NAME..."
open "$APP_PATH"

echo ""
echo "✅ 完成！如果应用正常打开，以后可以直接双击启动。"
echo ""
read -p "按回车键关闭此窗口..." 
