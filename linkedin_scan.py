"""
linkedin_scan.py（本地版 / jobspy）
------------
  1. 用 python-jobspy 直接抓取 LinkedIn 上西雅图地区 + Fully Remote 的 SDE 职位
     （不经过第三方聚合API，精准度更高，但请注意：这是直接抓取LinkedIn网页，
     不是官方授权渠道，存在被限流/封锁IP的风险——所以刻意选择在本地电脑跑，
     不放GitHub Actions等共享IP环境上，降低被墙概率，但依然不是零风险）
  2. 和 Notion 数据库里已有的记录去重（按 JD 链接）
  3. 对每条新职位调用 Claude API，用 tool use 强制返回结构化评估结果，
     并在高匹配度时额外生成简历定制化建议
  4. 全部结果写入 Notion（长期归档 / 投递追踪）
  5. match_score 超过阈值的，额外推一条 discord 提醒（带直达投递链接 + 简历建议）

可以手动运行，也可以配合 crontab 定时自动跑（可选，具体间隔自己按需设置，
见 README；.env 里的 HOURS_OLD 建议跟你实际的运行间隔保持一致）。

需要的环境变量见同目录下的 .env.example。
"""

import os
import re
import csv
import sys
import time
import random
import requests
import anthropic
import subprocess
import pandas as pd
from jobspy import scrape_jobs
from dotenv import load_dotenv

load_dotenv()

# ---------- 配置 ----------

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")  # 可选，不填就不推Discord

MATCH_THRESHOLD = int(os.environ.get("MATCH_THRESHOLD", "70"))
SEARCH_TERM = os.environ.get("SEARCH_TERM", "Software Engineer")
SEATTLE_LOCATION = os.environ.get("SEATTLE_LOCATION", "Seattle, WA")
SEATTLE_DISTANCE = int(os.environ.get("SEATTLE_DISTANCE", "25"))  # 英里，覆盖Bellevue/Redmond
HOURS_OLD = int(os.environ.get("HOURS_OLD", "24"))  # 建议和你的cron间隔一致
RESULTS_WANTED_PER_QUERY = int(os.environ.get("RESULTS_WANTED_PER_QUERY", "25"))

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "config", "profile.md")

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# 跟ats_scan.py保持一致的标题排除词、软件相关关键词、JD正文排除正则
SOFTWARE_KEYWORDS = [
    "software", "developer", "programmer",
    "software engineer", "software developer",
    "backend engineer", "back-end engineer", "backend developer",
    "full stack engineer", "full-stack engineer", "full stack developer",
    "application developer", "application engineer",
    "ai engineer", "forward deployed engineer",
    "sde", "sdet", "swe",
]

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


def is_software_related(title: str) -> bool:
    t = (title or "").lower()
    if any(kw in t for kw in EXCLUDE_KEYWORDS):
        return False
    return any(kw in t for kw in SOFTWARE_KEYWORDS)


def is_jd_excluded(description: str) -> bool:
    if not description:
        return False
    return bool(_JD_EXCLUDE_REGEX.search(description))


# ---------- 1. 抓取职位源（jobspy，直接抓LinkedIn）----------
# 分两次查询：一次西雅图周边（用distance覆盖Bellevue/Redmond等通勤圈），
# 一次Fully Remote。两次调用之间加个随机延迟，别一下打太快。

SEATTLE_AREA_KEYWORDS = [
    "seattle", "bellevue", "redmond", "kirkland", "bothell", "renton",
    "tacoma", "remote",
    "issaquah", "woodinville", "sammamish", "mercer island", "shoreline",
    "kent", "auburn", "federal way", "everett", "lynnwood", "puyallup",
    "burien", "seatac", "tukwila", "newcastle", "snoqualmie", "north bend",
    "duvall", "carnation", "maple valley", "covington", "des moines",
    "normandy park", "edmonds", "mountlake terrace", "mill creek", "marysville",
]
_DC_INDICATOR = re.compile(r"washington,?\s*d\.?c\.?|district of columbia")


def _mentions_wa_state(loc: str) -> bool:
    if "washington" not in loc:
        return False
    if _DC_INDICATOR.search(loc):
        return False
    return True


