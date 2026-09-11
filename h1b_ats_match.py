"""
h1b_ats_match.py
-----------------
接着 h1b_filter.py 的产出往下走：
  1. 合并公司名大小写不一致导致的重复统计（如 Amazon.com Services LLC / AMAZON.COM SERVICES LLC）
  2. 剔除已知走自建招聘系统的大厂（Amazon/Microsoft/Meta/Google/Apple这类，
     不会出现在Greenhouse/Lever这类第三方ATS上，已经被LinkedIn那条线覆盖了）
  3. 对剩下的公司，尝试猜测它们在 Greenhouse / Lever / Ashby / SmartRecruiters 上的slug，
     调用各平台官方公开接口（boards-api.greenhouse.io / api.lever.co /
     api.ashbyhq.com / api.smartrecruiters.com）确认是否命中

用法：
    pip install requests
    python h1b_ats_match.py h1b_sponsors_seattle_software.csv
"""
import re
import sys
import time
import csv
import requests

if len(sys.argv) < 2:
    print("用法: python h1b_ats_match.py h1b_sponsors_seattle_software.csv")
    sys.exit(1)

input_path = sys.argv[1]

# 已知走自建招聘系统的大厂，直接排除（不用管公司名后面各种法律实体后缀的写法）
EXCLUDE_KEYWORDS = [
    "amazon", "microsoft", "meta platforms", "facebook", "google", "alphabet",
    "apple inc",
]

LEGAL_SUFFIXES = [
    "inc", "llc", "corporation", "corp", "co", "ltd", "l l c", "l.l.c",
    "incorporated", "company", "usa", "us", "technologies", "technology",
]


def normalize_name(name: str) -> str:
    """去掉大小写和常见法律后缀差异，用来合并重复统计。"""
    n = name.lower().strip()
    n = re.sub(r"[.,]", "", n)
    words = n.split()
    words = [w for w in words if w not in LEGAL_SUFFIXES]
    return " ".join(words)


def guess_slugs(normalized_name: str) -> list:
    """猜测公司在ATS平台上可能用的slug，按常见命名习惯生成几种候选。"""
    compact = normalized_name.replace(" ", "")
    hyphenated = normalized_name.replace(" ", "-")
    first_word = normalized_name.split()[0] if normalized_name.split() else ""
    candidates = [compact, hyphenated, first_word]
    # 去重、去空
    seen = set()
    result = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def check_greenhouse(slug: str) -> bool:
    try:
        resp = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            timeout=8,
        )
        return resp.status_code == 200 and len(resp.json().get("jobs", [])) > 0
    except Exception:
        return False


def check_lever(slug: str) -> bool:
    try:
        resp = requests.get(
            f"https://api.lever.co/v0/postings/{slug}?mode=json",
            timeout=8,
        )
        return resp.status_code == 200 and len(resp.json()) > 0
    except Exception:
        return False


def check_ashby(slug: str) -> bool:
    try:
        resp = requests.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            timeout=8,
        )
        return resp.status_code == 200 and len(resp.json().get("jobs", [])) > 0
    except Exception:
        return False


def check_smartrecruiters(slug: str) -> bool:
    try:
        resp = requests.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            timeout=8,
        )
        return resp.status_code == 200 and len(resp.json().get("content", [])) > 0
    except Exception:
        return False


# ---------- 1. 读取并合并重复 ----------

merged = {}  # normalized_name -> {"display_name": ..., "count": ...}
with open(input_path, "r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        raw_name = row["EMPLOYER_NAME"]
        count = int(row["lca_count"])
        norm = normalize_name(raw_name)
        if norm not in merged:
            merged[norm] = {"display_name": raw_name, "count": 0}
        merged[norm]["count"] += count
        # 保留看起来更"正常大小写"的那个作为展示名（简单启发：不是全大写的优先）
        if raw_name != raw_name.upper():
            merged[norm]["display_name"] = raw_name

print(f"合并大小写重复后：{len(merged)} 家公司（原始 {sum(1 for _ in open(input_path)) - 1} 行）")

# ---------- 2. 剔除自建招聘系统的大厂 ----------

filtered = {
    norm: info for norm, info in merged.items()
    if not any(kw in norm for kw in EXCLUDE_KEYWORDS)
}
print(f"剔除自建招聘系统大厂后：{len(filtered)} 家公司")

# 按LCA数量排序，全部公司都做ATS匹配（不做数量截断——如果清单很大，
# 跑起来可能要几十分钟，可以自己加一个截断逻辑做快速测试）
sorted_companies = sorted(filtered.items(), key=lambda x: x[1]["count"], reverse=True)
to_check = sorted_companies
print(f"共 {len(to_check)} 家公司做ATS匹配（数量较大时可能要跑几十分钟，请耐心等）\n")

# ---------- 3. 逐个尝试匹配Greenhouse/Lever ----------

hits = []
for i, (norm, info) in enumerate(to_check):
    for slug in guess_slugs(norm):
        if check_greenhouse(slug):
            hits.append({"company": info["display_name"], "platform": "greenhouse", "slug": slug, "lca_count": info["count"]})
            break
        if check_lever(slug):
            hits.append({"company": info["display_name"], "platform": "lever", "slug": slug, "lca_count": info["count"]})
            break
        if check_ashby(slug):
            hits.append({"company": info["display_name"], "platform": "ashby", "slug": slug, "lca_count": info["count"]})
            break
        if check_smartrecruiters(slug):
            hits.append({"company": info["display_name"], "platform": "smartrecruiters", "slug": slug, "lca_count": info["count"]})
            break
        time.sleep(0.3)  # 别对这几个公开API打太快
    if (i + 1) % 20 == 0:
        print(f"已检查 {i + 1}/{len(to_check)}，目前命中 {len(hits)} 家")

# ---------- 4. 输出结果 ----------

output_path = "h1b_ats_matches.csv"
with open(output_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["company", "platform", "slug", "lca_count"])
    writer.writeheader()
    for h in sorted(hits, key=lambda x: x["lca_count"], reverse=True):
        writer.writerow(h)

print(f"\n完成！命中 {len(hits)} 家公司，已保存到 {output_path}")
for h in sorted(hits, key=lambda x: x["lca_count"], reverse=True)[:30]:
    print(f"  [{h['platform']}] {h['company']} (slug: {h['slug']}, LCA数: {h['lca_count']})")
