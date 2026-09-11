#!/usr/bin/env bash
# setup.sh —— 一键初始化脚本
# 做的事：建虚拟环境、装依赖、交互式收集配置生成 .env
set -e

echo "=== job-scan-bot 初始化 ==="
echo ""

# 1. 虚拟环境
if [ ! -d "venv" ]; then
    echo "[1/4] 创建虚拟环境..."
    python3 -m venv venv
else
    echo "[1/4] 虚拟环境已存在，跳过"
fi
source venv/bin/activate

# 2. 依赖
echo "[2/4] 安装依赖..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 3. 收集配置
echo "[3/4] 配置向导（直接回车可跳过可选项）"
echo ""

if [ -f ".env" ]; then
    read -p ".env 已存在，要覆盖重新配置吗？(y/N) " overwrite
    if [ "$overwrite" != "y" ] && [ "$overwrite" != "Y" ]; then
        echo "保留现有 .env，跳过配置向导"
        echo ""
        echo "[4/4] 完成。运行 'source venv/bin/activate && python linkedin_scan.py --dry-run' 测试一下。"
        exit 0
    fi
fi

read -p "Anthropic API Key (必填, sk-ant-...): " anthropic_key
read -p "Notion Integration Token (必填, ntn_... 或 secret_...): " notion_key
read -p "Notion Database ID (必填, 32位字符): " notion_db
read -p "Discord Webhook URL (可选，直接回车跳过): " discord_url
echo ""
read -p "评估匹配阈值 MATCH_THRESHOLD (默认70): " match_threshold
match_threshold=${match_threshold:-70}
read -p "搜索地点 SEATTLE_LOCATION (默认 'Seattle, WA'): " seattle_location
seattle_location=${seattle_location:-"Seattle, WA"}

cat > .env << EOF
# Claude API
ANTHROPIC_API_KEY=${anthropic_key}

# Notion
NOTION_API_KEY=${notion_key}
NOTION_DATABASE_ID=${notion_db}

# Discord（可选）
DISCORD_WEBHOOK_URL=${discord_url}

# 职位搜索条件
MATCH_THRESHOLD=${match_threshold}
SEARCH_TERM=Software Engineer
SEATTLE_LOCATION=${seattle_location}
SEATTLE_DISTANCE=25
HOURS_OLD=9
RESULTS_WANTED_PER_QUERY=30

# ats_scan.py（Greenhouse/Lever/Ashby/SmartRecruiters）——都是默认值，想调直接改这里
ATS_MATCHES_PATH=h1b_ats_matches.csv
MAX_JOBS_PER_COMPANY=0
ATS_CONCURRENCY=6
RETRY_DELAY_SECONDS=5
MIN_DESCRIPTION_LENGTH=50
ATS_TEST_LIMIT=0
MAX_JOBS_TO_EVALUATE=0
EOF

echo ""
echo "[4/4] .env 已生成。"
echo ""
echo "接下来手动做的两件事："
echo "  1. 编辑 config/profile.md，填入你的简历背景/筛选标准"
echo "  2. 按 README 里的说明，在 Notion 建好数据库和对应字段"
echo ""
echo "都做完之后，运行：source venv/bin/activate && python linkedin_scan.py --dry-run"
