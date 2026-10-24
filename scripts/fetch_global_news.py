#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全球（非中文互联网）科研资讯每日多语种检索
==========================================
为 5 个研究板块检索中国大陆中文互联网之外的每日新闻/媒体/科研资讯:
  t2t          T2T（端粒到端粒）基因组
  cmed         稻纵卷叶螟功能基因组学 (Cnaphalocrocis medinalis)
  sexdet       昆虫性别决定演化机制
  ppi          蛋白互作预测
  insecticide  新型杀虫剂
数据源(均免费、无需 key):
  1. Google News RSS (主源): 多地区/多语言 locale, 英语为主, 覆盖港澳台媒体
     (zh-Hant HK/TW)、日韩、西/葡/法/德/意/印尼/俄等语种的本地科技媒体;
     关键词以拉丁学名/英文专名为主锚点, 辅以各语种翻译词。
  2. GDELT 2.0 DOC API (备源): 按 sourcelang 做多语种全球新闻检索,
     严格限速(>=8s/次); 中文仅取港澳台及海外华文源(不含中国大陆)。
产出:
  manual_inbox/global-YYYY-MM-DD.json 分片(条目数组, type="news"),
  由 fetch_literature.py 现有的 load_manual_inbox 合并逻辑(doi/pmid/URL/标题
  四重去重, 保留条目自带 boards/type) 并入 literature.json, 进而进入当天分板块
  邮件与网页看板。分片由 Actions 运行时生成、随 job 销毁, 无需入库。
容错:
  - 单 locale/单语种请求失败只告警不中断; 两源全部失败也正常退出(不阻断流水线)
  - GDELT 前两个语种连续被限流即熔断, 跳过剩余语种(出口 IP 被整体限流时不再空等)
  - 同一天重复运行幂等: 与已有当日分片按 id/URL/标题去重后合并
手动运行:
  python3 scripts/fetch_global_news.py --days 2
  python3 scripts/fetch_global_news.py --no-gdelt     # 只跑 Google News
  python3 scripts/fetch_global_news.py --no-gnews     # 只跑 GDELT