def is_relevant_location(location: str, is_remote: bool = False) -> bool:
    """空Location默认保留（可能是Remote职位没填这个字段，保守起见不误杀）。
    is_remote：jobspy自带的remote标记，只要为True就直接判定相关，不用再看
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

def fetch_jobs():
    all_rows = []

    try:
        df_seattle = scrape_jobs(
            site_name=["linkedin"],
            search_term=SEARCH_TERM,
            location=SEATTLE_LOCATION,
            distance=SEATTLE_DISTANCE,
            hours_old=HOURS_OLD,
            results_wanted=RESULTS_WANTED_PER_QUERY,
            linkedin_fetch_description=True,
        )
        seattle_rows = df_seattle.to_dict(orient="records")
        seattle_rows = [r for r in seattle_rows if is_relevant_location(str(r.get("location", "")), bool(r.get("is_remote")))]
        seattle_rows = [r for r in seattle_rows if is_software_related(str(r.get("title", "")))]
        seattle_rows = [r for r in seattle_rows if not is_jd_excluded(str(r.get("description", "")))]
        all_rows.extend(seattle_rows)
    except Exception as e:
        print(f"[warn] 抓取西雅图职位失败: {e}", file=sys.stderr)

    time.sleep(random.uniform(3, 8))  # 别连续打两次请求

    try:
        df_remote = scrape_jobs(
            site_name=["linkedin"],
            search_term=SEARCH_TERM,
            is_remote=True,
            hours_old=HOURS_OLD,
            results_wanted=RESULTS_WANTED_PER_QUERY,
            linkedin_fetch_description=True,
        )
        remote_rows = df_remote.to_dict(orient="records")
        remote_rows = [r for r in remote_rows if is_relevant_location(str(r.get("location", "")), bool(r.get("is_remote")))]
        remote_rows = [r for r in remote_rows if is_software_related(str(r.get("title", "")))]
        remote_rows = [r for r in remote_rows if not is_jd_excluded(str(r.get("description", "")))]
        all_rows.extend(remote_rows)
    except Exception as e:
        print(f"[warn] 抓取Remote职位失败: {e}", file=sys.stderr)

    jobs = []
    seen_urls = set()  # 同一次运行内，西雅图和Remote结果可能重叠，先去重一次
    for raw in all_rows:
        job = parse_job(raw)
        if job and job["url"] not in seen_urls:
            seen_urls.add(job["url"])
            jobs.append(job)
    return jobs


def parse_job(raw: dict):
    """把jobspy返回的原始字段，统一成脚本内部用的结构。"""
    try:
        url = raw.get("job_url")
        title = raw.get("title")
        company = raw.get("company")
        if not url or not title or not company:
            return None
        date_posted = raw.get("date_posted")
        if pd.isna(date_posted):
            posted_at = ""
        elif hasattr(date_posted, "isoformat"):
            posted_at = date_posted.isoformat()
        else:
            posted_at = str(date_posted)
        return {
            "title": title,
            "company": company,
            "location": raw.get("location", "") or ("Remote" if raw.get("is_remote") else ""),
            "posted_at": posted_at,
            "url": url,  # 去重用这个字段
            "description": raw.get("description", "") or "",
        }
    except Exception as e:
        print(f"[warn] 职位字段解析失败，跳过: {e}", file=sys.stderr)
        return None


# ---------- 2. 去重：查 Notion 里已有哪些链接 ----------

def get_existing_urls():
    urls = set()
    payload = {"page_size": 100}
    while True:
        resp = requests.post(
            f"{NOTION_BASE}/databases/{NOTION_DATABASE_ID}/query",
            headers=_notion_headers(),
            json=payload,
            timeout=30,
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


# ---------- 3. Claude 结构化评估 ----------

EVAL_TOOL = {
    "name": "submit_evaluation",
    "description": "提交对这条职位与候选人简历匹配度的结构化评估，匹配度高时附带简历定制建议",
    "input_schema": {
        "type": "object",
        "properties": {
            "match_score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "0-100，候选人与该职位的匹配程度",
            },
            "apply_recommendation": {
                "type": "string",
                "enum": ["YES", "NO"],
            },
            "key_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "候选人简历与职位要求之间的主要差距，没有则给空数组",
            },
            "reasoning": {
                "type": "string",
                "description": "一到两句话说明打分理由，控制在500字符以内",
            },
            "resume_tailoring_tips": {
                "type": "string",
                "description": (
                    "如果 apply_recommendation 是 YES，给3条针对这个JD的简历修改建议"
                    "（比如哪些项目描述该往前调、该突出哪些关键词/技术栈），"
                    "控制在1500字符以内；"
                    "如果是 NO，留空字符串即可"
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


def load_profile():
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def evaluate_job(job: dict, profile_text: str) -> dict:
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        temperature=0,
        tools=[EVAL_TOOL],
        tool_choice={"type": "tool", "name": "submit_evaluation"},
        messages=[
            {
                "role": "user",
                "content": (
                    "下面是候选人的背景/筛选标准，以及一条职位描述。"
                    "请评估匹配度并调用 submit_evaluation 提交结果。\n\n"
                    "特别注意：如果 apply_recommendation 是 YES，"
                    "resume_tailoring_tips 字段必须填写至少3条具体的简历修改建议"
                    "（比如该突出哪些项目/关键词、该往简历前面调整哪些经历），不能留空，"
                    "且总长度不超过1500字符。\n\n"
                    f"### 候选人背景与筛选标准\n{profile_text}\n\n"
                    f"### 职位信息\n"
                    f"职位: {job['title']}\n公司: {job['company']}\n地点: {job['location']}\n\n"
                    f"### 职位描述\n{job['description']}"
                ),
            }
        ],
    )
    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_evaluation":
            return block.input
    raise RuntimeError("Claude 没有返回预期的工具调用结果")


# ---------- 4. 写入 Notion ----------
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
        "Key Gaps": {
            "rich_text": [{"text": {"content": _truncate("; ".join(evaluation.get("key_gaps", [])))}}]
        },
        "Reasoning": {"rich_text": [{"text": {"content": _truncate(evaluation.get("reasoning", ""))}}]},
        "Resume Tips": {
            "rich_text": [{"text": {"content": _truncate(evaluation.get("resume_tailoring_tips", ""))}}]
        },
        "Applied": {"checkbox": False},
        "Status": {"select": {"name": "New"}},
        "Source": {"select": {"name": "linkedin"}},
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


# ---------- 5. Discord 高分提醒 ----------

def send_discord_alert(job: dict, evaluation: dict):
    if not DISCORD_WEBHOOK_URL:
        return
    tips = evaluation.get('resume_tailoring_tips') or '无'
    tips_preview = tips[:300] + "...(完整建议见Notion)" if len(tips) > 300 else tips

    content = (
        f"**{job['title']}** @ {job['company']} ({job['location']})\n"
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
        f"✅ **linkedin_scan.py 本轮运行完成**\n"
        f"抓到 {total_fetched} 条，新增 {new_count} 条评估，"
        f"其中 {high_match_count} 条超过阈值（{MATCH_THRESHOLD}%）"
        + (f"，{failed_count} 条评估失败" if failed_count else "")
    )
    requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)


# ---------- 6. 跑完的反馈：本地弹窗 ----------

def send_macos_notification(title: str, message: str):
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            timeout=10,
        )
    except Exception as e:
        print(f"[warn] 本地通知发送失败（不影响主流程）: {e}", file=sys.stderr)

# ---------- helpers ----------

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
    all_jobs = fetch_jobs()
    print(f"抓到 {len(all_jobs)} 条职位")

    existing_urls = get_existing_urls()
    new_jobs = [j for j in all_jobs if j["url"] not in existing_urls]
    print(f"其中 {len(new_jobs)} 条是新的")

    if dry_run:
        preview_path = "linkedin_preview.csv"
        with open(preview_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["title", "company", "location", "posted_at", "description_length", "url"])
            writer.writeheader()
            for j in new_jobs:
                writer.writerow({
                    "title": j["title"],
                    "company": j["company"],
                    "location": j["location"],
                    "posted_at": j.get("posted_at", ""),
                    "description_length": len(j.get("description", "")),
                    "url": j["url"],
                })
        print(f"[预览模式] 不会调用Claude评估、不会写Notion，只导出候选职位表格。")
        print(f"共 {len(new_jobs)} 条即将送去评估的职位，已保存到 {preview_path}，可以打开看看有没有问题。")
        return

    print("开始评估")

    high_match_count = 0
    failed_count = 0

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
    print(f"完成 — {summary}")
    send_macos_notification("linkedin_scan 跑完了", summary)
    send_discord_summary(len(all_jobs), len(new_jobs), high_match_count, failed_count)


if __name__ == "__main__":
    main()
