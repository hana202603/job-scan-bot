"""
ats_scan.py（本地版）
---------------------
读取 h1b_ats_matches.csv（公司名 + platform + slug 清单），
按平台分别调用 Greenhouse / Lever / Ashby / SmartRecruiters 的官方公开接口，
抓取每家公司当前所有在招职位，做标题关键词过滤（只留软件相关），
和 Notion 数据库里已有的记录去重（按 JD 链接，跟linkedin_scan.py共用同一个Notion数据库，
所以不会跟LinkedIn那条线重复评估同一个URL），对新职位调用Claude评估，
写入Notion（带Source字段标记来源），高分职位推Discord提醒。

不依赖发布日期做过滤——用"URL有没有在Notion里出现过"作为唯一判断依据，
首次运行会抓到每家公司当前全部在招的软件相关职位，之后每天重跑只评估新出现的。

并发+限流处理：用线程池控制总并发数（默认6），遇到429（限流）响应会自动退避
等待后重试一次；其他网络异常（超时等）同样重试一次。某家公司这次抓取失败了，
不会有额外的"定时重试"机制——因为脚本本身就是要重复运行的，下次整体重跑时
会自然重新尝试，不需要额外状态记录。

需要的环境变量见 .env，额外用到（都有默认值，不填也能跑）：
    ATS_MATCHES_PATH=h1b_ats_matches.csv
    MAX_JOBS_PER_COMPANY=0        # 0表示不限制
    ATS_CONCURRENCY=6             # 同时抓取的公司数上限
    RETRY_DELAY_SECONDS=5         # 请求失败/被限流后，重试前等待的秒数

用法：
    python ats_scan.py
"""

import os
import re
import sys
import time
import csv
import subprocess
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import anthropic
from dotenv import load_dotenv

load_dotenv()

# ---------- 配置 ----------

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

MATCH_THRESHOLD = int(os.environ.get("MATCH_THRESHOLD", "70"))
ATS_MATCHES_PATH = os.environ.get("ATS_MATCHES_PATH", "h1b_ats_matches.csv")
MAX_JOBS_PER_COMPANY = int(os.environ.get("MAX_JOBS_PER_COMPANY", "0"))  # 0=不限制
ATS_CONCURRENCY = int(os.environ.get("ATS_CONCURRENCY", "6"))
RETRY_DELAY_SECONDS = int(os.environ.get("RETRY_DELAY_SECONDS", "5"))
MIN_DESCRIPTION_LENGTH = int(os.environ.get("MIN_DESCRIPTION_LENGTH", "50"))
ATS_TEST_LIMIT = int(os.environ.get("ATS_TEST_LIMIT", "0"))  # 0=不限制，测试时设个小数字
MAX_JOBS_TO_EVALUATE = int(os.environ.get("MAX_JOBS_TO_EVALUATE", "0"))  # 0=不限制，测试时能精确控制这次最多评估几条

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "config", "profile.md")

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SOFTWARE_KEYWORDS = [
    "software", "developer", "programmer",
    "software engineer", "software developer",
    "backend engineer", "back-end engineer", "backend developer",
    "full stack engineer", "full-stack engineer", "full stack developer",
    "application developer", "application engineer",
    "ai engineer", "forward deployed engineer",
    "sde", "sdet", "swe",
]

# 标题里带这些词的，不管是否匹配SOFTWARE_KEYWORDS，一律排除
EXCLUDE_KEYWORDS = [
    # 头衔/职级类——明显不是IC岗位，或职级过高
    "manager", "chief", "vp", "vice president", "president", "director", "head of",
    "principal", "staff",
    # 职能类——带software/engineer但实际不是工程IC岗
    "sales", "marketing", "recruiter", "trainer", "hr business partner",
    "solutions engineer", "data engineer", "technical support", "support engineer",
    "machine learning engineer",
    # 经验层级/项目性质类
    "intern", "internship", "new grad", "early career", "entry level", "entry-level",
    "bootcamp", "apprenticeship", "fellowship",
    # 安全许可/身份要求类
    "clearance", "secret", "ts/sci", "public trust", "security clearance",
    "us citizen", "u.s. citizen", "green card only", "citizenship required",
]