"""
import argparse
import hashlib
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
from email.utils import parsedate_to_datetime

CN_TZ = timezone(timedelta(hours=8))
TIMEOUT = 30
RETRY = 2
PER_BOARD_CAP = 25          # 每板块每天最多收录条数
TOTAL_CAP = 100             # 每天全球资讯总条数上限
GNEWS_PAUSE = 0.4           # Google News 请求间隔(秒)
GDELT_PAUSE = 10            # GDELT 官方限速 1 req / 5s; 共享 IP 环境取 10s
TOOL_NAME = "t2t-literature-board"
BOARDS_ORDER = ["t2t", "cmed", "sexdet", "ppi", "insecticide"]

# ==================== Google News locale 矩阵 ====================
# (hl, gl, ceid, 语言标签, 是否英语源)
GNEWS_LOCALES = [
    ("en-US", "US", "US:en", "en", True),
    ("en-GB", "GB", "GB:en", "en", True),
    ("en-IN", "IN", "IN:en", "en", True),
    ("en-CA", "CA", "CA:en", "en", True),
    ("en-AU", "AU", "AU:en", "en", True),
    ("zh-HK", "HK", "HK:zh-Hant", "zh-Hant-HK", False),
    ("zh-TW", "TW", "TW:zh-Hant", "zh-Hant-TW", False),
    ("ja", "JP", "JP:ja", "ja", False),
    ("ko", "KR", "KR:ko", "ko", False),
    ("es", "ES", "ES:es", "es-ES", False),
    ("es-419", "MX", "MX:es-419", "es-MX", False),
    ("pt-BR", "BR", "BR:pt-419", "pt-BR", False),
    ("fr", "FR", "FR:fr", "fr", False),
    ("de", "DE", "DE:de", "de", False),
    ("it", "IT", "IT:it", "it", False),
    ("id", "ID", "ID:id", "id", False),
    ("ru", "RU", "RU:ru", "ru", False),
]

# ==================== 板块关键词与相关性把关 ====================
# 英语主检索式(各英语 locale 共用)
EN_QUERIES = {
    "t2t": ('("telomere-to-telomere" OR "telomere to telomere" OR T2T OR gapless OR "gap-free") '
            'AND (genome OR genomic OR assembly OR chromosome OR sequencing)'),
    "cmed": ('("Cnaphalocrocis medinalis" OR "rice leaffolder" OR "rice leaf folder" OR '
             '"rice leaf roller" OR "rice leafroller")'),
    "sexdet": ('("sex determination" OR "sex-determining" OR doublesex OR "sex-lethal" OR '
               'transformer OR feminizer OR Wolbachia OR "sex chromosome") '
               'AND (insect OR mosquito OR moth OR fly OR beetle OR wasp OR silkworm OR '
               'drosophila OR bombyx OR aphid OR planthopper)'),
    "ppi": ('("protein-protein interaction" OR "protein-protein interactions" OR interactome OR '
            '"protein complex" OR "protein complexes" OR "binding affinity" OR "protein docking") '
            'AND (predict* OR prediction OR "deep learning" OR "machine learning" OR AlphaFold OR '
            'model OR network OR structure OR docking OR interactome OR computational)'),
    "insecticide": ('(insecticide OR insecticidal OR biopesticide OR bioinsecticide OR pesticide OR '
                    'neonicotinoid OR diamide OR "Bacillus thuringiensis" OR dsRNA pesticide OR '
                    '"RNAi-based" pesticide OR botanical pesticide) '
                    'AND (novel OR new OR mechanism OR target OR resistance OR resistant OR '
                    'discovery OR "mode of action" OR toxicity OR bioinsecticide OR biopesticide)'),
}

# 各非英语语种补充的本地语锚点(拉丁/英文专名已在 BASE_ANCHORS 中全局追加)
NATIVE_TERMS = {
    "t2t": {
        "zh-Hant-HK": "端粒到端粒 OR 無缺口基因組",
        "zh-Hant-TW": "端粒到端粒 OR 無缺口基因組",
        "ja": "テロメア間 OR ギャップレスゲノム OR 完全ゲノム",
        "ko": "텔로미어 OR 무결손 게놈 OR 완전 게놈",
        "es-ES": "genoma telómero OR genoma sin huecos",
        "es-MX": "genoma telómero OR genoma sin huecos",
        "pt-BR": "genoma telômero OR genoma sem lacunas",
        "fr": "télomère à télomère OR génome sans trou",
        "de": "Telomer-zu-Telomer OR lückenloses Genom",
        "it": "genoma senza gap OR telomero-telomero",
        "id": "genom tanpa celah OR telomer-ke-telomer",
        "ru": "теломера к теломере OR бесшовный геном",
    },
    "sexdet": {
        "zh-Hant-HK": "昆蟲 性別決定 OR 蚊子 性別決定",
        "zh-Hant-TW": "昆蟲 性別決定 OR 蚊子 性別決定",
        "ja": "昆虫 性決定 OR ボルバキア 性決定",
        "ko": "곤충 성결정 OR 볼바키아",
        "es-ES": "determinación del sexo insecto OR determinación sexual mosquito",
        "es-MX": "determinación del sexo insecto OR determinación sexual mosquito",
        "pt-BR": "determinação do sexo inseto OR determinação sexual mosquito",
        "fr": "détermination du sexe insecto OR détermination sexuelle moustique",
        "de": "Geschlechtsbestimmung Insekt OR Geschlechtsbestimmung Moskito",
        "it": "determinazione del sesso insetto OR zanzara Wolbachia",
        "id": "penentuan kelamin serangga OR nyamuk Wolbachia",
        "ru": "определение пола насекомых OR определение пола комара",
    },
    "ppi": {
        "zh-Hant-HK": "蛋白質交互作用 OR 蛋白互作 預測",
        "zh-Hant-TW": "蛋白質交互作用 OR 蛋白互作 預測",
        "ja": "タンパク質間相互作用 OR タンパク質複合体",
        "ko": "단백질 상호작용 예측 OR 단백질 복합체",
        "es-ES": "interacción proteína-proteína OR proteína docking",
        "es-MX": "interacción proteína-proteína OR proteína acoplamiento",
        "pt-BR": "interação proteína-proteína OR proteína docking",
        "fr": "interaction protéine-protéine OR protéine docking",
        "de": "Protein-Protein-Interaktion OR Proteinkomplex",
        "it": "interazione proteina-proteina OR complesso proteico",
        "id": "interaksi protein-protein OR kompleks protein",
        "ru": "белок-белковые взаимодействия OR белковый комплекс",
    },
    "insecticide": {
        "zh-Hant-HK": "新殺蟲劑 OR 生物農藥 OR 蘇力菌",
        "zh-Hant-TW": "新殺蟲劑 OR 生物農藥 OR 蘇力菌",
        "ja": "新規殺虫剤 OR 生物農薬 OR ネオニコチノイド",
        "ko": "신규 살충제 OR 바이오농약 OR 네오니코티노이드",
        "es-ES": "nuevo insecticida OR bioinsecticida OR bioplaguicida",
        "es-MX": "nuevo insecticida OR bioinsecticida OR bioplaguicida",
        "pt-BR": "novo inseticida OR bioinseticida OR biopesticida",
        "fr": "nouvel insecticide OR bioinsecticide OR biopesticide",
        "de": "neues Insektizid OR Bioinsektizid OR biologisches Pestizid",
        "it": "nuovo insetticida OR bioinsetticida OR biopesticida",
        "id": "insektisida baru OR biopestisida",
        "ru": "новый инсектицид OR биоинсектицид OR биопестицид",
    },
    "cmed": {
        "zh-Hant-HK": "稻縱卷葉螟",
        "zh-Hant-TW": "稻縱卷葉螟",
        "ko": "혹명나방",
        # 日语/西语等本地稻名生僻, 仅用拉丁学名+英文名(在 BASE_ANCHORS)
    },
}

# 非英语检索时, 与本地语词并列的拉丁/英文专名锚点(避免漏掉本土媒体里的英文专名)
LANG_INVARIANT = {
    "t2t": "T2T OR telomere OR gapless",
    "cmed": '"Cnaphalocrocis medinalis" OR "rice leaffolder" OR "rice leaf folder" OR "rice leaf roller"',
    "sexdet": "doublesex OR Wolbachia OR transformer OR sex-lethal",
    "ppi": "AlphaFold OR interactome OR PPI OR docking",
    "insecticide": "insecticide OR biopesticide OR neonicotinoid OR diamide OR "
                   '"Bacillus thuringiensis" OR dsRNA',
}

# ---- 相关性正则(应用于标题, 新闻 RSS 无正文摘要) ----
def _re(pat):
    return re.compile(pat, re.I | re.U)

T2T_ANCHOR = _re(
    r"telomere[\s\-]?to[\s\-]?telomere|telomere.à.télomère|telomere.zu.telomer|telomero.telomero|"
    r"\bt2t\b|gap[\s\-]?less|gap[\s\-]?free|端粒到端粒|無缺口|无缺口|ギャップレス|テロメア間|"
    r"무결손|텔로미어|genoma (telómero|tel[ôo]mero|sin huecos|sem lacunas)|génome sans trou|"
    r"lückenlos(es)? genom|telomer.ke.telomer|бесшовн\w* геном|теломер\w* к теломер")
GENOME_CTX = _re(
    r"genom|genomic|assembly|chromosom|sequenc|dna|基因組|基因组|ゲノム|게놈|染色體|染色体|"
    r"геном|хромосом")

CMED_ANCHOR = _re(
    r"cnaphalocrocis medinalis|rice\s*leaf[\s\-]?(folder|roller)|rice leaffolder|"
    r"稻縱卷葉螟|稻纵卷叶螟|혹명나방")

SEX_ANCHOR = _re(
    r"sex[\s\-]?determin|doublesex|\bdsx\b|sex[\s\-]?lethal|femini[sz]er|wolbachia|"
    r"haplodiploid|sex chromosome|性別決定|性别决定|性決定|성결정|볼바키아|"
    r"determinaci[óo]n del sexo|determinaç[ãa]o do sexo|détermination du sexe|"
    r"geschlechtsbestimmung|determinazione del sesso|penentuan kelamin|"
    r"определени\w* пола")
INSECT_CTX = _re(
    r"insect|mosquito|moth|fly|flies|beetle|wasp|aphid|silkworm|drosophila|bombyx|"
    r"lepidoptera|diptera|coleoptera|planthopper|bug|pest|昆蟲|昆虫|곤충|蚊|蟲|虫|"
    r"insecto|inseto|insecte|insekt|insetto|serangga|насеком\w*|комар")

PPI_ANCHOR = _re(
    r"protein[\s\u2013-]?protein interaction|protein interacci[óo]n|interacci[óo]n prote[ií]na|"
    r"intera[çc][ãa]o prote[ií]na|"
    r"interaction prot[ée]ine|prot[ée]ine.prot[ée]ine interaction|"
    r"protein.protein.interaktion|interaktion protein.protein|"
    r"interazione proteina.proteina|interaksi protein|"
    r"белок.белков\w* взаимодейств|\bppi\b|interactome|protein complex|"
    r"prote[ií]na docking|蛋白質交互作用|蛋白互作|蛋白质相互作用|タンパク質間相互作用|"
    r"단백질 상호작용|prote[ií]na.complex|protein docking|binding affinity")
PPI_CTX = _re(
    r"predict|forecast|deep learning|machine learning|neural|alphafold|artificial intelligence|"
    r"\bmodel\b|network|docking|structure|computational|algorithm|in silico|interactome|"
    r"預測|预测|予測|예측|predicci|prediç|prédiction|vorhersage|previsione|структур\w*|"
    r"構造|상호작용 예측|deep learning")
PPI_EXCLUDE = _re(
    r"network pharmacology|traditional chinese medicine|\btcm\b|herbal|serum|urine|"
    r"diagnostic (marker|value|signature)|osteoarthritis|parkinson|alzheimer|\bcancer\b|"
    r"tumo[ur]|carcinoma|fibrosis|clinical trial|\bpatients?\b|hepatotoxic|nephrotoxic|"
    r"protein powder|bodybuilding|whey protein|nutrition supplement|protein bar|dietary supplement")

INS_ANCHOR = _re(
    r"insecticid|bioinsecticid|biopesticid|pesticide|acaricide|neonicotinoid|diamide|"
    r"bacillus thuringiensis|\bbt toxin\b|dsrna pesticide|bioinsecticide|"
    r"殺蟲劑|杀虫剂|殺虫剤|살충제|生物農藥|生物农药|生物農薬|바이오농약|蘇力菌|苏力菌|"
    r"insecticida|bioinsecticida|bioplaguicida|inseticida|bioinseticida|bioinsecticide|"
    r"bioinsektizid|biologisches pestizid|bioinsetticida|biopestisida|инсектицид|"
    r"биоинсектицид|биопестицид|ネオニコチノイド")
INS_CTX = _re(
    r"novel|new |mechanism|mode of action|target|resistan|discovery|toxicity|dsrna|diamide|"
    r"新殺|新杀|新規|신규|새로운|nuevo|novo|nouvea?l?|neue\b|nuovo|baru|нов\w+|"
    r"生物農|生物农|生物農薬|바이오농약|bio|molecular|receptor|gene|crispr|rnai")
INS_EXCLUDE = _re(
    r"river|\bwater\b|wastewater|effluent|drinking|groundwater|\blake\b|sediment|"
    r"tap water|food residue|maximum residue limit|mrl|water quality|surface water|"
    r"地表水|水體|水体|河流水|自來水|自来水")

# GDELT: 各 sourcelang 对应的本地语锚点(并入大 OR 查询)
GDELT_SOURCELANGS = [
    ("english", "en", ""),
    ("chinese", "zh-Hant", "country:HKG OR country:TWN OR country:MAC OR country:SGP OR country:MYS"),
    ("japanese", "ja", ""),
    ("korean", "ko", ""),
    ("spanish", "es", ""),
    ("portuguese", "pt", ""),
    ("french", "fr", ""),
    ("german", "de", ""),
    ("italian", "it", ""),
    ("indonesian", "id", ""),
    ("russian", "ru", ""),
    ("arabic", "ar", ""),
    ("vietnamese", "vi", ""),
    ("turkish", "tr", ""),
]


def http_get(url, timeout=TIMEOUT, retries=RETRY):
    headers = {"User-Agent": f"{TOOL_NAME}/1.0 (academic digest; global news)",
               "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*"}
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"GET 失败 {url[:90]}: {last}")


def gdelt_get_json(url, max_retries=3):
    """GDELT 限速严格(共享 IP 常 429 或返回 HTML 限流页):
    429/非 JSON 时按 15/30/45s 退避重试, 最终失败返回 None。"""
    headers = {"User-Agent": f"{TOOL_NAME}/1.0 (academic digest; global news)",
               "Accept": "application/json"}
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            data = json.loads(raw)
            return data
        except urllib.error.HTTPError as e:
            wait = 15 * attempt
            print(f"[warn] GDELT HTTP {e.code}, {wait}s 退避({attempt}/{max_retries})",
                  file=sys.stderr)
            if e.code == 429:
                time.sleep(wait)
                continue
            time.sleep(3)
        except json.JSONDecodeError:
            wait = 15 * attempt
            print(f"[warn] GDELT 返回非 JSON(限流页), {wait}s 退避({attempt}/{max_retries})",
                  file=sys.stderr)
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] GDELT 请求异常 {str(e)[:100]}, {15 * attempt}s 退避", file=sys.stderr)
            time.sleep(15 * attempt)
    return None


def title_fingerprint(title):
    return re.sub(r"\W+", "", (title or "").lower())


def clean_title(t, source_name):
    """Google News RSS 标题形如 'Headline - Publisher', 去掉尾部出版方。"""
    t = re.sub(r"\s+", " ", (t or "")).strip()
    if source_name and t.endswith(" - " + source_name):
        t = t[: -(len(" - " + source_name))].strip()
    elif " - " in t:
        # 无 source 标签时, 仅当最后一段像出版方(短且无句末标点)才剥离
        head, tail = t.rsplit(" - ", 1)
        if len(tail) <= 40 and "\n" not in tail:
            t = head.strip()
    return t


def rfc822_to_date(s):
    try:
        return parsedate_to_datetime(s).astimezone(CN_TZ).date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


def seendate_to_date(s):
    # GDELT: 20261021T170000Z
    try:
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    except Exception:  # noqa: BLE001
        return ""


# ==================== 相关性分类 ====================
def classify(title, desc=""):
    """返回命中的板块 key 列表(可多个); 不命中返回 []。"""
    blob = f"{title or ''} {desc or ''}"
    boards = []
    if T2T_ANCHOR.search(blob) and GENOME_CTX.search(blob):
        boards.append("t2t")
    if CMED_ANCHOR.search(blob):
        boards.append("cmed")
    if SEX_ANCHOR.search(blob) and INSECT_CTX.search(blob):
        boards.append("sexdet")
    if (PPI_ANCHOR.search(blob) and PPI_CTX.search(blob)
            and not PPI_EXCLUDE.search(blob)):
        boards.append("ppi")
    if INS_ANCHOR.search(blob) and INS_CTX.search(blob) \
            and not (INS_EXCLUDE.search(blob) and not INS_CTX.search(blob)):
        # 残留/水体监测类: 命中环境词且无机制/靶标/抗性等研究语境 -> 排除
        if not (INS_EXCLUDE.search(blob)
                and not re.search(r"mechan|target|receptor|resistan|mode of action|dsrna|"
                                  r"基因|target site|作用靶標|作用靶标|機制|机制|機轉|내성|저항성",
                                  blob, re.I)):
            boards.append("insecticide")
    # 保持固定顺序
    return [b for b in BOARDS_ORDER if b in boards]


# ==================== 源 1: Google News RSS ====================
def gnews_query(board, lang):
    if lang == "en":
        return EN_QUERIES[board]
    terms = NATIVE_TERMS[board].get(lang)
    parts = [LANG_INVARIANT[board]]
    if terms:
        parts.append(f"({terms})")
    q = "(" + " OR ".join(parts) + ")"  # 括号保证 AND 优先级高于 OR, 语境词约束所有锚点
    # cmed 物种名本身已足够特异; 其余板块非英语锚点多为宽词, 追加基因组/昆虫语境
    if board == "t2t":
        q += " AND (genome OR assembly OR chromosome OR 基因組 OR ゲノム OR 게놈 OR genoma OR génome OR Genom OR геном)"
    elif board == "sexdet":
        q += " AND (insect OR mosquito OR moth OR 昆蟲 OR 昆虫 OR 곤충 OR insecto OR inseto OR insecte OR Insekt OR insetto OR serangga OR насекомое)"
    elif board == "ppi":
        q += " AND (predict OR model OR AlphaFold OR network OR structure OR 預測 OR 予測 OR 예측 OR predicción OR modelo OR modèle OR Modell OR modello OR структура)"
    elif board == "insecticide":
        q += " AND (novel OR new OR mechanism OR resistance OR target OR 新 OR 新規 OR nuevo OR novo OR nouveau OR neu OR nuovo OR baru OR новый)"
    return q


def fetch_gnews(days):
    """逐 locale × 板块抓取 Google News RSS; 返回原始条目列表。"""
    out = []
    n_req, n_fail = 0, 0
    board_raw = {b: 0 for b in BOARDS_ORDER}
    for hl, gl, ceid, lang, is_en in GNEWS_LOCALES:
        for board in BOARDS_ORDER:
            q = gnews_query(board, lang) + f" when:{days}d"
            url = ("https://news.google.com/rss/search?"
                   + urllib.parse.urlencode({"q": q, "hl": hl, "gl": gl, "ceid": ceid}))
            n_req += 1
            try:
                raw = http_get(url)
                root = ET.fromstring(raw)
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                print(f"[warn] Google News {lang}/{board} 失败: {str(e)[:120]}", file=sys.stderr)
                time.sleep(GNEWS_PAUSE * 3)
                continue
            board_raw[board] += len(root.findall(".//item"))
            for item in root.findall(".//item"):
                title = item.findtext("title") or ""
                src_el = item.find("source")
                source_name = (src_el.text or "").strip() if src_el is not None else ""
                link = item.findtext("link") or ""
                pub = rfc822_to_date(item.findtext("pubDate") or "")
                title = clean_title(title, source_name)
                out.append({
                    "title": title,
                    "url": link,
                    "source": source_name or "Google News",
                    "publish_date": pub,
                    "via": f"Google News:{lang}",
                    "lang": lang,
                })
            time.sleep(GNEWS_PAUSE)
    print(f"[info] Google News: 请求 {n_req} 次, 失败 {n_fail} 次, 原始候选 {len(out)} 条")
    print(f"[info] Google News 各板块原始候选(去重前): {board_raw}")
    return out


# ==================== 源 2: GDELT ====================
def gdelt_native_anchors(lang):
    """各语种并入大 OR 查询的本地语锚点(跨板块合并)。
    zh-Hant 合并港/台两套关键词。"""
    keys = ["zh-Hant-HK", "zh-Hant-TW"] if lang == "zh-Hant" else [lang]
    bag = []
    for board in BOARDS_ORDER:
        for k in keys:
            term = NATIVE_TERMS[board].get(k)
            if term:
                bag.append("(" + term + ")")
                break
    return " OR ".join(bag)


def gdelt_master_query(lang, country_or):
    groups = [
        '("telomere-to-telomere" OR "telomere to telomere" OR T2T OR gapless OR "gap-free")',
        '("Cnaphalocrocis medinalis" OR "rice leaffolder" OR "rice leaf folder" OR "rice leaf roller")',
        '("sex determination" OR doublesex OR "sex-lethal" OR feminizer OR Wolbachia OR "sex chromosome")',
        '("protein-protein interaction" OR interactome OR "protein complex" OR "binding affinity")',
        '(insecticide OR biopesticide OR bioinsecticide OR neonicotinoid OR diamide OR "Bacillus thuringiensis")',
    ]
    native = gdelt_native_anchors(lang)
    if native:
        groups.append("(" + native + ")")
    q = "(" + " OR ".join(groups) + ")"
    if country_or:
        q += f" ({country_or})"
    return q


def fetch_gdelt(days):
    out = []
    n_req, n_fail = 0, 0
    consec_fail = 0
    for i, (src_lang, lang, country_or) in enumerate(GDELT_SOURCELANGS):
        q = gdelt_master_query(lang, country_or) + f" sourcelang:{src_lang}"
        url = ("https://api.gdeltproject.org/api/v2/doc/doc?"
               + urllib.parse.urlencode({
                   "query": q, "mode": "ArtList", "format": "json",
                   "maxrecords": 200, "timespan": f"{days}d", "sort": "DateDesc"}))
        n_req += 1
        # 前两个语种(english/chinese)只做 2 次重试, 快速判定本 IP 是否整体被限
        retries = 2 if i < 2 else 3
        data = gdelt_get_json(url, max_retries=retries)
        if data is None:
            n_fail += 1
            arts = []
            consec_fail += 1
            # 熔断: 最前面的语种连续失败说明出口 IP 被整体限流, 跳过剩余语种,
            # 本次以 Google News 为主源, 避免每个语种空等 90s 拖垮整个工作流
            if consec_fail >= 2 and i <= 2:
                print("[warn] GDELT 前两个语种连续被限流, 判定本出口 IP 整体受限, "
                      "跳过剩余语种(本次以 Google News 为主源)", file=sys.stderr)
                break
        else:
            arts = data.get("articles", []) or []
            consec_fail = 0
        for a in arts:
            title = re.sub(r"\s+", " ", a.get("title", "") or "").strip()
            link = a.get("url", "") or ""
            if not title or not link:
                continue
            out.append({
                "title": title,
                "url": link,
                "source": a.get("domain", "") or "GDELT",
                "publish_date": seendate_to_date(a.get("seendate", "")),
                "via": f"GDELT:{lang}",
                "lang": lang,
                "country": a.get("sourcecountry", ""),
            })
        print(f"[info] GDELT {src_lang}: 候选 {len(arts)} 条")
        if i < len(GDELT_SOURCELANGS) - 1:
            time.sleep(GDELT_PAUSE)
    print(f"[info] GDELT: 请求 {n_req} 次, 失败 {n_fail} 次, 原始候选 {len(out)} 条")
    return out


# ==================== 合并 / 去重 / 分级 ====================
def merge_and_classify(raw_items, today):
    """跨源按标准化标题去重(GDELT 原文 URL 优先于 Google News 跳转链接),
    再做板块分类与配额。"""
    merged = {}
    # 先放 GDELT(canonical URL), 后放 Google News, 同标题时保留 GDELT 的 URL 但合并 via/boards
    for it in sorted(raw_items, key=lambda x: 0 if x["via"].startswith("GDELT") else 1):
        fp = title_fingerprint(it["title"])
        if not fp:
            continue
        hit = merged.get(fp)
        if hit is None:
            merged[fp] = dict(it)
        else:
            # 同源重复或跨源同题: 保留 canonical URL, 记录另一来源
            vias = {v.strip() for v in hit["via"].split(";") if v.strip()}
            vias.add(it["via"])
            hit["via"] = ";".join(sorted(vias))
            if it["via"].startswith("GDELT") and hit["via"].startswith("Google News"):
                hit["url"] = it["url"]
                hit["source"] = it["source"]
            if not hit.get("publish_date") and it.get("publish_date"):
                hit["publish_date"] = it["publish_date"]

    accepted = []
    board_count = {b: 0 for b in BOARDS_ORDER}
    for fp, it in merged.items():
        boards = classify(it["title"])
        if not boards:
            continue
        # 板块配额: 任一板块未超即收
        if all(board_count[b] >= PER_BOARD_CAP for b in boards):
            continue
        for b in boards:
            board_count[b] += 1
        it["boards"] = boards
        accepted.append(it)
        if len(accepted) >= TOTAL_CAP:
            break

    accepted.sort(key=lambda x: (x.get("publish_date", ""), x["title"]), reverse=True)
    return accepted, board_count


def to_item(it, today):
    url = it["url"]
    h = hashlib.sha1((url or it["title"]).encode("utf-8")).hexdigest()[:10]
    board_names = {
        "t2t": "T2T 端粒到端粒基因组",
        "cmed": "稻纵卷叶螟功能基因组学",
        "sexdet": "昆虫性别决定演化机制",
        "ppi": "蛋白互作预测",
        "insecticide": "新型杀虫剂",
    }
    lang = it.get("lang", "")
    pub = it.get("publish_date") or today
    item = {
        "id": f"news-{pub.replace('-', '')}-{h}",
        "title": it["title"].rstrip("."),
        "type": "news",
        "boards": sorted(it["boards"]),
        "source": it.get("source", ""),
        "authors": "",
        "publish_date": pub,
        "added_at": today,
        "url": url,
        "doi": "",
        "pmid": "",
        "abstract": "",
        "species_latin": "",
        "species_en": "",
        "species_zh": "",
        "takeaway": it["title"].rstrip("."),
        "conclusion": "",
        "keywords": [board_names[b] for b in sorted(it["boards"])] + ["全球资讯", lang],
        "via": it.get("via", ""),
    }
    # 内置物种词典: 标题命中稻纵卷叶螟等时自动补三名
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import fetch_literature as fl  # noqa: E402
        latin, en, zh = fl.detect_species(it["title"], "")
        item["species_latin"], item["species_en"], item["species_zh"] = latin, en, zh
    except Exception:  # noqa: BLE001
        pass
    return item


def main():
    ap = argparse.ArgumentParser(
        description="全球多语种科研资讯检索, 写入 manual_inbox/global-*.json 分片")
    ap.add_argument("--days", type=int, default=2, help="回溯天数(默认2)")
    ap.add_argument("--no-gnews", action="store_true", help="跳过 Google News")
    ap.add_argument("--no-gdelt", action="store_true", help="跳过 GDELT")
    ap.add_argument("--out", default=None,
                    help="分片输出路径(默认 manual_inbox/global-YYYY-MM-DD.json)")
    args = ap.parse_args()

    today = datetime.now(CN_TZ).date().isoformat()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inbox_dir = os.path.join(root, "manual_inbox")
    os.makedirs(inbox_dir, exist_ok=True)
    out_path = args.out or os.path.join(inbox_dir, f"global-{today}.json")

    raw = []
    if not args.no_gnews:
        try:
            raw.extend(fetch_gnews(args.days))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] Google News 源整体失败: {e}", file=sys.stderr)
    if not args.no_gdelt:
        try:
            raw.extend(fetch_gdelt(args.days))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] GDELT 源整体失败: {e}", file=sys.stderr)

    if not raw:
        print("[warn] 两源均无数据, 本次不写分片(不阻断流水线)")
        return

    accepted, board_count = merge_and_classify(raw, today)
    new_items = [to_item(it, today) for it in accepted]

    # 同日幂等: 读入已有分片, 按 id/URL/标题去重合并
    existing = []
    if os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:  # noqa: BLE001
            existing = []
    seen_ids = {x.get("id") for x in existing}
    seen_urls = {x.get("url") for x in existing if x.get("url")}
    seen_fp = {title_fingerprint(x.get("title", "")) for x in existing}
    added = 0
    for it in new_items:
        if it["id"] in seen_ids or it["url"] in seen_urls \
                or title_fingerprint(it["title"]) in seen_fp:
            continue
        existing.append(it)
        seen_ids.add(it["id"])
        seen_urls.add(it["url"])
        seen_fp.add(title_fingerprint(it["title"]))
        added += 1

    existing.sort(key=lambda x: (x.get("publish_date", ""), x.get("id", "")), reverse=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"[ok] 全球资讯分类后新增 {added} 条(分片累计 {len(existing)} 条) -> {out_path}")
    print(f"[ok] 各板块命中: {board_count}")
    print(f"GLOBAL_NEWS_ADDED={added}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
