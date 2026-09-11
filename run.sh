#!/bin/bash
# 本地cron调用的入口脚本。假设 .env 文件和 linkedin_scan.py 在同一目录下。

cd "$(dirname "$0")"

# 加载 .env 里的环境变量
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# 用虚拟环境里的python（如果你建了venv的话），没有就退回系统python3
if [ -f venv/bin/python ]; then
  PYTHON=venv/bin/python
else
  PYTHON=python3
fi

# 追加写日志，方便回头看每次跑的情况/报错
$PYTHON linkedin_scan.py >> run.log 2>&1
