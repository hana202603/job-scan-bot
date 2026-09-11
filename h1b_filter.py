"""
h1b_filter.py
-------------
读取从DOL官网手动下载的LCA披露数据Excel文件（可以一次传多个，比如合并几个季度/财年）
(https://www.dol.gov/agencies/eta/foreign-labor/performance → Disclosure Data标签
→ 下载对应季度/财年的xlsx文件)，筛选出西雅图/华盛顿州、软件相关职位的H1B担保公司，
按申请数量排序输出一份CSV。

用法（单个文件）：
    python h1b_filter.py LCA_Disclosure_Data_FY2026_Q2.xlsx

用法（合并多个文件，比如凑齐最近几个季度/财年）：
    python h1b_filter.py FY2025.xlsx FY2026_Q1.xlsx FY2026_Q2.xlsx
"""
import sys
import pandas as pd

if len(sys.argv) < 2:
    print("用法: python h1b_filter.py <文件1.xlsx> [文件2.xlsx ...]")
    sys.exit(1)

input_paths = sys.argv[1:]

dfs = []
for path in input_paths:
    print(f"读取 {path} ...")
    df_part = pd.read_excel(path)
    print(f"  {len(df_part)} 条记录")
    dfs.append(df_part)

df = pd.concat(dfs, ignore_index=True)
print(f"\n合并 {len(input_paths)} 个文件后，共 {len(df)} 条记录。")
print("列名如下（如果下面的自动筛选报错找不到列，对照这里手动改脚本里的候选列名列表）：")
print(list(df.columns))
print()


def find_col(candidates):
    """列名在不同年份的披露文件里大小写/格式可能不完全一致，做个宽松匹配。"""
    lower_map = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


state_col = find_col(["WORKSITE_STATE", "WORKSITE_STATE_1", "EMPLOYER_STATE"])
title_col = find_col(["JOB_TITLE"])
soc_col = find_col(["SOC_CODE"])
employer_col = find_col(["EMPLOYER_NAME"])
status_col = find_col(["CASE_STATUS"])

missing = [
    name for name, col in
    [("州", state_col), ("职位名", title_col), ("公司名", employer_col)]
    if col is None
]
if missing:
    print(f"[error] 找不到这些列: {missing}")
    print("请对照上面打印出来的实际列名，去改这个脚本里 find_col(...) 传入的候选名单")
    sys.exit(1)

filtered = df[df[state_col].astype(str).str.upper().str.strip() == "WA"]
print(f"筛完华盛顿州: {len(filtered)} 条")

if status_col:
    filtered = filtered[filtered[status_col].astype(str).str.upper().str.contains("CERTIFI", na=False)]
    print(f"筛完只保留Certified状态: {len(filtered)} 条")

software_keywords = ["software", "developer", "engineer", "programmer", "sde", "sdet"]
title_mask = filtered[title_col].astype(str).str.lower().apply(
    lambda t: any(kw in t for kw in software_keywords)
)
filtered = filtered[title_mask]
print(f"筛完软件相关职位关键词: {len(filtered)} 条")

if soc_col:
    print(f"（提示：{soc_col} 这一列如果有15-1252这种SOC代码，可以再加一层更精确的过滤）")

summary = (
    filtered.groupby(employer_col)
    .agg(
        lca_count=(employer_col, "size"),
        states=(state_col, lambda s: "+".join(sorted(set(s.astype(str).str.upper().str.strip())))),
    )
    .reset_index()
    .sort_values("lca_count", ascending=False)
)

output_path = "h1b_sponsors_seattle_software.csv"
summary.to_csv(output_path, index=False)
print(f"\n筛出 {len(summary)} 家公司，已保存到 {output_path}")
print("\n申请数量最多的前30家:")
print(summary.head(30).to_string(index=False))
