#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T2T (Telomere-to-Telomere / 端粒到端粒) 已发表文献每日多源采集脚本
=====================================================================
数据源（全部免费；前三个无需 API Key）:
  1. Europe PMC   https://www.ebi.ac.uk/europepmc/  覆盖 PubMed/MEDLINE、带摘要，主源
  2. PubMed       NCBI E-utilities (esearch+efetch)，官方源、收录最快，冗余校验
  3. OpenAlex     https://openalex.org/ 开放学术全库，能抓到 MEDLINE 之外出版商条目
  4. (预留, 尚未实现) Elsevier Scopus：需申请 ELSEVIER_API_KEY 且完整权限依赖机构订阅；
     取得 key 后可按前三源同样模式新增 fetch_elsevier() 并加入 sources 列表

设计原则:
  - 只保留正式发表期刊论文，显式排除预印本 (preprint)
  - 跨源按 DOI / PMID / 标题指纹去重，同一篇被多源命中时互补空缺字段
  - 任一单源故障只告警不中断；全部源失败才以非零退出
  - 新增条目 added_at=运行当天(Asia/Shanghai)，供邮件日报挑选

手动运行:
  python3 scripts/fetch_literature.py --days 7
退出码:
  0 成功(无论是否有新增) ; 1 所有数据源均失败
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ---------- 常量配置 ----------
CN_TZ = timezone(timedelta(hours=8))  # 北京时间
EPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
OPENALEX_API = "https://api.openalex.org/works"
# NCBI/OpenAlex 礼貌池建议留联系方式（可被环境变量覆盖，不涉及隐私）
CONTACT_MAIL = os.environ.get("CONTACT_MAIL", "t2t-board@example.com")
TOOL_NAME = "t2t-literature-board"

# 与基因组组装相关的统一检索词
EPMC_QUERY = ('(T2T OR "telomere-to-telomere") '
              'AND (genome OR genomic OR assembly OR chromosome OR sequencing)')
PUBMED_TERM = ('(telomere-to-telomere[tiab] OR T2T[tiab]) '
               'AND (genome[tiab] OR genomic[tiab] OR assembly[tiab] '
               'OR chromosome[tiab] OR sequencing[tiab])')
OPENALEX_SEARCH = "telomere-to-telomere T2T genome assembly chromosome"

FORMAL_SOURCES = {"MED", "PMC"}                  # Europe PMC 正式来源（排除 PPR 预印本）
RELEVANCE_RE = re.compile(r"telomere[\s\-]*to[\s\-]*telomere|t2t|gap[\s\-]?free|gapless", re.I)
FULLNAME_RE = re.compile(r"telomere[\s\-]*to[\s\-]*telomere", re.I)
CONTEXT_RE = re.compile(r"assembl|genome|chromosom", re.I)


def is_relevant(title, abstract):
    """相关性把关：标题直接命中 T2T 词即收；否则要求摘要以全称讨论 T2T 基因组/组装，
    排除仅把 T2T-CHM13 当作比对参考的无关文章。"""
    t, a = title or "", abstract or ""
    if RELEVANCE_RE.search(t):
        return True
    return bool(FULLNAME_RE.search(a)) and bool(CONTEXT_RE.search(a))


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
    return re.sub(r"\s+", " ", text).strip()


def norm_doi(doi):
    if not doi:
        return ""
    doi = doi.strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)


def title_fingerprint(title):
    return "title:" + re.sub(r"\W+", "", (title or "").lower())


