# T2T（端粒到端粒）基因组文献日报看板

每天自动检索 T2T / Telomere-to-Telomere 基因组**正式发表论文**，汇集微信公众号/中文资讯，
在网页看板展示，并把"学术日报"通过邮件推送给你。

## 一、它每天怎么工作（时间线，北京时间）

| 时间 | 执行者 | 动作 |
|---|---|---|
| 07:35 | 豆包定时任务 | 检索微信生态/中文科研媒体的 T2T 文章，去重后追加到 `literature.json` |
| 08:20 | GitHub Actions | ① 从 Europe PMC/PubMed 拉取最近 3 天正式论文（已排除预印本）② 合并去重写回 `literature.json` ③ 把全部新增汇总成邮件日报发出 ④ 自动 commit 回仓库 |
| 全天 | GitHub Pages | 看板网页随时可看，数据随仓库自动更新 |

> 说明：
> - 正式论文走 **Europe PMC 免费公开 API**，无需任何 API Key，覆盖 PubMed/MEDLINE；Bing 学术 API 已停止服务，故不采用。
> - 微信公众号没有公开检索 API、`mp.weixin.qq.com` 原始链接也基本不被外网搜索引擎收录，因此微信部分由豆包每日检索"公众号镜像/中文科研媒体"补录；你自己看到好的公众号文章，也可按文末模板手动加一条。

## 二、目录结构

```
t2t-literature-board/
├── index.html                  # 文献看板网页（GitHub Pages 托管）
├── literature.json             # 全部文献数据（脚本自动更新 + 手动补录）
├── scripts/
│   ├── fetch_literature.py     # 每日采集正式论文（纯标准库，零依赖）
│   └── send_digest.py          # 生成并发送邮件日报（支持 --dry-run 预览）
├── state/                      # 运行状态（last_digest_date.txt 记录已发到哪天）
├── .github/workflows/daily.yml # GitHub Actions 每日工作流
└── README.md
```

## 三、部署步骤（约 10 分钟，全程免费）

### Step 1：新建 GitHub 公开仓库
GitHub 右上角 New repository，仓库名建议 `t2t-literature-board`，选 **Public**（免费账号开 Pages 要求公开），
不要勾选自动生成 README。把本目录所有文件推上去：

```bash
cd t2t-literature-board
git init
git add .
git commit -m "init: T2T literature board"
git branch -M main
git remote add origin https://github.com/<你的用户名>/t2t-literature-board.git
git push -u origin main
```

### Step 2：配置邮箱 Secrets（发日报用）
仓库页面 → **Settings → Secrets and variables → Actions → New repository secret**，逐个添加：

| Secret 名 | 值示例（QQ 邮箱） | 说明 |
|---|---|---|
| `SMTP_HOST` | `smtp.qq.com` | 163/126 邮箱填 `smtp.163.com` / `smtp.126.com`，Outlook 填 `smtp.office365.com` |
| `SMTP_PORT` | `465` | QQ/163/126 用 465(SSL)；Outlook 用 587 |
| `SMTP_USER` | `你的邮箱账号` | 发件邮箱账号 |
| `SMTP_PASS` | 邮箱**授权码** | 不是登录密码！邮箱设置→POP3/SMTP/IMAP→开启 SMTP 服务后生成授权码 |
| `MAIL_FROM` | `你的邮箱账号` | 一般与 SMTP_USER 相同 |
| `MAIL_TO` | `收信邮箱@xx.com` | 多个收件人用英文逗号分隔 |
| `SKIP_IF_EMPTY`（可选） | `1` | 填 1 则当天无新增时不发邮件；不填则每天都发（无新增会发简报） |

### Step 3：手动测试一次 Actions
仓库 **Actions** 标签页 → 左侧 `daily-t2t-digest` → 右侧 **Run workflow**。
等 1 分钟左右刷新，绿色对勾即成功；点进运行记录可看到拉到几篇论文、邮件发给了谁。
此时你的邮箱应收到第一封日报。

### Step 4：开启看板网页
仓库 **Settings → Pages**：Source 选 `Deploy from a branch`，Branch 选 `main` / 根目录 `/ (root)`，Save。
1~2 分钟后得到网址：`https://<你的用户名>.github.io/t2t-literature-board/`，全网公开（页面已带 noindex，不会被搜索引擎主动收录，但拿到链接的人可访问，**不要在 json 里放未发表敏感数据**）。

完成。之后每天 08:20（GitHub 定时可能有数分钟到数十分钟延迟，属正常现象）自动运行。

## 四、手动补录一篇微信文章

打开 `literature.json`，在数组最前面按下面格式加一条（注意逗号），commit 后看板与次日邮件都会带上：

```json
{
  "id": "manual-20260831-xxx",
  "title": "文章完整标题",
  "type": "wechat",
  "source": "公众号名称",
  "authors": "",
  "publish_date": "2026-08-31",
  "added_at": "2026-08-31",
  "url": "https://mp.weixin.qq.com/s/xxxx",
  "doi": "",
  "pmid": "",
  "abstract": "一两句话摘要，会显示在卡片和邮件里。",
  "keywords": ["T2T"]
}
```

## 五、本地运行 / 预览

```bash
# 拉取最近 14 天论文并合并
python3 scripts/fetch_literature.py --days 14

# 只生成邮件预览（state/digest_preview.html），不真正发信
python3 scripts/send_digest.py --dry-run

# 本地看板预览
python3 -m http.server 8000
# 浏览器打开 http://127.0.0.1:8000/
```

## 六、自定义

- **检索关键词/范围**：改 `scripts/fetch_literature.py` 顶部 `EPMC_QUERY`（例如限定昆虫：加 `AND (insect OR Lepidoptera OR ...)`）。
- **回溯天数**：Actions 里 `--days 3`，改大可容忍周末/服务波动。
- **发送时间**：改 `.github/workflows/daily.yml` 的 cron（UTC！北京时间减 8 小时；当前 `20 0 * * *` = 北京 08:20）。
- **不想公开看板**：GitHub Pages 免费版不支持私密访问；可不开 Pages，只保留邮件日报，仓库保持私有（Actions 仍可运行）。

## 七、常见问题

1. **Actions 没按时跑？** GitHub 免费账号的 schedule 在高峰期会延迟，属平台行为；`--days 3` 的冗余窗口可保证不漏文献。
2. **邮件发送失败报 535？** 基本都是授权码错误/未开启 SMTP 服务，重新生成授权码并更新 Secret。
3. **某篇论文不想看？** 直接从 `literature.json` 删除该条即可；想长期屏蔽某方向就收窄 `EPMC_QUERY`。
4. **预印本去哪了？** 脚本只保留 source 为 MED/PMC 的正式发表文献，bioRxiv 等预印本（PPR）按需求被过滤。