# JD正文层面的排除正则：公民/绿卡要求、9年以上经验门槛、知识图谱类专业术语
# （这几类误伤概率低，可以在送Claude评估之前就机械式判断掉，省评估成本）
JD_EXCLUDE_PATTERNS = [
    r"must be (a )?us citizen",
    r"us citizen(s)? or permanent resident",
    r"green card or us citizen",
    r"citizenship and eligibility for a? ?u\.?s\.? government",
    r"eligib(le|ility) for a? ?(u\.?s\.? )?(government )?secret",
    r"active secret (or higher )?clearance",
    r"secret or higher clearance",
    r"security clearance (is )?required",
    r"\bts/sci\b", r"\bpublic trust\b",
    r"\b(9|1\d|2\d)\+?\s*years?\s*(of\s*)?experience",
    r"\brdf\b", r"\bsparql\b", r"\br2rml\b", r"ontology modeling",
]
_JD_EXCLUDE_REGEX = re.compile("|".join(JD_EXCLUDE_PATTERNS), re.IGNORECASE)

_debug_printed = {"greenhouse": False, "lever": False, "ashby": False, "smartrecruiters": False, "smartrecruiters_detail": False}


def _debug_once(key: str, raw):
    if not _debug_printed.get(key, False):
        print(f"\n[debug] {key} 原始数据样例:\n{raw}\n")
        _debug_printed[key] = True


def is_software_related(title: str) -> bool:
    t = (title or "").lower()
    if any(kw in t for kw in EXCLUDE_KEYWORDS):
        return False
    return any(kw in t for kw in SOFTWARE_KEYWORDS)


# 跟linkedin_scan.py保持一致，用来过滤ATS抓来的职位地点——保留完整城市名单，
# 不剔除容易跟其他州撞车的名字（Auburn/Covington/Des Moines等），
# 按你的要求优先保证不漏掉西雅图地区职位，接受偶尔混入其他地区
SEATTLE_AREA_KEYWORDS = [
    "seattle", "bellevue", "redmond", "kirkland", "bothell", "renton",
    "tacoma", "remote",
    "issaquah", "woodinville", "sammamish", "mercer island", "shoreline",
    "kent", "auburn", "federal way", "everett", "lynnwood", "puyallup",
    "burien", "seatac", "tukwila", "newcastle", "snoqualmie", "north bend",
    "duvall", "carnation", "maple valley", "covington", "des moines",
    "normandy park", "edmonds", "mountlake terrace", "mill creek", "marysville",
]
# "washington"单独处理：这个词会跟"Washington, D.C."(华盛顿特区，联邦首都，
# 跟华盛顿州完全是两个地方)撞车，所以不能简单当子串匹配，要求"washington"
# 出现、且附近没有紧跟着"d.c."/"dc"/"district of columbia"这类特区标识
_DC_INDICATOR = re.compile(r"washington,?\s*d\.?c\.?|district of columbia")


def _mentions_wa_state(loc: str) -> bool:
    if "washington" not in loc:
        return False
    if _DC_INDICATOR.search(loc):
        return False  # 是华盛顿特区，不是华盛顿州，排除
    return True


def is_relevant_location(location: str, is_remote: bool = False) -> bool:
    """空Location默认保留（可能是Remote职位没填这个字段，保守起见不误杀）。
    is_remote：平台自带的remote标记，只要为True就直接判定相关，不用再看
    location字符串里有没有写"remote"字样（解决"United States"这类没写
    "remote"但确实是远程职位被漏判的问题）。"""
    if is_remote:
        return True
    loc = (location or "").strip().lower()
    if not loc:
        return True
    if _mentions_wa_state(loc):
        return True
    return any(kw in loc for kw in SEATTLE_AREA_KEYWORDS)


def is_jd_excluded(description: str) -> bool:
    """JD正文里命中公民/绿卡要求、9年以上经验门槛、知识图谱类术语，直接排除。"""
    if not description:
        return False
    return bool(_JD_EXCLUDE_REGEX.search(description))


# ---------- 0. 带重试/限流退避的HTTP请求封装 ----------

