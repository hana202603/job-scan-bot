# job-scan-bot

自动化职位匹配助手：抓取职位（LinkedIn + 4个ATS平台的公开职位板），用 Claude API 评估
和你简历的匹配度，结果写入 Notion，高分职位推 Discord 提醒。

**⚠️ 使用须知**：`linkedin_scan.py` 用 [python-jobspy](https://github.com/speedyapply/JobSpy)
直接抓取 LinkedIn 网页，**不是官方授权的API**，存在被限流/临时封锁IP的风险。建议在本地
电脑上跑，不要放在 GitHub Actions 等共享IP环境上。`ats_scan.py` 走的是
Greenhouse/Lever/Ashby/SmartRecruiters 各自的公开API，属于官方支持的调用方式，风险低很多。

## 这个项目做什么

- `linkedin_scan.py`：用 jobspy 抓 LinkedIn 上西雅图地区 + Remote 的职位
- `ats_scan.py`：抓一批公司在 Greenhouse/Lever/Ashby/SmartRecruiters 上的职位板
- `h1b_filter.py` + `h1b_ats_match.py`：从美国劳工部公开的 H-1B 披露数据里筛出目标公司清单，
  再去匹配这些公司具体用的是哪个 ATS 平台（这一步是可选的，如果你想扩展 `ats_scan.py` 的抓取范围）

两个抓取脚本共用同一个 Notion 数据库，按职位链接去重，不会互相重复评估。

## 效果预览

> `![Notion看板截图](docs/notion-preview.png)`

## 前置准备

跑之前需要三样东西：

1. **Anthropic API Key**——去 [console.anthropic.com](https://console.anthropic.com/) 注册并创建一个API Key。
   **注意：这是单独的开发者平台账号，跟你平时用的 claude.ai 网页版/App 是两个系统**，
   网页版订阅不能直接用来调用API，需要在这个开发者平台单独付费/充值。
   费用不高：默认用的是 Haiku 模型，单条职位评估大概几厘钱人民币，一次性跑几百条
   预计几美元。
2. **Notion 账号**——用来存储和追踪抓到的职位（见下方 Quickstart 里的具体设置步骤）
3. （可选）**Discord Webhook**——如果想要高分职位实时推送提醒

## 5分钟跑起来

```bash
git clone <this-repo>
cd job-scan-bot
chmod +x setup.sh
./setup.sh
```

`setup.sh` 会自动建虚拟环境、装依赖、交互式问你要填的 API key，生成 `.env`。

还需要你手动做两件事：

1. **填 `config/profile.md`**——写你的简历背景和筛选标准（模板里有格式说明）
2. **建 Notion 数据库**，加这些字段（类型要对应）：

   | 字段名 | 类型 |
   |---|---|
   | Title | Title |
   | Company | Text |
   | Location | Text |
   | JD Link | URL |
   | Match Score | Number |
   | Recommendation | Select（选项：YES, NO） |
   | Key Gaps | Text |
   | Reasoning | Text |
   | Resume Tips | Text |
   | Applied | Checkbox |
   | Status | Select（选项：New, Reviewed, Applied, Rejected） |
   | Source | Select（选项：linkedin, greenhouse, lever, ashby, smartrecruiters） |
   | Possible PERM | Checkbox |
   | Posted Date | Date |
   | Date Added | Created time |

   建好后，去 [notion.so/my-integrations](https://www.notion.so/my-integrations) 创建一个
   internal integration 拿到 Token（`setup.sh` 里要填的那个），再回数据库页面右上角
   `...` → `Connections`，把这个 integration 加进去。

3. 跑一次预览（不花钱，不会真的调用 Claude）确认没问题：

```bash
source venv/bin/activate
python linkedin_scan.py --dry-run
```

看 `linkedin_preview.csv` 里的结果符合预期，就可以去掉 `--dry-run` 正式跑：

```bash
python linkedin_scan.py
```

## 扩展：接入更多公司（ats_scan.py）

`ats_scan.py` 需要一份 `company,platform,slug` 格式的 CSV 清单（默认找 `h1b_ats_matches.csv`）
才能知道要抓哪些公司。生成这份清单：

```bash
# 1. 去 https://www.dol.gov/agencies/eta/foreign-labor/performance 下载 LCA披露数据(xlsx)
python h1b_filter.py 你下载的文件.xlsx
# 2. 拿刚才的输出，匹配这些公司用的ATS平台（这一步会跑几分钟到十几分钟）
python h1b_ats_match.py h1b_sponsors_seattle_software.csv
# 3. 跑ats_scan
python ats_scan.py --dry-run
```

## （可选）设置定时自动运行

不想每次手动跑，也可以用 crontab 设置成按固定间隔自动执行——**这一步完全是可选的**，
不设置的话，你随时手动运行 `python linkedin_scan.py` / `python ats_scan.py` 就行，
效果一样，只是需要自己记得跑。

如果想自动化：

```bash
crontab -e
```
加一行，按自己的需要设置抓取频率（下面是每6小时跑一次的例子）：
```
0 */6 * * * /完整路径/job-scan-bot/run.sh
```

**注意**：如果改了抓取间隔，记得同步调整 `.env` 里的 `HOURS_OLD`（LinkedIn 只抓多少
小时内发布的职位），让它和你的运行间隔保持一致——比如改成每12小时跑一次，
`HOURS_OLD` 也建议改成 12 左右，不然要么漏掉职位，要么重复扫描太多旧数据。

## 配置项

所有可调参数见 `.env.example`，比较重要的几个：

- `MATCH_THRESHOLD`：超过这个分数才推 Discord 提醒（Notion 里所有结果都会写入，不受这个影响）
- `HOURS_OLD`：LinkedIn 只抓多少小时内发布的职位，建议和你的运行间隔保持一致
- `MAX_JOBS_PER_COMPANY` / `MAX_JOBS_TO_EVALUATE`（`ats_scan.py`）：控制单次运行的评估上限，
  避免意外产生过高的 API 费用

默认用的模型是 `claude-haiku-4-5-20251001`（便宜、速度快，够用于这种结构化打分任务）。
如果想换成别的模型，直接改 `linkedin_scan.py` / `ats_scan.py` 里 `evaluate_job()` 函数中
`model=` 这一行，目前没有做成环境变量。

## 遇到问题

常见报错和排查方法见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

## License

MIT
