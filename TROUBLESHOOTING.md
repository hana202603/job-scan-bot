# 常见问题排查

## `KeyError: 'ANTHROPIC_API_KEY'` 或类似报错
`.env` 没有被正确加载。确认：
- `.env` 文件和 `linkedin_scan.py`/`ats_scan.py` 在同一目录下
- 用 `./run.sh` 跑（会自动加载 `.env`），或者确认脚本里有 `load_dotenv()` 这一步

## Greenhouse抓到的职位描述是空的
Greenhouse 的职位列表接口默认不返回完整描述，必须在URL后面加 `?content=true` 参数。
`ats_scan.py` 里已经处理了这个，如果你自己另外写抓取逻辑要注意这一点。

## SmartRecruiters 抓到的职位数量明显偏少
这个接口默认分页，一次只返回一页（可能只有10-20条）。需要用 `limit` + `offset` 参数循环
翻页，直到取完 `totalFound` 字段标注的总数。`ats_scan.py` 里的 `fetch_smartrecruiters_jobs`
已经处理了分页。

## Remote 职位没有被正确识别/消失了
jobspy 抓到的真正 Remote 职位，`location` 字段经常是 `"United States"` 这种国家级字符串，
不是字面的 "Remote"。要同时检查 jobspy 返回的 `is_remote` 布尔字段，不能只看 location 文本。

## 地点判断把不相关城市误判成西雅图地区（或反过来）
用简单的城市名子串匹配有个天生的坑：不少城市名在多个州都存在（比如 Des Moines 在爱荷华州
是首府，在华盛顿州也有个同名小镇；Auburn、Covington、Kent 也都是各州常见地名）。同理
"Washington" 这个词，"Washington State" 和 "Washington, D.C." 是完全不同的地方。

这个问题没有完美解法，只能取舍：
- 想要更精确：用州代码（`, WA`）配合词边界正则判断，牺牲一些召回率
- 想要不漏掉机会：保留完整城市名单，接受偶尔混入不相关地区的结果，靠 Claude 评估这一步
  再做最终判断

## Ashby 的 remote 标记不可靠
实测发现部分公司（尤其体量较大的科技公司）在 Ashby 后台，会把大量岗位统一标记成
`isRemote: true`，不管这个岗位实际是不是 Hybrid/Onsite——像是把这个字段当"公司整体支持
远程文化"的笼统标记在用，不是"这条职位确实100%远程"的精确信息。如果发现地点过滤后
混入大量跟目标地区无关的结果，可以考虑不信任这个平台的 remote 标记，只靠 location 文本判断。

## 一家公司在多个城市都有办公室，只抓到了一个地点
Ashby 有个 `secondaryLocations` 数组字段，装着除主 `location` 之外的其他办公地点。
只读主字段会漏掉这部分信息，需要把两者拼接起来再做地点判断。

## 被限流 / 429 报错
`ats_scan.py` 内置了限流退避（遇到429等一段时间自动重试一次）。如果频繁出现，可以调大
`RETRY_DELAY_SECONDS`，或者调小 `ATS_CONCURRENCY` 降低并发量。

## Notion 写入报 400 错误
最常见的原因：
- 某个 rich_text 字段超过2000字符（Notion的硬性上限）——代码里已经有截断处理，如果自己加了
  新字段要记得同样处理
- 日期字段格式不对——空字符串或 `"None"`/`"nan"` 这类无效值不能直接传给 Notion 的 date
  属性，要么留空整个属性、要么确保是合法的 ISO 8601 格式

## 想先看看结果再花钱评估
两个脚本都支持 `--dry-run`，会走完整个抓取+过滤流程，导出预览CSV，但不会调用 Claude、
不会写 Notion：

```bash
python linkedin_scan.py --dry-run
python ats_scan.py --dry-run
```

## 一次运行的费用大概多少
用 Haiku 模型，单条职位评估大概几厘钱人民币的量级（几毫美分）。首次全量运行如果候选量
较大（几百条），预计几美元；日常增量运行因为有去重，成本会低很多。可以用
`MAX_JOBS_TO_EVALUATE` 提前设一个上限控制单次花费。