def http_get_with_retry(url: str, timeout: int = 15) -> requests.Response | None:
    """
    统一的GET请求封装：
      - 收到429（限流）：等待 RETRY_DELAY_SECONDS 秒后重试一次
      - 其他异常（超时/连接错误等）：同样等待后重试一次
      - 重试后依然失败，返回None，调用方按"这次没抓到"处理，不阻塞整体流程
    """
    for attempt in range(2):  # 最多试2次：第一次 + 1次重试
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 429:
                if attempt == 0:
                    print(f"[warn] 被限流(429)，{RETRY_DELAY_SECONDS}秒后重试: {url}", file=sys.stderr)
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                return None
            return resp
        except requests.RequestException as e:
            if attempt == 0:
                print(f"[warn] 请求异常，{RETRY_DELAY_SECONDS}秒后重试: {url} — {e}", file=sys.stderr)
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            print(f"[warn] 重试后仍然失败，跳过: {url} — {e}", file=sys.stderr)
            return None
    return None


# ---------- 1. 日期清洗：把各平台不同格式的发布时间统一成ISO日期字符串 ----------

def normalize_posted_at(raw_value, platform: str) -> str:
    """缺失/解析失败一律返回空字符串，不让脏数据混进Notion（跟linkedin_scan.py处理jobspy日期的思路一致）。"""
    if raw_value is None or raw_value == "":
        return ""
    try:
        if platform == "lever":
            # createdAt 是毫秒时间戳
            ms = int(raw_value)
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
        if platform in ("greenhouse", "ashby"):
            # 一般已经是ISO8601字符串，比如 2026-08-01T12:34:56-04:00，取日期部分
            return str(raw_value)[:10]
        if platform == "smartrecruiters":
            # releasedDate 一般是 YYYY-MM-DD
            return str(raw_value)[:10]
    except (ValueError, TypeError):
        return ""
    return ""


# ---------- 2. 按平台抓取 ----------

