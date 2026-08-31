#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T2T (Telomere-to-Telomere / 端粒到端粒) 已发表文献每日采集脚本
=================================================================
数据源: Europe PMC REST API (https://europepmc.org/RestfulWebService)
  - 免费、无需 API Key
  - 覆盖 PubMed/MEDLINE 与 PMC 正式发表文献
  - 通过 FIRST_PDATE 日期窗口只取最近 N 天新发表 / 新收录的文献
  - 显式排除 source=PPR 的预印本 (preprint)，只保留正式发表论文

产出:
  - 合并去重后写回 literature.json (看板读取的数据源)
  - 新增条目带 added_at=运行当天(Asia/Shanghai)，供邮件日报脚本挑选

手动运行:
  python3 scripts/fetch_literature.py --days 7
退出码:
  0 成功 (无论是否有新增) ; 1 拉取失败且未改动数据
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------- 常量配置 ----------
CN_TZ = timezone(timedelta(hours=8))  # 北京时间
EPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
# Europe PMC 查询语句：T2T / telomere-to-telomere 且与基因组组装相关
# 如需收窄/放宽，修改这里即可
EPMC_QUERY = ('(T2T OR "telomere-to-telomere") '
              'AND (genome OR genomic OR assembly OR chromosome OR sequencing)')
# 只保留这些来源 => 正式发表；PPR=预印本，明确排除
FORMAL_SOURCES = {"MED", "PMC"}
PAGE_SIZE = 80
MAX_PAGES = 3
RETRY = 3
TIMEOUT = 30


def today_cn():
    return datetime.now(CN_TZ).date()


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def http_get_json(url):
    last_err = None
    for attempt in range(1, RETRY + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "t2t-literature-board/1.0 (academic digest; contact=repo-owner)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 2 ** attempt
            print(f"[warn] 第 {attempt}/{RETRY} 次请求失败: {e}; {wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Europe PMC 请求连续失败: {last_err}")


def fetch_epmc(days):
    """拉取最近 days 天内首次发表/收录的 T2T 相关正式文献。"""
    end = today_cn()
    start = end - timedelta(days=days)
    query = f"{EPMC_QUERY} AND FIRST_PDATE:[{start.isoformat()} TO {end.isoformat()}]"
    results, cursor = [], "*"
    for _ in range(MAX_PAGES):
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",          # core 才带 abstractText
            "pageSize": str(PAGE_SIZE),
            "sort": "FIRST_PDATE_D desc",
            "cursorMark": cursor,
        }
        url = EPMC_API + "?" + urllib.parse.urlencode(params)
        data = http_get_json(url)
        batch = data.get("resultList", {}).get("result", [])
        results.extend(batch)
        next_cursor = data.get("nextCursorMark", "")
        if not next_cursor or next_cursor == cursor or len(batch) < PAGE_SIZE:
            break
        cursor = next_cursor
    return results


def normalize_item(rec, added):
    """把 Europe PMC 记录转成看板统一 schema。"""
    source_db = rec.get("source", "")
    pmid = rec.get("pmid", "")
    doi = rec.get("doi", "")
    if pmid:
        url = f"https://europepmc.org/article/MED/{pmid}"
        rid = f"pmid-{pmid}"
    elif doi:
        url = f"https://doi.org/{doi}"
        rid = f"doi-{doi.lower()}"
    else:
        url = ""
        rid = "title-" + re.sub(r"\W+", "", rec.get("title", "").lower())[:60]

    journal = ""
    jinfo = rec.get("journalInfo", {}) or {}
    if isinstance(jinfo.get("journal"), dict):
        journal = jinfo["journal"].get("title", "")
    if not journal:
        journal = rec.get("journalTitle", "")
    # 清洗 NLM 双语刊名后缀，如 "Journal of genetics and genomics = Yi chuan xue bao"
    journal = re.sub(r"\s*=\s*[^=]+$", "", journal).strip()

    return {
        "id": rid,
        "title": (rec.get("title", "") or "").strip().rstrip("."),
        "type": "paper",
        "source": journal or source_db,
        "authors": rec.get("authorString", ""),
        "publish_date": rec.get("firstPublicationDate", ""),
        "added_at": added.isoformat(),
        "url": url,
        "doi": doi,
        "pmid": pmid,
        "abstract": strip_html(rec.get("abstractText", "")),
        "keywords": ["T2T", "telomere-to-telomere"],
    }


def dedup_key(item):
    if item.get("pmid"):
        return "pmid:" + item["pmid"]
    if item.get("doi"):
        return "doi:" + item["doi"].lower()
    return "title:" + re.sub(r"\W+", "", item.get("title", "").lower())


def load_existing(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"{path} 顶层结构必须是数组 list")
        return data


def main():
    ap = argparse.ArgumentParser(description="采集最近 N 天 T2T 正式发表文献并合并入 literature.json")
    ap.add_argument("--days", type=int, default=3, help="回溯天数 (默认 3，配合每日任务留冗余)")
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "literature.json"))
    args = ap.parse_args()
    data_path = os.path.abspath(args.data)

    existing = load_existing(data_path)
    seen = {dedup_key(x) for x in existing}
    print(f"[info] 已有记录 {len(existing)} 条; 开始检索最近 {args.days} 天文献...")

    raw = fetch_epmc(args.days)
    print(f"[info] Europe PMC 返回 {len(raw)} 条 (含预印本)")

    added = today_cn()
    new_items = []
    for rec in raw:
        if rec.get("source") not in FORMAL_SOURCES:  # 排除 PPR 预印本等
            continue
        item = normalize_item(rec, added)
        if not item["title"]:
            continue
        key = dedup_key(item)
        if key in seen:
            continue
        seen.add(key)
        new_items.append(item)

    if new_items:
        existing.extend(new_items)
        # 先按发表日期降序，无日期排最后
        existing.sort(key=lambda x: x.get("publish_date", ""), reverse=True)
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        print(f"[ok] 新增正式文献 {len(new_items)} 条，当前总计 {len(existing)} 条 -> {data_path}")
    else:
        print("[ok] 未发现新增文献，literature.json 保持不变")

    # 给 Actions 日志一个机器可读摘要
    print(f"NEW_COUNT={len(new_items)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
