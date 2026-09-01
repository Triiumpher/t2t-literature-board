# T2T & 昆虫功能基因组 · 多专题科研文献看板

一个纯静态、零后端、零数据库的**个人科研文献追踪系统**：GitHub Actions 每天从多个公开学术源自动检索 5 个研究方向的**已正式发表**论文，生成可筛选的网页看板，并通过你自己的 SMTP 邮箱把分专题学术日报发到指定邮箱；微信公众号 / 中文资讯由每日补录任务汇入。

- 在线看板：<https://triiumpher.github.io/t2t-literature-board/>
- 纯前端：`index.html` + `literature.json`，托管于 GitHub Pages
- 定时采集与发信：`.github/workflows/daily.yml`（GitHub Actions，免费额度内）

## 五个研究板块

| key | 板块名称 | 检索侧重 |
|---|---|---|
| `t2t` | T2T 端粒到端粒基因组 | telomere-to-telomere / gap-free / 无缺口组装 |
| `cmed` | 稻纵卷叶螟功能基因组学 | *Cnaphalocrocis medinalis* / rice leaffolder，基因组·转录组·基因功能（回溯窗口放宽到 45 天） |
| `sexdet` | 昆虫性别决定演化机制 | sex determination / doublesex / transformer / Wolbachia / 性染色体 |
| `ppi` | 蛋白互作预测 | PPI / interactome / complex，预测·深度学习·对接·结构（已剔除网络药理学/中药对接噪音） |
| `insecticide` | 新型杀虫剂 | 新化合物·作用靶标·机制·抗性·毒理（已剔除水体残留监测类环境文章） |

一篇文章可同时归入多个板块（`boards` 数组）。某板块当天无新增时，**邮件里该板块整段不出现**。

## 数据源

| 源 | 是否需 Key | 说明 |
|---|---|---|
| Europe PMC | 否 | 主源，覆盖 PubMed/MEDLINE，摘要较全，排除预印本 |
| PubMed (E-utilities) | 否 | NCBI，收录最快 |
| OpenAlex | 否 | 全学科开放库，排除预印本仓库 |
| **Elsevier Scopus** | **是 `ELSEVIER_API_KEY`** | 第四源；未配 key 时自动跳过、不影响其余三源 |
| 微信公众号 / 中文资讯 | — | 无合法公开 API，由每日补录任务检索后写入 `literature.json` |

> 每板块设“目标条数”（默认 5）：不足时自动把回溯窗口从 3 天逐步扩大到 7→15 天上限（`cmed` 为 45 天）以尽量凑够；若该方向真实发文不足，则以实际为准，不编造。

## 每条文献的字段

`id, title, type(paper/wechat), boards[], source, authors, publish_date, added_at, url, doi, pmid, abstract`，以及本次新增：

- `species_latin / species_en / species_zh`：研究物种**拉丁学名 / 英文名 / 中文名**（内置昆虫词典 + 双名法高置信识别，宁空勿错）
- `takeaway`：**一句话总结**
- `conclusion`：**主要结论**

> Actions runner 内不内置大模型，默认用规则从摘要抽取英文要点；若配置可选 LLM（见下），则由大模型改写为中文概括。

## 每日时间线（北京时间）

1. **06:35** 助手「每日多专题中文资讯补录」任务检索五个板块的微信/中文媒体，去重后提交 `literature.json`
2. **07:20** GitHub Actions 主触发（UTC `20 23 * * *`）：四源采集 → 分板块发邮件 → 提交数据
3. **08:47** 兜底触发（UTC `47 0 * * *`）：防止 GitHub 高负载漏跑；脚本当日幂等，不会重复发信

## 一次性配置（Repository Settings → Secrets and variables → Actions）

| Secret 名 | 必填 | 内容 |
|---|---|---|
| `SMTP_HOST` | 是 | 如 `smtp.126.com` |
| `SMTP_PORT` | 是 | `465`（SSL）或 `587`（STARTTLS） |
| `SMTP_USER` | 是 | 发信邮箱登录名 |
| `SMTP_PASS` | 是 | 邮箱 **SMTP 授权码**（不是登录密码） |
| `MAIL_FROM` | 是 | 发信邮箱地址 |
| `MAIL_TO` | 是 | 收件邮箱（多个用英文逗号分隔） |
| `ELSEVIER_API_KEY` | 启用 Scopus 必填 | dev.elsevier.com 申请的 API Key |
| `ELSEVIER_INSTTOKEN` | 否 | 机构 instToken，用于订阅级摘要/全文 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 否 | 兼容 OpenAI 协议的大模型（豆包/DeepSeek/OpenAI），把英文摘要**改写式**概括为中文一句话总结/结论，并自动回填历史英文条目，详见下文「配置大模型中文总结」 |