def fetch_greenhouse_jobs(slug: str) -> list:
    resp = http_get_with_retry(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if resp is None or not resp.ok:
        return []
    raw_jobs = resp.json().get("jobs", [])
    if raw_jobs:
        _debug_once("greenhouse", raw_jobs[0])
    parsed = []
    for r in raw_jobs:
        parsed.append({
            "title": r.get("title", ""),
            "location": (r.get("location") or {}).get("name", ""),
            "posted_at": normalize_posted_at(r.get("updated_at"), "greenhouse"),
            "url": r.get("absolute_url", ""),
            "description": r.get("content", "") or "",
            "is_remote": False,  # Greenhouse没有原生remote字段，靠location文本里的"remote"字样判断
        })
    return parsed


def fetch_lever_jobs(slug: str) -> list:
    resp = http_get_with_retry(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if resp is None or not resp.ok:
        return []
    raw_jobs = resp.json()
    if not isinstance(raw_jobs, list):
        return []
    if raw_jobs:
        _debug_once("lever", raw_jobs[0])
    parsed = []
    for r in raw_jobs:
        categories = r.get("categories", {}) or {}
        # workplaceType字段名不完全确定，可能是"remote"/"hybrid"/"onsite"这几个值之一，
        # 字段名和取值范围没有100%把握，靠上面的debug打印核对，不对的话再调整
        workplace_type = str(r.get("workplaceType", "")).lower()
        parsed.append({
            "title": r.get("text", ""),
            "location": categories.get("location", ""),
            "posted_at": normalize_posted_at(r.get("createdAt"), "lever"),
            "url": r.get("hostedUrl", "") or r.get("applyUrl", ""),
            "description": r.get("descriptionPlain", "") or r.get("description", "") or "",
            "is_remote": workplace_type == "remote",
        })
    return parsed


def fetch_ashby_jobs(slug: str) -> list:
    resp = http_get_with_retry(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if resp is None or not resp.ok:
        return []
    raw_jobs = resp.json().get("jobs", [])
    if raw_jobs:
        _debug_once("ashby", raw_jobs[0])
    parsed = []
    for r in raw_jobs:
        url = r.get("jobUrl") or r.get("applyUrl") or ""
        primary_location = r.get("location") or r.get("locationName") or ""
        primary_location = primary_location if isinstance(primary_location, str) else ""
        # secondaryLocations是个数组，可能有0个或多个额外地点，不管有几个都全部拼进来，
        # 确保像"Menlo Park; Bellevue"这种多地点职位不会因为只看了主地点而漏判
        secondary = r.get("secondaryLocations", []) or []
        secondary_names = [s for s in secondary if isinstance(s, str)]
        location = "; ".join(filter(None, [primary_location] + secondary_names))
        parsed.append({
            "title": r.get("title", ""),
            "location": location,
            "posted_at": normalize_posted_at(r.get("publishedAt"), "ashby"),
            "url": url,
            "description": r.get("descriptionHtml", "") or r.get("description", "") or "",
            # 不再信任isRemote这个标记——实测发现这些公司把它当"公司整体支持远程文化"
            # 的笼统旗标在用，不是"这条职位确实远程"的精确标记，89%的职位都被标成True，
            # 明显不可靠。改回只靠location文本里有没有"remote"字样判断。
            "is_remote": False,
        })
    return parsed


def fetch_smartrecruiters_job_detail(slug: str, job_id: str) -> str:
    """SmartRecruiters的列表接口不带完整描述，只有命中标题过滤的职位才值得
    额外调一次详情接口，避免对每条职位都多打一次请求。"""
    resp = http_get_with_retry(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}")
    if resp is None or not resp.ok:
        return ""
    data = resp.json()
    _debug_once("smartrecruiters_detail", data)
    # 字段路径不确定，多试几个候选，拼接能拿到的部分
    job_ad = data.get("jobAd", {}) or {}
    sections = job_ad.get("sections", {}) or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key, {}) or {}
        text = section.get("text", "")
        if text:
            parts.append(text)
    if parts:
        return "\n\n".join(parts)
    # 兜底：有些返回结构可能直接是顶层字段
    return data.get("description", "") or ""


def fetch_smartrecruiters_jobs(slug: str) -> list:
    parsed = []
    offset = 0
    page_size = 100
    while True:
        resp = http_get_with_retry(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={page_size}&offset={offset}"
        )
        if resp is None or not resp.ok:
            break
        data = resp.json()
        raw_jobs = data.get("content", [])
        if raw_jobs:
            _debug_once("smartrecruiters", raw_jobs[0])

        for r in raw_jobs:
            title = r.get("name", "")
            if not is_software_related(title):
                continue  # 先按标题过滤，只有过了的才继续拿详情，省请求
            location_obj = r.get("location", {}) or {}
            location = ", ".join(
                filter(None, [location_obj.get("city"), location_obj.get("region"), location_obj.get("country")])
            )
            job_id = r.get("id", "")
            url = r.get("postingUrl") or f"https://jobs.smartrecruiters.com/{slug}/{job_id}"
            description = fetch_smartrecruiters_job_detail(slug, job_id) if job_id else ""
            parsed.append({
                "title": title,
                "location": location,
                "posted_at": normalize_posted_at(r.get("releasedDate"), "smartrecruiters"),
                "url": url,
                "description": description,
                "is_remote": bool(location_obj.get("remote", False)),  # 确认过的真实字段，之前实测数据里见过
            })

        total_found = data.get("totalFound", 0)
        offset += page_size
        if offset >= total_found or not raw_jobs:
            break

    return parsed


FETCHERS = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
    "smartrecruiters": fetch_smartrecruiters_jobs,
}


# ---------- 3. 并发抓取 + 标题过滤（SmartRecruiters在自己的fetcher里已经过滤过了）----------

def load_companies(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fetch_one_company(row: dict) -> tuple:
    company_name = row["company"]
    platform = row["platform"]
    slug = row["slug"]

    stats = {"raw": 0, "after_title": 0, "after_location": 0, "after_jd_regex": 0, "after_min_desc": 0}

    fetcher = FETCHERS.get(platform)
    if not fetcher:
        return [], stats

    raw_jobs = fetcher(slug)
    stats["raw"] = len(raw_jobs)

    # smartrecruiters的fetcher内部已经做过标题过滤(含EXCLUDE_KEYWORDS)，这里针对其余平台再过滤一遍
    if platform != "smartrecruiters":
        raw_jobs = [j for j in raw_jobs if j["title"] and is_software_related(j["title"])]
    stats["after_title"] = len(raw_jobs)

    # 地点过滤：只留大西雅图地区/Remote
    raw_jobs = [j for j in raw_jobs if is_relevant_location(j.get("location", ""), j.get("is_remote", False))]
    stats["after_location"] = len(raw_jobs)

    # JD正文过滤：公民/绿卡要求、9年以上经验门槛、知识图谱类术语
    raw_jobs = [j for j in raw_jobs if not is_jd_excluded(j.get("description", ""))]
    stats["after_jd_regex"] = len(raw_jobs)

    # 描述过短的跳过不评估——大概率是占位内容/抓取不全，评估质量不可靠
    kept = []
    for j in raw_jobs:
        if len(j.get("description", "")) < MIN_DESCRIPTION_LENGTH:
            print(f"[skip] 描述过短({len(j.get('description', ''))}字符)，跳过: {j['title']} @ {company_name}", file=sys.stderr)
            continue
        kept.append(j)
    raw_jobs = kept
    stats["after_min_desc"] = len(raw_jobs)

    relevant = [j for j in raw_jobs if j["url"]]
    if MAX_JOBS_PER_COMPANY > 0:
        relevant = relevant[:MAX_JOBS_PER_COMPANY]

    for j in relevant:
        j["company"] = company_name
        j["source"] = platform

    return relevant, stats


def fetch_all_ats_jobs() -> list:
    companies = load_companies(ATS_MATCHES_PATH)
    if ATS_TEST_LIMIT > 0:
        companies = companies[:ATS_TEST_LIMIT]
        print(f"[测试模式] 只处理前 {len(companies)} 家公司")
    print(f"读到 {len(companies)} 家公司，并发数={ATS_CONCURRENCY}")

    all_jobs = []
    platform_stats = {}  # platform -> 累加的stats
    done = 0
    with ThreadPoolExecutor(max_workers=ATS_CONCURRENCY) as executor:
        futures = {executor.submit(fetch_one_company, row): row for row in companies}
        for future in as_completed(futures):
            row = futures[future]
            try:
                jobs, stats = future.result()
                all_jobs.extend(jobs)
                platform = row["platform"]
                if platform not in platform_stats:
                    platform_stats[platform] = {"raw": 0, "after_title": 0, "after_location": 0, "after_jd_regex": 0, "after_min_desc": 0}
                for key in platform_stats[platform]:
                    platform_stats[platform][key] += stats[key]
            except Exception as e:
                print(f"[warn] {row['company']} 处理失败: {e}", file=sys.stderr)
            done += 1
            if done % 30 == 0:
                print(f"已处理 {done}/{len(companies)} 家公司，目前累计 {len(all_jobs)} 条相关职位")

    print("\n各平台过滤漏斗（原始 → 标题过滤后 → 地点过滤后 → JD正文过滤后 → 描述长度过滤后）：")
    for platform, s in platform_stats.items():
        print(f"  {platform}: {s['raw']} → {s['after_title']} → {s['after_location']} → {s['after_jd_regex']} → {s['after_min_desc']}")

    seen_urls = set()
    deduped_jobs = []
    for j in all_jobs:
        if j["url"] not in seen_urls:
            seen_urls.add(j["url"])
            deduped_jobs.append(j)
    if len(deduped_jobs) < len(all_jobs):
        print(f"本次运行内部去重：{len(all_jobs) - len(deduped_jobs)} 条重复URL被合并")

    return deduped_jobs


# ---------- 4. 去重：查 Notion 里已有哪些链接（跟linkedin_scan.py共用同一个数据库） ----------

def get_existing_urls() -> set:
    urls = set()
    payload = {"page_size": 100}
    while True:
        resp = requests.post(
            f"{NOTION_BASE}/databases/{NOTION_DATABASE_ID}/query",
            headers=_notion_headers(), json=payload, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            url_prop = page["properties"].get("JD Link", {}).get("url")
            if url_prop:
                urls.add(url_prop)
        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break
    return urls


# ---------- 5. Claude 结构化评估（跟linkedin_scan.py同一套EVAL_TOOL） ----------

EVAL_TOOL = {
    "name": "submit_evaluation",
    "description": "提交对这条职位与候选人简历匹配度的结构化评估，匹配度高时附带简历定制建议",
    "input_schema": {
        "type": "object",
        "properties": {
            "match_score": {"type": "integer", "minimum": 0, "maximum": 100, "description": "0-100，候选人与该职位的匹配程度"},
            "apply_recommendation": {"type": "string", "enum": ["YES", "NO"]},
            "key_gaps": {"type": "array", "items": {"type": "string"}, "description": "候选人简历与职位要求之间的主要差距，没有则给空数组"},
            "reasoning": {"type": "string", "description": "一到两句话说明打分理由，控制在500字符以内"},
            "resume_tailoring_tips": {
                "type": "string",
                "description": (
                    "如果 apply_recommendation 是 YES，给3条针对这个JD的简历修改建议，"
                    "控制在1500字符以内；如果是 NO，留空字符串即可"
                ),
            },
            "possible_perm_ad": {
                "type": "boolean",
                "description": (
                    "该职位是否有PERM/劳工认证广告的迹象（比如精确的'本科+X年 或 硕士+Y年'"
                    "等价条款、要求引用内部职位编码、8个以上互不相关的技术堆砌罗列）。"
                    "这只是标记供人工复核，不代表自动拒绝。"
                ),
            },
        },
        "required": ["match_score", "apply_recommendation", "key_gaps", "reasoning", "resume_tailoring_tips", "possible_perm_ad"],
    },
}


def load_profile() -> str:
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def evaluate_job(job: dict, profile_text: str) -> dict:
    description = job["description"] or "(职位描述为空，请基于标题、公司、地点酌情评估，并在reasoning里说明描述缺失)"
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        temperature=0,
        tools=[EVAL_TOOL],
        tool_choice={"type": "tool", "name": "submit_evaluation"},
        messages=[{
            "role": "user",
            "content": (
                "下面是候选人的背景/筛选标准，以及一条职位描述。"
                "请评估匹配度并调用 submit_evaluation 提交结果。\n\n"
                "特别注意：如果 apply_recommendation 是 YES，"
                "resume_tailoring_tips 字段必须填写至少3条具体的简历修改建议，不能留空，"
                "且总长度不超过1500字符。\n\n"
                f"### 候选人背景与筛选标准\n{profile_text}\n\n"
                f"### 职位信息\n职位: {job['title']}\n公司: {job['company']}\n地点: {job['location']}\n\n"
                f"### 职位描述\n{description}"
            ),
        }],
    )
    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_evaluation":
            return block.input
    raise RuntimeError("Claude 没有返回预期的工具调用结果")


# ---------- 6. 写入 Notion ----------

def _truncate(text: str, limit: int = 2000) -> str:
    return text[:limit - 3] + "..." if len(text) > limit else text


def write_to_notion(job: dict, evaluation: dict):
    properties = {
        "Title": {"title": [{"text": {"content": _truncate(job["title"])}}]},
        "Company": {"rich_text": [{"text": {"content": _truncate(job["company"])}}]},
        "Location": {"rich_text": [{"text": {"content": _truncate(job["location"])}}]},
        "JD Link": {"url": job["url"]},
        "Match Score": {"number": evaluation.get("match_score", 0)},
        "Recommendation": {"select": {"name": evaluation.get("apply_recommendation", "NO")}},
        "Key Gaps": {"rich_text": [{"text": {"content": _truncate("; ".join(evaluation.get("key_gaps", [])))}}]},
        "Reasoning": {"rich_text": [{"text": {"content": _truncate(evaluation.get("reasoning", ""))}}]},
        "Resume Tips": {"rich_text": [{"text": {"content": _truncate(evaluation.get("resume_tailoring_tips", ""))}}]},
        "Applied": {"checkbox": False},
        "Status": {"select": {"name": "New"}},
        "Source": {"select": {"name": job.get("source", "unknown")}},
        "Possible PERM": {"checkbox": evaluation.get("possible_perm_ad", False)},
    }
    if job.get("posted_at"):
        properties["Posted Date"] = {"date": {"start": job["posted_at"]}}

    resp = requests.post(
        f"{NOTION_BASE}/pages",
        headers=_notion_headers(),
        json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties},
        timeout=30,
    )
    if not resp.ok:
        print(f"[error] Notion写入失败: {resp.status_code} {resp.text}", file=sys.stderr)
    resp.raise_for_status()


# ---------- 7. Discord 提醒 + 本地通知 ----------

def send_discord_alert(job: dict, evaluation: dict):
    if not DISCORD_WEBHOOK_URL:
        return
    tips = evaluation.get("resume_tailoring_tips") or "无"
    tips_preview = tips[:300] + "...(完整建议见Notion)" if len(tips) > 300 else tips
    content = (
        f"**{job['title']}** @ {job['company']} ({job['location']}) [来源: {job.get('source', '')}]\n"
        f"匹配度: {evaluation['match_score']}%  |  建议: {evaluation['apply_recommendation']}\n"
        f"差距: {', '.join(evaluation['key_gaps']) or '无明显差距'}\n"
        f"简历建议: {tips_preview}\n"
        f"[直达投递链接]({job['url']})"
    )
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)
    if not resp.ok:
        print(f"[warn] Discord推送失败: {resp.status_code} {resp.text}", file=sys.stderr)