def http_get(url, accept="application/json", raw=False):
    last_err = None
    for attempt in range(1, RETRY + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": f"{TOOL_NAME}/2.0 (academic digest; mailto:{CONTACT_MAIL})",
                "Accept": accept,
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                content = resp.read().decode("utf-8")
                return content if raw else json.loads(content)
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 2 ** attempt
            print(f"[warn] 请求失败({attempt}/{RETRY}) {url[:90]}... : {e}; {wait}s 重试", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"连续 {RETRY} 次请求失败: {last_err}")


def make_item(*, title, source, authors, publish_date, doi, pmid, abstract, via, added):
    """统一 schema。"""
    doi = norm_doi(doi)
    pmid = str(pmid or "").strip()
    if pmid:
        url = f"https://europepmc.org/article/MED/{pmid}"
        rid = f"pmid-{pmid}"
    elif doi:
        url = f"https://doi.org/{doi}"
        rid = f"doi-{doi}"
    else:
        url = ""
        rid = "title-" + re.sub(r"\W+", "", title.lower())[:60]
    return {
        "id": rid,
        "title": title.strip().rstrip("."),
        "type": "paper",
        "source": source,
        "authors": authors or "",
        "publish_date": publish_date or "",
        "added_at": added.isoformat(),
        "url": url,
        "doi": doi,
        "pmid": pmid,
        "abstract": strip_html(abstract or ""),
        "keywords": ["T2T", "telomere-to-telomere"],
        "via": via,
    }


# ---------------- 数据源 1: Europe PMC ----------------
def fetch_epmc(days, added):
    end = today_cn()
    start = end - timedelta(days=days)
    query = f"{EPMC_QUERY} AND FIRST_PDATE:[{start.isoformat()} TO {end.isoformat()}]"
    out, cursor = [], "*"
    for _ in range(MAX_PAGES):
        params = {"query": query, "format": "json", "resultType": "core",
                  "pageSize": str(PAGE_SIZE), "sort": "FIRST_PDATE_D desc", "cursorMark": cursor}
        data = http_get(EPMC_API + "?" + urllib.parse.urlencode(params))
        batch = data.get("resultList", {}).get("result", [])
        for rec in batch:
            if rec.get("source") not in FORMAL_SOURCES:      # 排除 PPR 预印本
                continue
            if not is_relevant(rec.get("title", ""), rec.get("abstractText", "")):
                continue
            jinfo = rec.get("journalInfo", {}) or {}
            journal = jinfo.get("journal", {}).get("title", "") if isinstance(jinfo.get("journal"), dict) else ""
            journal = journal or rec.get("journalTitle", "")
            journal = re.sub(r"\s*=\s*[^=]+$", "", journal).strip()   # 去 NLM 双语刊名后缀
            out.append(make_item(
                title=rec.get("title", "") or "", source=journal or rec.get("source", ""),
                authors=rec.get("authorString", ""),
                publish_date=rec.get("firstPublicationDate", ""),
                doi=rec.get("doi", ""), pmid=rec.get("pmid", ""),
                abstract=rec.get("abstractText", ""), via="EuropePMC", added=added))
        nxt = data.get("nextCursorMark", "")
        if not nxt or nxt == cursor or len(batch) < PAGE_SIZE:
            break
        cursor = nxt
    return out


# ---------------- 数据源 2: PubMed E-utilities ----------------
MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _pubmed_date(art):
    ad = art.find(".//Article/ArticleDate")
    if ad is not None:
        y, m, d = ad.findtext("Year"), ad.findtext("Month"), ad.findtext("Day")
        if y:
            return f"{y}-{int(m):02d}-{int(d):02d}" if m and d else f"{y}-{int(m or 1):02d}-01"
    pd_ = art.find(".//Journal/JournalIssue/PubDate")
    if pd_ is not None:
        y = pd_.findtext("Year")
        mo = pd_.findtext("Month")
        d = pd_.findtext("Day")
        if y and mo:
            return f"{y}-{MONTHS.get(mo[:3], '01')}-{int(d) if d else 1:02d}"
        med = pd_.findtext("MedlineDate") or ""
        m = re.match(r"(\d{4})[\s-]*([A-Za-z]{3})?", med)
        if m:
            return f"{m.group(1)}-{MONTHS.get((m.group(2) or 'Jan')[:3], '01')}-01"
        if y:
            return f"{y}-01-01"
    return ""


def fetch_pubmed(days, added):
    params = {"db": "pubmed", "term": PUBMED_TERM, "reldate": str(days),
              "datetype": "pdat", "retmax": str(PAGE_SIZE), "retmode": "json",
              "tool": TOOL_NAME, "email": CONTACT_MAIL}
    data = http_get(PUBMED_SEARCH + "?" + urllib.parse.urlencode(params))
    ids = data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    time.sleep(0.4)  # 无 key 限速 ≤3 次/秒
    fparams = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml",
               "tool": TOOL_NAME, "email": CONTACT_MAIL}
    xml_text = http_get(PUBMED_FETCH + "?" + urllib.parse.urlencode(fparams),
                        accept="application/xml", raw=True)
    root = ET.fromstring(xml_text)
    out = []
    for art in root.findall(".//PubmedArticle"):
        ptypes = [(pt.text or "").lower() for pt in art.findall(".//PublicationType")]
        if "preprint" in ptypes:                              # 排除预印本
            continue
        title_el = art.find(".//Article/ArticleTitle")
        title = "".join(title_el.itertext()) if title_el is not None else ""
        authors = []
        for a in art.findall(".//AuthorList/Author"):
            ln, ini = a.findtext("LastName"), a.findtext("Initials")
            if ln:
                authors.append(f"{ln} {ini}" if ini else ln)
        doi = ""
        for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = aid.text or ""
        abstract_parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            txt = "".join(ab.itertext())
            abstract_parts.append(f"{label}: {txt}" if label else txt)
        if not is_relevant(title, " ".join(abstract_parts)):
            continue
        out.append(make_item(
            title=title, source=art.findtext(".//Article/Journal/Title") or "",
            authors=", ".join(authors) + ("." if authors else ""),
            publish_date=_pubmed_date(art), doi=doi,
            pmid=art.findtext(".//MedlineCitation/PMID"),
            abstract=" ".join(abstract_parts), via="PubMed", added=added))
    return out


