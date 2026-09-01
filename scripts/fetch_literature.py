#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多专题科研文献每日多源采集脚本
================================
为 5 个研究板块分别从多个公开学术源检索“已正式发表”的最新文献，跨源去重、
字段互补后合并入 literature.json，供分板块邮件日报与网页看板使用。
研究板块 (BOARDS):
  t2t          T2T（端粒到端粒）基因组
  cmed         稻纵卷叶螟功能基因组学 (Cnaphalocrocis medinalis)
  sexdet       昆虫性别决定演化机制
  ppi          蛋白互作预测
  insecticide  新型杀虫剂
数据源:
  1. Europe PMC   (免费, 无 key, 覆盖 PubMed/MEDLINE, 摘要全)        主源
  2. PubMed       NCBI E-utilities (免费, 无 key, 收录最快)
  3. OpenAlex     (免费, 无 key, 全学科开放库)
  4. Elsevier Scopus (需环境变量 ELSEVIER_API_KEY; 无 key 自动跳过;
                      订阅级摘要/全文依赖机构 IP 或 ELSEVIER_INSTTOKEN)
人工补录:
  manual_inbox/*.json 为人工/定时任务检索的中文公众号与科研资讯分片,
  运行时按 doi/pmid/标题/URL 去重后并入主库(boards 由分片自带)。
每条记录在原 schema 上扩展:
  boards        所属板块 key 列表(一篇可同时属于多个板块)
  species_latin 研究物种拉丁学名(自动识别)
  species_en    研究物种英文名(内置词典映射)
  species_zh    研究物种中文名(内置词典映射)
  takeaway      一句话总结(规则抽取; 若配置 LLM 则为中文精炼总结)
  conclusion    主要结论(规则抽取; 若配置 LLM 则为中文)
可选 LLM 中文增强(默认关闭, 配置后自动启用, 任何失败都回退规则版、不阻断):
  OPENAI_API_KEY  兼容 OpenAI Chat Completions 协议的 key(DeepSeek/豆包/OpenAI 等)
  OPENAI_BASE_URL 接口地址(可选)
  LLM_MODEL       模型名(可选)
手动运行:
  python3 scripts/fetch_literature.py --days 3
退出码: 0 成功(无论是否新增) ; 1 所有数据源对所有板块均失败
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ---------------- 常量 ----------------
CN_TZ = timezone(timedelta(hours=8))
EPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
OPENALEX_API = "https://api.openalex.org/works"
SCOPUS_API = "https://api.elsevier.com/content/search/scopus"
CONTACT_MAIL = os.environ.get("CONTACT_MAIL", "t2t-board@example.com")
TOOL_NAME = "t2t-literature-board"
FORMAL_SOURCES = {"MED", "PMC"}           # Europe PMC 正式来源(排除 PPR 预印本)
PAGE_SIZE = 50
MAX_PAGES = 2
RETRY = 3
TIMEOUT = 30
PER_BOARD_TARGET = 5                      # 每板块期望的最少新增条数
MAX_EXPAND_DAYS = 14                      # 为凑够目标条数, 窗口最多自适应扩展到的天数

# ==================== 板块配置 ====================
def _re(pat):
    return re.compile(pat, re.I)


# T2T 严格相关性: 标题命中 T2T 词即收; 否则要求摘要以“全称”讨论基因组/组装,
# 以排除仅把 T2T-CHM13 当比对参考的无关文章
T2T_TITLE_RE = _re(r"telomere[\s\-]*to[\s\-]*telomere|t2t|gap[\s\-]?free|gapless")
T2T_FULLNAME_RE = _re(r"telomere[\s\-]*to[\s\-]*telomere")
T2T_CONTEXT_RE = _re(r"assembl|genome|chromosom")

# 每个板块给出四个源各自的检索式, 以及本地相关性把关正则 require(需全部命中)
BOARDS = [
    {
        "key": "t2t",
        "name": "T2T 端粒到端粒基因组",
        "epmc": ('(T2T OR "telomere-to-telomere" OR gap-free OR gapless) '
                 'AND (genome OR genomic OR assembly OR chromosome OR sequencing)'),
        "pubmed": ('(telomere-to-telomere[tiab] OR T2T[tiab] OR gap-free[tiab] OR gapless[tiab]) '
                   'AND (genome[tiab] OR genomic[tiab] OR assembly[tiab] '
                   'OR chromosome[tiab] OR sequencing[tiab])'),
        "openalex": "telomere-to-telomere T2T gap-free genome assembly chromosome",
        "scopus": ('TITLE-ABS-KEY("telomere-to-telomere" OR T2T OR "gap-free" OR gapless) '
                   'AND TITLE-ABS-KEY(genome OR genomic OR assembly OR chromosome OR sequencing)'),
        "require": [_re(r"telomere[\s\-]*to[\s\-]*telomere|t2t|gap[\s\-]?free|gapless")],
        "strict_t2t": True,
    },
    {
        "key": "cmed",
        "name": "稻纵卷叶螟功能基因组学",
        "epmc": ('("Cnaphalocrocis medinalis" OR "rice leaffolder" OR "rice leaf folder" '
                 'OR "rice leaf roller" OR "rice leafroller") '
                 'AND (genome OR genomic OR transcriptome OR gene OR genes OR protein OR '
                 'functional OR CRISPR OR RNAi OR detoxification OR olfactory OR chitin OR '
                 'development OR resistance OR enzyme OR receptor)'),
        "pubmed": ('("Cnaphalocrocis medinalis"[tiab] OR "rice leaffolder"[tiab] OR '
                   '"rice leaf folder"[tiab] OR "rice leaf roller"[tiab]) '
                   'AND (genome[tiab] OR genomic[tiab] OR transcriptome[tiab] OR gene[tiab] '
                   'OR protein[tiab] OR functional[tiab] OR CRISPR[tiab] OR RNAi[tiab] OR '
                   'detoxification[tiab] OR olfactory[tiab] OR chitin[tiab] OR receptor[tiab])'),
        "openalex": "Cnaphalocrocis medinalis rice leaffolder genome gene functional",
        "scopus": ('TITLE-ABS-KEY("Cnaphalocrocis medinalis" OR "rice leaffolder" OR '
                   '"rice leaf folder" OR "rice leaf roller") '
                   'AND TITLE-ABS-KEY(genome OR genomic OR transcriptome OR gene OR protein OR '
                   'functional OR CRISPR OR RNAi OR detoxification OR olfactory OR receptor)'),
        "require": [_re(r"cnaphalocrocis medinalis|rice\s*leaf[\s\-]?(folder|roller)")],
        "max_days": 45,                  # 该物种发文较少, 允许回溯更久以凑够条数
    },
    {
        "key": "sexdet",
        "name": "昆虫性别决定演化机制",
        "epmc": ('(insect OR lepidoptera OR diptera OR hymenoptera OR coleoptera OR mosquito OR '
                 'fly OR flies OR moth OR silkworm OR aphid OR wasp OR beetle OR butterfly OR '
                 'planthopper OR locust) '
                 'AND ("sex determination" OR "sex-determining" OR sex-determination OR doublesex '
                 'OR transformer OR "sex-lethal" OR feminizer OR "complementary sex determiner" '
                 'OR Wolbachia OR "sex chromosome" OR haplodiploid OR "sex ratio" OR masculinize '
                 'OR feminize) '
                 'AND (evolution OR evolutionary OR mechanism OR pathway OR cascade OR gene OR '
                 'splicing OR regulation OR conserved OR divergent)'),
        "pubmed": ('(insect[tiab] OR lepidoptera[tiab] OR diptera[tiab] OR hymenoptera[tiab] OR '
                   'mosquito[tiab] OR moth[tiab] OR silkworm[tiab] OR aphid[tiab] OR beetle[tiab] '
                   'OR planthopper[tiab]) '
                   'AND ("sex determination"[tiab] OR doublesex[tiab] OR transformer[tiab] OR '
                   '"sex-lethal"[tiab] OR feminizer[tiab] OR Wolbachia[tiab] OR '
                   '"sex chromosome"[tiab] OR haplodiploid[tiab] OR "sex ratio"[tiab]) '
                   'AND (evolution[tiab] OR evolutionary[tiab] OR mechanism[tiab] OR pathway[tiab] '
                   'OR gene[tiab] OR splicing[tiab] OR regulation[tiab])'),
        "openalex": ("insect sex determination evolution doublesex transformer Wolbachia "
                     "sex chromosome mechanism"),
        "scopus": ('TITLE-ABS-KEY(insect OR lepidoptera OR diptera OR hymenoptera OR mosquito OR '
                   'moth OR silkworm OR aphid OR beetle) '
                   'AND TITLE-ABS-KEY("sex determination" OR doublesex OR transformer OR '
                   '"sex-lethal" OR feminizer OR Wolbachia OR "sex chromosome" OR haplodiploid) '
                   'AND TITLE-ABS-KEY(evolution OR mechanism OR pathway OR gene OR splicing OR '
                   'regulation OR conserved)'),
        "require": [_re(r"sex[\s\-]?determin|doublesex|\bdsx\b|transformer|\btra\b|sex[\s\-]?lethal|"
                        r"femini[sz]er|wolbachia|haplodiploid|sex chromosome|sex ratio"),
                    _re(r"insect|lepidoptera|diptera|hymenoptera|coleoptera|mosquito|moth|silkworm|"
                        r"aphid|beetle|fly|flies|wasp|butterfly|planthopper|locust|drosophila|"
                        r"bombyx|cnaphalocrocis")],
    },
    {
        "key": "ppi",
        "name": "蛋白互作预测",
        "epmc": ('("protein-protein interaction" OR "protein–protein interaction" OR '
                 '"protein interaction network" OR PPI OR interactome OR "protein complex") '
                 'AND (predict* OR forecast OR "deep learning" OR "machine learning" OR network '
                 'OR AlphaFold OR docking OR interolog OR co-expression OR "contact prediction" '
                 'OR computational OR model OR algorithm)'),
        "pubmed": ('("protein-protein interaction"[tiab] OR PPI[tiab] OR interactome[tiab] OR '
                   '"protein complex"[tiab]) '
                   'AND (predict*[tiab] OR "deep learning"[tiab] OR "machine learning"[tiab] OR '
                   'network[tiab] OR AlphaFold[tiab] OR docking[tiab] OR interolog[tiab] OR '
                   'co-expression[tiab] OR computational[tiab] OR algorithm[tiab])'),
        "openalex": ("protein-protein interaction prediction deep learning network AlphaFold "
                     "interactome computational"),
        "scopus": ('TITLE-ABS-KEY("protein-protein interaction" OR PPI OR interactome OR '
                   '"protein complex") '
                   'AND TITLE-ABS-KEY(predict OR prediction OR "deep learning" OR '
                   '"machine learning" OR network OR AlphaFold OR docking OR interolog OR '
                   'co-expression OR computational OR algorithm)'),
        "require": [_re(r"protein[\s–\-]*protein interaction|\bppi\b|interactome|protein complex|"
                        r"protein interaction"),
                    _re(r"predict|deep learning|machine learning|network|alphafold|docking|"
                        r"interolog|co.expression|computational|algorithm|model|evolution|"
                        r"conservation|structure")],
        # 标题需落在“互作/复合物/网络/结构”方法学范畴
        "require_title": [_re(
            r"protein.protein|\bppi\b|interactome|protein complex|interaction|docking|"
            r"contact|complex structure|binding")],
        # 排除人类疾病网络药理学/中药分子对接/临床生物标志物范式
        "exclude": [_re(
            r"network pharmacology|network toxicology|traditional chinese medicine|\btcm\b|"
            r"herbal?|serum|urine|diagnostic (marker|value|signature)|therapeutic intervention|"
            r"anti-inflammatory|osteoarthritis|parkinson|alzheimer|\bcancer\b|tumo[ur]|carcinoma|"
            r"fibrosis|clinical trial|\bpatients?\b|drug.target|mesangial|endothelial dysfunction|"
            r"hepatotoxic|nephrotoxic|cardiotoxic")],
    },
    {
        "key": "insecticide",
        "name": "新型杀虫剂",
        "epmc": ('(insecticide OR insecticidal OR pesticide OR pesticidal OR biopesticide OR '
                 '"pest control" OR acaricide) '
                 'AND (novel OR new OR discovery OR target OR "mode of action" OR resistance OR '
                 'mechanism OR efficacy OR toxicity OR compound OR molecule OR dsRNA OR '
                 '"RNAi-based" OR diamide OR neonicotinoid OR "Bacillus thuringiensis" OR '
                 'botanical OR synergist OR formulation)'),
        "pubmed": ('(insecticide[tiab] OR insecticidal[tiab] OR pesticide[tiab] OR '
                   'biopesticide[tiab] OR acaricide[tiab]) '
                   'AND (novel[tiab] OR new[tiab] OR discovery[tiab] OR target[tiab] OR '
                   '"mode of action"[tiab] OR resistance[tiab] OR mechanism[tiab] OR '
                   'efficacy[tiab] OR toxicity[tiab] OR dsRNA[tiab] OR diamide[tiab] OR '
                   'neonicotinoid[tiab] OR "Bacillus thuringiensis"[tiab] OR botanical[tiab])'),
        "openalex": ("novel insecticide pesticide mode of action target resistance discovery "
                     "biopesticide dsRNA"),
        "scopus": ('TITLE-ABS-KEY(insecticide OR insecticidal OR pesticide OR biopesticide OR '
                   'acaricide) '
                   'AND TITLE-ABS-KEY(novel OR new OR discovery OR target OR "mode of action" OR '
                   'resistance OR mechanism OR efficacy OR toxicity OR dsRNA OR diamide OR '
                   'neonicotinoid OR "Bacillus thuringiensis" OR botanical OR compound)'),
        "require": [_re(r"insecticid|pesticid|biopesticide|acaricid|pest control")],
        # 标题需体现“新型/机制/毒理/抗性/化合物”等研发属性, 排除水体残留监测等环境类文章
        "require_title": [_re(
            r"novel|new |discovery|target|mode of action|mechanism|efficacy|toxic|bioactiv|"
            r"compound|molecule|synthesi|synerg|formulation|resistan|larvicid|adulticid|"
            r"bioassay|mortality|dsrna|diamide|neonicotinoid|botanical|essential oil|"
            r"metabolite|receptor|enzyme|inhibitor|agonist|antagonist|knockdown|insecticid|"
            r"acaricid|biopesticide")],
    },
]
BOARD_BY_KEY = {b["key"]: b for b in BOARDS}

# ==================== 物种词典(拉丁 -> [中文名, 英文名]) ====================
SPECIES_DICT = {
    "cnaphalocrocis medinalis": ("稻纵卷叶螟", "Rice leaffolder"),
    "bombyx mori": ("家蚕", "Domestic silkworm"),
    "bombyx mandarina": ("野桑蚕", "Wild silkworm"),
    "drosophila melanogaster": ("黑腹果蝇", "Fruit fly"),
    "tribolium castaneum": ("赤拟谷盗", "Red flour beetle"),
    "spodoptera frugiperda": ("草地贪夜蛾", "Fall armyworm"),
    "spodoptera litura": ("斜纹夜蛾", "Tobacco cutworm"),
    "helicoverpa armigera": ("棉铃虫", "Cotton bollworm"),
    "nilaparvata lugens": ("褐飞虱", "Brown planthopper"),
    "sogatella furcifera": ("白背飞虱", "White-backed planthopper"),
    "laodelphax striatellus": ("灰飞虱", "Small brown planthopper"),
    "apis mellifera": ("西方蜜蜂", "Western honey bee"),
    "anopheles gambiae": ("冈比亚按蚊", "African malaria mosquito"),
    "aedes aegypti": ("埃及伊蚊", "Yellow fever mosquito"),
    "culex pipiens": ("尖音库蚊", "Northern house mosquito"),
    "plutella xylostella": ("小菜蛾", "Diamondback moth"),
    "manduca sexta": ("烟草天蛾", "Tobacco hornworm"),
    "locusta migratoria": ("飞蝗", "Migratory locust"),
    "homo sapiens": ("人", "Human"),
    "mus musculus": ("小家鼠", "House mouse"),
    "oryza sativa": ("水稻", "Asian rice"),
    "arabidopsis thaliana": ("拟南芥", "Thale cress"),
    "zea mays": ("玉米", "Maize"),
}
# 双名法正则(属名首字母大写 + 种加词小写)
BINOMIAL_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\.\s?|\s)([a-z][a-z\-]{2,}))\b")
# 常见学术英语词, 出现在“属名/种加词”位置时判定为误抓
SPECIES_STOP = {
    "mixed","these","those","their","which","where","while","based","using","novel","new","first",
    "high","low","global","drive","drives","driven","major","general","specific","different","various",
    "evidence","occurrence","occurrences","results","result","study","studies","analysis","analyses",
    "treatment","treatments","networks","network","systems","system","transport","transformation",
    "wastewater","across","among","between","within","without","through","into","from","with","such",
    "this","that","here","thus","therefore","however","although","overall","total","using","based",
    "pesticide","pesticides","insecticide","insecticides","insecticidal","pesticidal","chemical",
    "protein","proteins","gene","genes","genome","genomes","protein-protein","interaction","recent",
    "multiple","several","many","most","both","each","every","some","any","associated","related",
    "increased","decreased","reduced","induced","mediated","regulated","controlled","combined",
    "integrated","enhanced","improved","detected","identified","reported","characterized","compared",
    "two","three","four","five","one","non","anti","post","pre","sub","super","inter","intra","cross",
    "role","roles","effect","effects","impact","impacts","response","responses","activity","activities",
    "expression","exposure","application","evaluation","assessment","management","control","controls",
}
# 典型拉丁种加词后缀(命中才对“非词典物种”给予置信)
LATIN_SUFFIX_RE = re.compile(
    r"(us|a|um|is|ii|i|ae|ana|anum|ata|atum|ella|ina|icus|ica|icum|ense|ensis|oides|oides|"
    r"vorus|vorum|phila|philus|ceps|cornis|penis|fer|ger|pennis|cauda|derma|soma|stoma)$", re.I)
TAKE_LEAD_RE = re.compile(
    r"(we show|we report|we found|we identify|we demonstrate|we reveal|we present|we propose|"
    r"here we|this study shows|this study reveals|these results|results show|results revealed|"
    r"our findings|we characterize|we assembled|we generated)", re.I)
CONCL_RE = re.compile(
    r"(conclusion[s]?|taken together|overall,|in summary|collectively,|these findings|"
    r"our results suggest|our data suggest|we conclude|altogether,)", re.I)


# ==================== 工具函数 ====================
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

def http_request(url, accept="application/json", headers=None, raw=False, data=None,
                 timeout=TIMEOUT, retries=RETRY):
    base_headers = {"User-Agent": f"{TOOL_NAME}/3.0 (academic digest; mailto:{CONTACT_MAIL})",
                    "Accept": accept}
    if headers:
        base_headers.update(headers)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=base_headers, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read().decode("utf-8")
                return content if raw else json.loads(content)
        except urllib.error.HTTPError as e:
            # 把 Elsevier/LLM 网关返回的错误体带出来, 便于在 Actions 日志定位(key 权限/字段/配额)
            body = ""
            try:
                body = (e.read() or b"").decode("utf-8", "ignore")[:400]
            except Exception:  # noqa: BLE001
                pass
            # 429 与 5xx 值得重试; 其余 4xx(400/401/403/404/422)重试无意义, 直接抛出
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {e.code} for {url[:90]} ; resp={body}") from e
            last_err = f"HTTP {e.code} {body}".strip()
            wait = 2 ** attempt
            print(f"[warn] 可重试错误({attempt}/{retries}) {url[:90]}... : {last_err}; {wait}s 重试",
                  file=sys.stderr)
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 2 ** attempt
            print(f"[warn] 请求失败({attempt}/{retries}) {url[:90]}... : {e}; {wait}s 重试",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"连续 {retries} 次请求失败: {last_err}")

def split_sentences(text):
    text = strip_html(text)
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if len(p.strip()) > 15]

def extract_takeaway(abstract):
    sents = split_sentences(abstract)
    if not sents:
        return ""
    for s in sents:
        if TAKE_LEAD_RE.search(s):
            return s[:280].strip()
    return sents[0][:280].strip()

def extract_conclusion(abstract):
    sents = split_sentences(abstract)
    if not sents:
        return ""
    for s in reversed(sents):          # 结论通常在末段
        if CONCL_RE.search(s):
            return s[:320].strip()
    for s in sents:
        if CONCL_RE.search(s):
            return s[:320].strip()
    return sents[-1][:320].strip() if len(sents) > 1 else ""

def detect_species(title, abstract):
    """返回 (latin, en, zh)。优先匹配内置词典; 否则仅在高置信(种加词像拉丁词、
    且两个词都不是常见英语词)时回填双名法, 宁空勿错。"""
    blob = f"{title or ''} {abstract or ''}"
    low = blob.lower()
    # 1) 词典最高优先
    for latin, (zh, en) in SPECIES_DICT.items():
        if latin in low:
            words = latin.split()
            cap = words[0].capitalize() + (" " + " ".join(w.lower() for w in words[1:]) if len(words) > 1 else "")
            return cap, en, zh
    # 2) 正则候选, 用 STOP 词表与拉丁后缀双重把关; 仅接受“种加词带典型拉丁后缀”的高置信结果, 宁空勿错
    for m in BINOMIAL_RE.finditer(blob):
        whole = re.sub(r"\s+", " ", m.group(1)).strip()
        words = whole.split()
        genus, epithet = words[0].rstrip("."), words[-1].lower()
        if genus.lower() in SPECIES_STOP or epithet in SPECIES_STOP:
            continue
        if len(genus) < 3 or len(epithet) < 3:
            continue
        if LATIN_SUFFIX_RE.search(epithet):
            return whole, "", ""
    return "", "", ""

def relevant(board, title, abstract):
    t, a = title or "", abstract or ""
    if board.get("strict_t2t"):
        # 标题命中 T2T 词即收; 否则摘要须以全称讨论基因组/组装
        if T2T_TITLE_RE.search(t):
            return True
        return bool(T2T_FULLNAME_RE.search(a) and T2T_CONTEXT_RE.search(a))
    blob = f"{t} {a}"
    if not all(rx.search(blob) for rx in board["require"]):
        return False
    for rx in board.get("require_title", []):       # 若指定, 标题必须命中其一
        if not rx.search(t):
            return False
    for rx in board.get("exclude", []):             # 若指定, 命中任一即排除
        if rx.search(blob):
            return False
    return True

def make_item(*, title, source, authors, publish_date, doi, pmid, abstract, via, added, board_key):
    doi = norm_doi(doi)
    pmid = str(pmid or "").strip()
    abstract = strip_html(abstract or "")
    if pmid:
        url = f"https://europepmc.org/article/MED/{pmid}"
        rid = f"pmid-{pmid}"
    elif doi:
        url = f"https://doi.org/{doi}"
        rid = f"doi-{doi}"
    else:
        url = ""
        rid = "title-" + re.sub(r"\W+", "", (title or "").lower())[:60]
    latin, en, zh = detect_species(title, abstract)
    return {
        "id": rid,
        "title": (title or "").strip().rstrip("."),
        "type": "paper",
        "boards": [board_key],
        "source": source,
        "authors": authors or "",
        "publish_date": publish_date or "",
        "added_at": added.isoformat(),
        "url": url,
        "doi": doi,
        "pmid": pmid,
        "abstract": abstract,
        "species_latin": latin,
        "species_en": en,
        "species_zh": zh,
        "takeaway": extract_takeaway(abstract),
        "conclusion": extract_conclusion(abstract),
        "keywords": [BOARD_BY_KEY[board_key]["name"]],
        "via": via,
    }


# ==================== 数据源 1: Europe PMC ====================
def fetch_epmc(board, days, added):
    end = today_cn()
    start = end - timedelta(days=days)
    query = f"({board['epmc']}) AND FIRST_PDATE:[{start.isoformat()} TO {end.isoformat()}]"
    out, cursor = [], "*"
    for _ in range(MAX_PAGES):
        params = {"query": query, "format": "json", "resultType": "core",
                  "pageSize": str(PAGE_SIZE), "sort": "FIRST_PDATE_D desc", "cursorMark": cursor}
        data = http_request(EPMC_API + "?" + urllib.parse.urlencode(params))
        batch = data.get("resultList", {}).get("result", [])
        for rec in batch:
            if rec.get("source") not in FORMAL_SOURCES:
                continue
            title, ab = rec.get("title", ""), rec.get("abstractText", "")
            if not relevant(board, title, ab):
                continue
            jinfo = rec.get("journalInfo", {}) or {}
            journal = jinfo.get("journal", {}).get("title", "") if isinstance(jinfo.get("journal"), dict) else ""
            journal = journal or rec.get("journalTitle", "")
            journal = re.sub(r"\s*=\s*[^=]+$", "", journal).strip()
            out.append(make_item(
                title=title, source=journal or rec.get("source", ""),
                authors=rec.get("authorString", ""),
                publish_date=rec.get("firstPublicationDate", ""),
                doi=rec.get("doi", ""), pmid=rec.get("pmid", ""),
                abstract=ab, via="EuropePMC", added=added, board_key=board["key"]))
        nxt = data.get("nextCursorMark", "")
        if not nxt or nxt == cursor or len(batch) < PAGE_SIZE:
            break
        cursor = nxt
    return out


# ==================== 数据源 2: PubMed ====================
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
        y, mo, d = pd_.findtext("Year"), pd_.findtext("Month"), pd_.findtext("Day")
        if y and mo:
            return f"{y}-{MONTHS.get(mo[:3], '01')}-{int(d) if d else 1:02d}"
        med = pd_.findtext("MedlineDate") or ""
        m = re.match(r"(\d{4})[\s-]*([A-Za-z]{3})?", med)
        if m:
            return f"{m.group(1)}-{MONTHS.get((m.group(2) or 'Jan')[:3], '01')}-01"
        if y:
            return f"{y}-01-01"
    return ""

def fetch_pubmed(board, days, added):
    params = {"db": "pubmed", "term": board["pubmed"], "reldate": str(days),
              "datetype": "pdat", "retmax": str(PAGE_SIZE), "retmode": "json",
              "tool": TOOL_NAME, "email": CONTACT_MAIL}
    data = http_request(PUBMED_SEARCH + "?" + urllib.parse.urlencode(params))
    ids = data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    time.sleep(0.4)
    fparams = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml",
               "tool": TOOL_NAME, "email": CONTACT_MAIL}
    xml_text = http_request(PUBMED_FETCH + "?" + urllib.parse.urlencode(fparams),
                            accept="application/xml", raw=True)
    root = ET.fromstring(xml_text)
    out = []
    for art in root.findall(".//PubmedArticle"):
        ptypes = [(pt.text or "").lower() for pt in art.findall(".//PublicationType")]
        if "preprint" in ptypes:
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
        ab_parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            ab_parts.append(f"{label}: {''.join(ab.itertext())}" if label else "".join(ab.itertext()))
        abstract = " ".join(ab_parts)
        if not relevant(board, title, abstract):
            continue
        out.append(make_item(
            title=title, source=art.findtext(".//Article/Journal/Title") or "",
            authors=", ".join(authors) + ("." if authors else ""),
            publish_date=_pubmed_date(art), doi=doi,
            pmid=art.findtext(".//MedlineCitation/PMID"),
            abstract=abstract, via="PubMed", added=added, board_key=board["key"]))
    return out


# ==================== 数据源 3: OpenAlex ====================
def _rebuild_abstract(inv):
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))

def fetch_openalex(board, days, added):
    end = today_cn()
    start = end - timedelta(days=days)
    params = {"search": board["openalex"],
              "filter": f"from_publication_date:{start.isoformat()},"
                        f"to_publication_date:{end.isoformat()},type:article",
              "per-page": str(PAGE_SIZE), "mailto": CONTACT_MAIL}
    data = http_request(OPENALEX_API + "?" + urllib.parse.urlencode(params))
    out = []
    for w in data.get("results", []):
        title = w.get("display_name", "") or ""
        abstract = _rebuild_abstract(w.get("abstract_inverted_index"))
        if not relevant(board, title, abstract):
            continue
        loc = w.get("primary_location") or {}
        src = (loc.get("source") or {}) if isinstance(loc, dict) else {}
        if src.get("type") == "repository":           # 排除预印本仓库
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
            abstract=abstract, via="OpenAlex", added=added, board_key=board["key"]))
    return out


# ==================== 数据源 4: Elsevier Scopus ====================
def scopus_configured():
    return bool(os.environ.get("ELSEVIER_API_KEY", "").strip())

def fetch_scopus(board, days, added):
    key = os.environ.get("ELSEVIER_API_KEY", "").strip()
    if not key:
        return None                                     # 未配置 -> 调用方跳过该源
    end = today_cn()
    start = end - timedelta(days=days)
    # Scopus 用 query 内 PUBYEAR 限定年份, 本地再按 coverDate 精确到天
    date_clause = f'AND PUBYEAR IS {end.year}'
    query = f"{board['scopus']} {date_clause}"
    headers = {"X-ELS-ApiKey": key, "Accept": "application/json"}
    inst = os.environ.get("ELSEVIER_INSTTOKEN", "").strip()
    if inst:
        headers["X-ELS-Insttoken"] = inst
    def _get(use_standard_view):
        # 关键: 不传 field 参数(field 会覆盖 view, 且非订阅 key 请求 dc:description 等
        # 未授权字段会直接 400, 导致整个源 0 结果)。只用 view=STANDARD 取该权限下可得元数据。
        params = {"query": query, "count": str(PAGE_SIZE), "sort": "-coverDate"}
        if use_standard_view:
            params["view"] = "STANDARD"
        return http_request(SCOPUS_API + "?" + urllib.parse.urlencode(params),
                            headers=headers, timeout=45)
    try:
        try:
            data = _get(True)
        except RuntimeError as first_err:
            # STANDARD 视图若因 entitlement 报错, 降级为默认视图再试一次
            print(f"[warn] [scopus/{board['key']}] STANDARD 视图失败: {first_err}; "
                  f"降级默认视图重试", file=sys.stderr)
            data = _get(False)
    except Exception as e:  # noqa: BLE001
        # 打印明确原因(401=key无效/403=无Scopus权限或需insttoken/429=超配额), 交由上层记为该源失败
        print(f"[error] [scopus/{board['key']}] Scopus 请求失败, 请核对 key 权限/配额/insttoken: {e}",
              file=sys.stderr)
        raise
    entries = (data.get("search-results", {}) or {}).get("entry", []) or []
    # Scopus 在“0 命中”时会返回一条仅含 error 的伪 entry, 需剔除
    entries = [r for r in entries if r.get("dc:title") or r.get("eid")]
    print(f"[info] [scopus/{board['key']}] 原始返回 {len(entries)} 条")
    out = []
    for rec in entries:
        subtype = (rec.get("subtype") or "").lower()
        if subtype and subtype not in {"ar", "re", "cp"}:    # 仅 Article/Review/会议论文
            continue
        title = rec.get("dc:title", "") or ""
        # 非订阅 key 通常拿不到 dc:description 摘要; 留空后由其他源(EuropePMC/PubMed)跨源互补
        abstract = rec.get("dc:description", "") or ""
        cover = (rec.get("prism:coverDate", "") or "")[:10]
        # 本地按天再过滤一次(PUBYEAR 只到年)
        if cover and cover < start.isoformat():
            continue
        if not relevant(board, title, abstract):
            # Scopus 非订阅常缺摘要, 标题命中板块核心词即保留
            if not all(rx.search(title) for rx in board["require"][:1]):
                continue
        doi = (rec.get("prism:doi", "") or "")
        eid = rec.get("eid", "") or ""
        url = f"https://doi.org/{norm_doi(doi)}" if doi else \
            (f"https://www.scopus.com/record/display.uri?eid={eid}" if eid else "")
        it = make_item(
            title=title, source=rec.get("prism:publicationName", "") or "Scopus",
            authors=rec.get("dc:creator", "") or "",
            publish_date=cover, doi=doi, pmid="",
            abstract=abstract, via="Scopus", added=added, board_key=board["key"])
        if url:
            it["url"] = url
        out.append(it)
    print(f"[info] [scopus/{board['key']}] 通过过滤入库候选 {len(out)} 条")
    return out


# ==================== 可选 LLM 中文增强 ====================
CN_CHAR_RE = re.compile(r"[一-鿿]")

def _strip_code_fence(s):
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()

def llm_enhance(items):
    """用大模型把英文标题/摘要【改写式】概括为中文一句话总结与主要结论, 并判定研究物种。
    - 严禁照抄/逐句翻译: prompt 明确要求用自己的话重新概括
    - 兼容 OpenAI / DeepSeek / 火山方舟豆包(均为 OpenAI Chat Completions 协议)
    - 单条失败自动回退规则结果, 绝不阻断; 返回成功增强条数
    """
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key or not items:
        return 0
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini").strip()
    url = base + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    todo = [it for it in items if it.get("title")]
    ok = 0
    for it in todo:
        abstract = (it.get("abstract") or "").strip()
        prompt = (
            "你是资深生命科学与昆虫遗传学文献编辑。请阅读下面论文的【标题】与【摘要】,"
            "用你自己的话重新概括, 严禁直接照抄英文原句、严禁逐句硬译。\n"
            "只输出一个严格 JSON 对象(不要 markdown、不要解释、不要多余文字), 字段如下:\n"
            '{"takeaway":"一句话中文总结, 不超过60字, 需点出研究对象/技术手段与最关键发现",'
            '"conclusion":"主要结论中文, 不超过120字, 说明核心结论、机制或科学意义",'
            '"species_zh":"研究物种中文名, 无法确定就空字符串",'
            '"species_en":"研究物种通用英文名, 无法确定就空字符串",'
            '"species_latin":"研究物种拉丁学名(双名法, 属名首字母大写), 无法确定就空字符串"}\n'
            "要求: 信息必须来自给定材料, 不得编造; 摘要缺失时仅依据标题谨慎概括, 物种不确定一律留空。\n"
            f"【标题】{it.get('title','')}\n"
            f"【摘要】{abstract[:2200] if abstract else '(无摘要, 请仅依据标题谨慎概括)'}")
        messages = [{"role": "user", "content": prompt}]
        # 先请求 JSON 模式; 若该网关不支持 response_format, 自动退化为普通对话
        payload_variants = [
            json.dumps({"model": model, "messages": messages, "temperature": 0.2,
                        "response_format": {"type": "json_object"}}).encode("utf-8"),
            json.dumps({"model": model, "messages": messages, "temperature": 0.2}).encode("utf-8"),
        ]
        obj = None
        last_e = None
        for body in payload_variants:
            for attempt in (1, 2):
                try:
                    resp = http_request(url, headers=headers, data=body,
                                        timeout=60, retries=1)
                    content = resp["choices"][0]["message"]["content"]
                    obj = json.loads(_strip_code_fence(content))
                    break
                except Exception as e:  # noqa: BLE001
                    last_e = e
                    time.sleep(1.2 * attempt)
            if obj is not None:
                break
        if obj is None:
            print(f"[warn] LLM 总结失败一条, 保留规则结果《{it.get('title','')[:40]}...》: {last_e}",
                  file=sys.stderr)
            continue
        for k in ("takeaway", "conclusion", "species_zh", "species_en", "species_latin"):
            v = (obj.get(k) or "").strip()
            if v:
                it[k] = v
        it["llm_enhanced"] = True
        ok += 1
        time.sleep(0.25)
    print(f"[info] LLM 中文改写完成: 成功 {ok}/{len(todo)} 条")
    return ok


# ==================== 全局去重入库器(支持多板块) ====================
class Ingestor:
    def __init__(self, existing):
        self.existing = existing
        self.new_items = []
        self.doi_idx, self.pmid_idx, self.title_idx, self.url_idx = {}, {}, {}, {}
        for i, x in enumerate(existing):
            self._index(x, ("old", i))
    def _index(self, it, ref):
        if it.get("doi"):
            self.doi_idx[it["doi"]] = ref
        if it.get("pmid"):
            self.pmid_idx[it["pmid"]] = ref
        if it.get("url"):
            self.url_idx[it["url"]] = ref
        self.title_idx.setdefault(title_fingerprint(it.get("title", "")), ref)
    def add(self, it, board_key):
        if not it.get("title"):
            return False
        hit = None
        if it["doi"] and it["doi"] in self.doi_idx:
            hit = self.doi_idx[it["doi"]]
        elif it["pmid"] and it["pmid"] in self.pmid_idx:
            hit = self.pmid_idx[it["pmid"]]
        else:
            hit = self.title_idx.get(title_fingerprint(it["title"]))
        if hit is None:  # 全新
            it["boards"] = sorted({board_key})
            self.new_items.append(it)
            self._index(it, ("new", len(self.new_items) - 1))
            return True
        bucket, j = hit
        target = self.existing[j] if bucket == "old" else self.new_items[j]
        # 并入板块
        boards = set(target.get("boards", [])) | {board_key}
        target["boards"] = sorted(boards)
        # 互补空缺字段
        for field in ("abstract", "source", "authors", "url", "doi", "pmid",
                      "publish_date", "species_latin", "species_en", "species_zh",
                      "takeaway", "conclusion"):
            if not target.get(field) and it.get(field):
                target[field] = it[field]
        vias = {v.strip() for v in (target.get("via", "") or "").split(";") if v.strip()}
        vias.add(it["via"])
        target["via"] = ";".join(sorted(v for v in vias if v))
        if target.get("doi"):
            self.doi_idx.setdefault(target["doi"], hit)
        if target.get("pmid"):
            self.pmid_idx.setdefault(target["pmid"], hit)
        return False
    def add_manual(self, it):
        """合并人工/中文补录分片(manual_inbox): 沿用 doi/pmid/标题/URL 四重去重,
        但保留条目自带的 boards 与全部字段(不被单一板块覆盖)。全新条目返回 True。"""
        if not it.get("title"):
            return False
        if it.get("doi") and it["doi"] in self.doi_idx:
            return False
        if it.get("pmid") and it["pmid"] in self.pmid_idx:
            return False
        if it.get("url") and it["url"] in self.url_idx:
            return False
        if title_fingerprint(it.get("title", "")) in self.title_idx:
            return False
        # 补齐 schema 默认值, 保证下游字段齐全
        it.setdefault("boards", [])
        it["boards"] = sorted({b for b in it["boards"] if b})
        defaults = (("type", "wechat"), ("authors", ""), ("doi", ""), ("pmid", ""),
                    ("abstract", ""), ("species_latin", ""), ("species_en", ""),
                    ("species_zh", ""), ("takeaway", ""), ("conclusion", ""),
                    ("keywords", []), ("source", "manual_inbox"))
        for f, d in defaults:
            it.setdefault(f, d)
        self.new_items.append(it)
        self._index(it, ("new", len(self.new_items) - 1))
        return True


def migrate_legacy(existing):
    """旧版单板块数据平滑迁移: 补 boards 与新字段。"""
    changed = False
    for x in existing:
        if not x.get("boards"):
            x["boards"] = ["t2t"]            # 历史库均为 T2T 主题
            changed = True
        for f, default in (("species_latin", ""), ("species_en", ""), ("species_zh", ""),
                           ("takeaway", ""), ("conclusion", "")):
            if f not in x:
                x[f] = default
                changed = True
        if not x.get("takeaway") and x.get("abstract"):
            if x.get("type") == "paper":
                x["takeaway"] = extract_takeaway(x["abstract"])
                x["conclusion"] = extract_conclusion(x["abstract"])
                latin, en, zh = detect_species(x.get("title", ""), x["abstract"])
                x["species_latin"] = x.get("species_latin") or latin
                x["species_en"] = x.get("species_en") or en
                x["species_zh"] = x.get("species_zh") or zh
            else:
                x["takeaway"] = x["abstract"][:120]
    return changed

def load_existing(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} 顶层结构必须是数组 list")
    return data

def load_manual_inbox(data_path):
    """读取与 literature.json 同目录的 manual_inbox/*.json 人工补录分片。
    每个分片为一个条目数组(可跨板块, boards 由分片自带); 由主流程去重后并入主库。
    分片合并后保留在仓库作为来源凭证, 靠去重保证幂等, 不会重复入库。"""
    inbox_dir = os.path.join(os.path.dirname(data_path), "manual_inbox")
    out = []
    if not os.path.isdir(inbox_dir):
        return out
    for fn in sorted(os.listdir(inbox_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(inbox_dir, fn), "r", encoding="utf-8") as f:
                arr = json.load(f)
            if isinstance(arr, list):
                out.extend(x for x in arr if isinstance(x, dict))
        except Exception as e:  # noqa: BLE001 单个分片损坏不影响主流程
            print(f"[warn] 读取人工补录分片 {fn} 失败, 跳过: {e}", file=sys.stderr)
    return out

# ==================== 主流程 ====================
def main():
    ap = argparse.ArgumentParser(description="多专题多源采集已发表文献并入 literature.json")
    ap.add_argument("--days", type=int, default=3, help="初始回溯天数(默认3)")
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "literature.json"))
    args = ap.parse_args()
    data_path = os.path.abspath(args.data)
    existing = load_existing(data_path)
    legacy_changed = migrate_legacy(existing)
    added = today_cn()

    source_fns = [
        ("EuropePMC", lambda b, d: fetch_epmc(b, d, added)),
        ("PubMed", lambda b, d: fetch_pubmed(b, d, added)),
        ("OpenAlex", lambda b, d: fetch_openalex(b, d, added)),
        ("Scopus", lambda b, d: fetch_scopus(b, d, added)),
    ]
    if not scopus_configured():
        print("[info] 未配置 ELSEVIER_API_KEY, 本次跳过 Elsevier Scopus 源(其余三源正常)")
        source_fns = [s for s in source_fns if s[0] != "Scopus"]

    ing = Ingestor(existing)
    # 先合并人工中文资讯补录分片(manual_inbox/*.json), 保留其自带 boards 并纳入同一去重索引
    manual_items = load_manual_inbox(data_path)
    n_manual = sum(1 for mit in manual_items if ing.add_manual(mit))
    if n_manual:
        print(f"[info] manual_inbox 合并新增人工补录 {n_manual} 条(收到分片 {len(manual_items)} 条)")
    failed_sources, board_new_count = set(), {b["key"]: 0 for b in BOARDS}
    any_stream_ok = False

    for board in BOARDS:
        cap_days = board.get("max_days", MAX_EXPAND_DAYS)
        window = args.days
        # 自适应扩窗: 该板块新增不足目标条数时, 按 3->7->15... 逐步扩大回溯窗口,
        # 直到凑够 PER_BOARD_TARGET 条或达到该板块窗口上限 cap_days
        while True:
            for name, fn in source_fns:
                try:
                    items = fn(board, window)
                    if items is None:
                        continue
                    any_stream_ok = True
                    added_here = 0
                    for it in items:
                        if ing.add(it, board["key"]):
                            added_here += 1
                    board_new_count[board["key"]] += added_here
                    print(f"[info] [{board['key']}] {name} 窗口{window}天 候选{len(items)} "
                          f"新入{added_here}")
                except Exception as e:  # noqa: BLE001 单源失败不拖垮
                    failed_sources.add(name)
                    print(f"[warn] [{board['key']}] 源 {name} 失败跳过: {e}", file=sys.stderr)
            if board_new_count[board["key"]] >= PER_BOARD_TARGET or window >= cap_days:
                break
            window = min(window * 2 + 1, cap_days)

    if not any_stream_ok and n_manual == 0:
        print("[error] 所有数据源均失败且无人工补录, literature.json 保持不变", file=sys.stderr)
        sys.exit(1)

    if ing.new_items:
        existing.extend(ing.new_items)

    # LLM 中文改写式总结(可选): 本次新增优先, 并回填“总结仍是英文规则版”的历史条目
    n_llm = 0
    if os.environ.get("OPENAI_API_KEY", "").strip():
        max_n = int(os.environ.get("LLM_MAX_ITEMS", "60"))
        new_ids = {id(x) for x in ing.new_items}

        def _needs_llm(x):
            if x.get("llm_enhanced") or not x.get("title"):
                return False
            return not CN_CHAR_RE.search(x.get("takeaway") or "")  # 总结还没有中文 -> 需大模型改写

        cand_new = [x for x in existing if id(x) in new_ids and _needs_llm(x)]
        cand_old = [x for x in existing if id(x) not in new_ids and _needs_llm(x)]
        cand_old.sort(key=lambda x: x.get("publish_date", ""), reverse=True)
        n_back = max(0, min(max_n, len(cand_new) + len(cand_old)) - len(cand_new))
        todo = (cand_new + cand_old)[:max_n]
        if todo:
            print(f"[info] 检测到 LLM key, 对 {len(todo)} 条做中文改写式总结"
                  f"(本次新增 {len(cand_new)} + 历史回填 {n_back})")
            n_llm = llm_enhance(todo)

    changed = bool(ing.new_items) or legacy_changed or n_llm > 0
    if changed:
        existing.sort(key=lambda x: (x.get("publish_date", ""), x.get("added_at", "")),
                       reverse=True)
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    print("[ok] 各板块本次新增:", {BOARD_BY_KEY[k]["name"]: v for k, v in board_new_count.items()})
    print(f"[ok] 本次新增合计 {len(ing.new_items)} 条, 当前总计 {len(existing)} 条 -> {data_path}")
    if failed_sources:
        print(f"[warn] 本次失败源: {', '.join(sorted(failed_sources))}(明日自动重试)", file=sys.stderr)
    print(f"NEW_COUNT={len(ing.new_items)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