### Elsevier API Key 申请与填写
1. 登录 <https://dev.elsevier.com/> → `My API Key` 创建；Website URL 填本看板地址 `https://triiumpher.github.io/t2t-literature-board/` 即可（仅作应用标识，不校验归属）。
2. 复制 API Key，在上图 Secrets 页面新建 `ELSEVIER_API_KEY` 粘贴保存。**只放进 Secret，绝不写进代码或提交记录。**
3. 配置后在 Actions 页手动 `Run workflow` 一次，Fetch 步骤日志出现 `[scopus/板块] 原始返回 N 条` 即生效。

> 权限说明（重要）：
> - Scopus **Search API 对个人（非机构订阅）key 也开放**，可检索到题录元数据（标题/期刊/DOI/日期/作者），代码因此**不请求摘要字段**（非订阅 key 请求 `dc:description` 会直接报错导致整源 0 条，该问题已修复）。Scopus 独有的条目摘要可能为空，会由 Europe PMC/PubMed 跨源互补。
> - Elsevier 按调用方出口 IP 判断机构订阅；Actions 海外动态 IP 属 non-subscriber。若需要订阅级摘要/全文，向学校图书馆申请 instToken，填入可选 Secret `ELSEVIER_INSTTOKEN`。
> - **如何判断 key 是否可用**：看 Fetch 日志——`原始返回 N 条` 表示成功；若出现 `HTTP 401 ... APIKEY_INVALID` 是 key 填错、`403/ENTITLEMENT` 是无 Scopus 权限（需 insttoken）、`429` 是超配额（次日恢复）。错误体直接打印，不再被静默跳过。

### 配置大模型中文总结（豆包 / DeepSeek，可选但强烈建议）
不配置时，`takeaway/conclusion` 只是从英文摘要里**抽取的原句**；配置后会由大模型**用中文重新概括**（不是照抄），并自动把库里历史英文条目逐步回填为中文。三个 Secret：

| Secret | 豆包（火山方舟） | DeepSeek（海外 runner 更稳） |
|---|---|---|
| `LLM_API_KEY` | 方舟控制台的 API Key | <https://platform.deepseek.com> 的 API Key |
| `LLM_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | `https://api.deepseek.com` |
| `LLM_MODEL` | 接入点 ID（`ep-xxxx`）或模型 ID（如 `doubao-1.5-pro-32k`） | `deepseek-chat` |

- 每天最多处理条数由可选环境变量 `LLM_MAX_ITEMS` 控制（默认 60，新增优先、历史按日期回填，分批几天即可把旧库全部中文化）。
- 单条总结失败会自动回退规则结果，**绝不阻断采集与发信**。
- Actions 运行在海外：DeepSeek 端点连通性最好；豆包中国区端点一般可达，若日志出现 LLM 连接超时，换 DeepSeek 即可。

## 手动运行（本地调试）

```bash
pip install pyyaml                         # 仅校验 workflow 时需要, 采集/发信纯标准库
python3 scripts/fetch_literature.py --days 3   # 四源采集并入 literature.json
python3 scripts/send_digest.py --dry-run       # 生成 state/digest_preview.html 预览, 不发信
python3 -m http.server 8000                    # 浏览器打开 http://localhost:8000 查看看板
```

## 手动补录一条中文资讯

往 `literature.json` 数组里加一条对象即可（注意 `boards` 决定它进哪个板块/邮件段落）：

```json
{
  "id": "manual-20260901-shorttag",
  "title": "完整中文标题",
  "type": "wechat",
  "boards": ["t2t"],
  "source": "公众号名称",
  "authors": "",
  "publish_date": "2026-09-01",
  "added_at": "2026-09-01",
  "url": "https://mp.weixin.qq.com/s/xxxx",
  "doi": "", "pmid": "",
  "abstract": "1-3 句中文摘要",
  "species_latin": "", "species_en": "", "species_zh": "",
  "takeaway": "一句话总结",
  "conclusion": "主要结论",
  "keywords": ["T2T"]
}
```

## 新增 / 修改板块要改三处

1. `scripts/fetch_literature.py` 的 `BOARDS`（四源检索式与相关性规则）
2. `scripts/send_digest.py` 的 `BOARD_ORDER / BOARD_NAMES / BOARD_COLOR`
3. `index.html` 的 `BOARDS / BOARD_ORDER`

## 常见问题

- **没收到邮件？** 先看 Actions 运行记录；GitHub 的 schedule 在高负载时可能延迟甚至漏跑，本项目用双时间点 + 当日幂等缓解。
- **Scopus 没结果？** 看 Fetch 日志的 `[scopus]` 行：`原始返回 0 条`是当天确无命中；`HTTP 401/403/429` 分别对应 key 填错/无权限/超配额。个人 key 能取题录、摘要可能缺，靠其他源互补。
- **某板块条数不到 5？** 冷门方向真实发文有限（如稻纵卷叶螟 45 天仅数篇），以实际为准，不会凑数。
- **总结是英文原句、不是中文概括？** 没配 LLM 时是规则抽取；按上文「配置大模型中文总结」填入豆包或 DeepSeek 三个 Secret 后，即改为大模型中文改写，并自动回填历史英文条目。