# ---------------- 数据源 3: OpenAlex ----------------
def _rebuild_abstract(inv):
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def fetch_openalex(days, added):
    end = today_cn()
    start = end - timedelta(days=days)
    params = {"search": OPENALEX_SEARCH,
              "filter": f"from_publication_date:{start.isoformat()},"
                        f"to_publication_date:{end.isoformat()},type:article",
              "per-page": "50", "mailto": CONTACT_MAIL}
    data = http_get(OPENALEX_API + "?" + urllib.parse.urlencode(params))
    out = []
    for w in data.get("results", []):
        title = w.get("display_name", "") or ""
        abstract = _rebuild_abstract(w.get("abstract_inverted_index"))
        # OpenAlex 为宽召回，本地统一做相关性把关
        if not is_relevant(title, abstract):
            continue
        loc = w.get("primary_location") or {}
        src = (loc.get("source") or {}) if isinstance(loc, dict) else {}
        if src.get("type") == "repository":                  # 排除预印本仓库条目
            continue
        authors = []
        for a in w.get("authorships", [])[:15]:
            name = (a.get("author") or {}).get("display_name")
            if name:
                authors.append(name)
        out.append(make_item(
            title=title, source=src.get("display_name", "") or "OpenAlex",
            authors=", ".join(authors),
            publish_date=w.get("publication_date", ""),
            doi=w.get("doi", ""), pmid="",
            abstract=abstract, via="OpenAlex", added=added))
    return out