def send_discord_summary(total_fetched: int, new_count: int, high_match_count: int, failed_count: int):
    if not DISCORD_WEBHOOK_URL:
        return
    content = (
        f"✅ **ats_scan.py 本轮运行完成**\n"
        f"抓到 {total_fetched} 条软件相关职位，新增 {new_count} 条评估，"
        f"其中 {high_match_count} 条超过阈值（{MATCH_THRESHOLD}%）"
        + (f"，{failed_count} 条评估失败" if failed_count else "")
    )
    requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)


def send_macos_notification(title: str, message: str):
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            timeout=10,
        )
    except Exception as e:
        print(f"[warn] 本地通知发送失败（不影响主流程）: {e}", file=sys.stderr)


def _notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# ---------- 主流程 ----------

def main():
    dry_run = "--dry-run" in sys.argv

    profile_text = load_profile()

    all_jobs = fetch_all_ats_jobs()
    print(f"\n抓到 {len(all_jobs)} 条软件相关职位（每家上限{MAX_JOBS_PER_COMPANY or '不限'}）")

    existing_urls = get_existing_urls()
    new_jobs = [j for j in all_jobs if j["url"] not in existing_urls]
    if not dry_run and MAX_JOBS_TO_EVALUATE > 0 and len(new_jobs) > MAX_JOBS_TO_EVALUATE:
        print(f"[测试模式] 新职位有{len(new_jobs)}条，只评估前 {MAX_JOBS_TO_EVALUATE} 条")
        new_jobs = new_jobs[:MAX_JOBS_TO_EVALUATE]
    print(f"其中 {len(new_jobs)} 条是新的（跟Notion里已有记录去重后）\n")

    if dry_run:
        preview_path = "ats_preview.csv"
        with open(preview_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["title", "company", "location", "is_remote", "source", "description_length", "url"])
            writer.writeheader()
            for j in new_jobs:
                writer.writerow({
                    "title": j["title"],
                    "company": j["company"],
                    "location": j["location"],
                    "is_remote": j.get("is_remote", False),
                    "source": j["source"],
                    "description_length": len(j.get("description", "")),
                    "url": j["url"],
                })
        print(f"[预览模式] 不会调用Claude评估、不会写Notion，只导出候选职位表格。")
        print(f"共 {len(new_jobs)} 条即将送去评估的职位，已保存到 {preview_path}，可以打开看看有没有问题。")
        return

    high_match_count = 0
    failed_count = 0

    print("开始评估\n")
    for job in new_jobs:
        try:
            evaluation = evaluate_job(job, profile_text)
        except Exception as e:
            print(f"[warn] 评估失败，跳过: {job['title']} @ {job['company']} — {e}", file=sys.stderr)
            failed_count += 1
            continue

        write_to_notion(job, evaluation)

        if evaluation["match_score"] >= MATCH_THRESHOLD:
            high_match_count += 1
            send_discord_alert(job, evaluation)

        time.sleep(1)

    summary = f"抓到{len(all_jobs)}条，新增{len(new_jobs)}条，{high_match_count}条超过阈值"
    print(f"\n完成 — {summary}")
    send_macos_notification("ats_scan 跑完了", summary)
    send_discord_summary(len(all_jobs), len(new_jobs), high_match_count, failed_count)


if __name__ == "__main__":
    main()