# ---------------- 跨源合并去重 ----------------
def merge_candidates(existing, streams):
    """streams: [(源名, [items])]；返回 (全部新条目列表, 每源统计)。"""
    doi_idx, pmid_idx, title_idx = {}, {}, {}
    # 给已有条目建索引
    for i, x in enumerate(existing):
        if x.get("doi"):
            doi_idx[x["doi"]] = ("old", i)
        if x.get("pmid"):
            pmid_idx[x["pmid"]] = ("old", i)
        tf = title_fingerprint(x.get("title", ""))
        title_idx.setdefault(tf, ("old", i))

    new_items, stats = [], {}
    for src_name, items in streams:
        cnt = 0
        for it in items:
            if not it["title"]:
                continue
            hit = None
            if it["doi"] and it["doi"] in doi_idx:
                hit = doi_idx[it["doi"]]
            elif it["pmid"] and it["pmid"] in pmid_idx:
                hit = pmid_idx[it["pmid"]]
            else:
                hit = title_idx.get(title_fingerprint(it["title"]))
            if hit is None:
                # 全新条目
                new_items.append(it)
                idx = len(new_items) - 1
                if it["doi"]:
                    doi_idx[it["doi"]] = ("new", idx)
                if it["pmid"]:
                    pmid_idx[it["pmid"]] = ("new", idx)
                title_idx[title_fingerprint(it["title"])] = ("new", idx)
                cnt += 1
            else:
                # 同篇已存在：互补空缺字段、追加 via
                bucket, j = hit
                target = existing[j] if bucket == "old" else new_items[j]
                for field in ("abstract", "source", "authors", "url", "doi", "pmid", "publish_date"):
                    if not target.get(field) and it.get(field):
                        target[field] = it[field]
                vias = {v.strip() for v in (target.get("via", "") or "").split(";") if v.strip()}
                vias.add(it["via"])
                target["via"] = ";".join(sorted(vias))
                # 索引可能因补字段而新增
                if target.get("doi"):
                    doi_idx.setdefault(target["doi"], hit)
                if target.get("pmid"):
                    pmid_idx.setdefault(target["pmid"], hit)
        stats[src_name] = cnt
    return new_items, stats


def load_existing(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"{path} 顶层结构必须是数组 list")
        return data


def main():
    ap = argparse.ArgumentParser(description="多源采集最近 N 天 T2T 正式发表文献并合并入 literature.json")
    ap.add_argument("--days", type=int, default=3, help="回溯天数 (默认 3，配合每日任务留冗余)")
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "literature.json"))
    args = ap.parse_args()
    data_path = os.path.abspath(args.data)

    existing = load_existing(data_path)
    added = today_cn()
    print(f"[info] 已有记录 {len(existing)} 条；多源检索最近 {args.days} 天文献...")

    sources = [
        ("EuropePMC", lambda: fetch_epmc(args.days, added)),
        ("PubMed",    lambda: fetch_pubmed(args.days, added)),
        ("OpenAlex",  lambda: fetch_openalex(args.days, added)),
    ]
    streams, failed = [], []
    for name, fn in sources:
        try:
            items = fn()
            print(f"[info] 源 {name}: 命中 {len(items)} 条候选")
            streams.append((name, items))
        except Exception as e:  # noqa: BLE001  单源失败不拖垮全局
            failed.append(name)
            print(f"[warn] 源 {name} 拉取失败，已跳过: {e}", file=sys.stderr)
    if not streams:
        print("[error] 所有数据源均失败，literature.json 保持不变", file=sys.stderr)
        sys.exit(1)

    new_items, stats = merge_candidates(existing, streams)
    if new_items:
        existing.extend(new_items)
        existing.sort(key=lambda x: x.get("publish_date", ""), reverse=True)
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        print(f"[ok] 各源新增（去重后）: {stats}")
        print(f"[ok] 本次共新增 {len(new_items)} 条，当前总计 {len(existing)} 条 -> {data_path}")
    else:
        print(f"[ok] 各源新增（去重后）: {stats}")
        print("[ok] 未发现新增文献，literature.json 保持不变")
    if failed:
        print(f"[warn] 本次失败源: {', '.join(failed)}（明日自动重试）", file=sys.stderr)
    print(f"NEW_COUNT={len(new_items)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
