#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T2T 学术日报邮件发送脚本
========================
挑选 literature.json 中 added_at 晚于 state/last_digest_date.txt 的全部条目
(含自动采集的正式论文 + 助手每日补录的微信/中文资讯)，生成 HTML 日报并通过 SMTP 发送。

所需环境变量 (在 GitHub 仓库 Settings -> Secrets and variables -> Actions 配置):
  SMTP_HOST   例: smtp.qq.com / smtp.163.com / smtp.126.com / smtp.office365.com
  SMTP_PORT   例: 465 (SSL) 或 587 (STARTTLS)，默认 465
  SMTP_USER   发件邮箱账号
  SMTP_PASS   邮箱授权码 (不是登录密码！QQ/163/126 需在邮箱设置里开启 SMTP 并生成授权码)
  MAIL_FROM   发件地址 (一般同 SMTP_USER)
  MAIL_TO     收件地址，多个用英文逗号分隔
可选:
  SKIP_IF_EMPTY=1   当天无新增时不发邮件 (默认仍发送“今日无新增”简报)

本地预览 (不发信):
  python3 scripts/send_digest.py --dry-run
"""
import argparse
import html
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

CN_TZ = timezone(timedelta(hours=8))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_PATH = os.path.join(ROOT, "literature.json")
STATE_DIR = os.path.join(ROOT, "state")
LAST_FILE = os.path.join(STATE_DIR, "last_digest_date.txt")
PREVIEW_FILE = os.path.join(STATE_DIR, "digest_preview.html")
ABSTRACT_CUT = 600


def today_cn():
    return datetime.now(CN_TZ).date()


def read_last_date():
    if os.path.exists(LAST_FILE):
        with open(LAST_FILE, "r", encoding="utf-8") as f:
            txt = f.read().strip()
            if txt:
                return datetime.strptime(txt, "%Y-%m-%d").date()
    # 首次运行：只发“今天”入库的，避免把历史存量一次性全发出
    return today_cn() - timedelta(days=1)


def write_last_date(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LAST_FILE, "w", encoding="utf-8") as f:
        f.write(d.isoformat())


def pick_new_items(since_date):
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        items = json.load(f)
    picked = [x for x in items if x.get("added_at", "") > since_date.isoformat()]
    papers = sorted([x for x in picked if x.get("type") == "paper"],
                   key=lambda x: x.get("publish_date", ""), reverse=True)
    wechats = sorted([x for x in picked if x.get("type") != "paper"],
                     key=lambda x: x.get("publish_date", ""), reverse=True)
    return papers, wechats


def render_html(today, papers, wechats):
    def block(title, items, color):
        if not items:
            return ""
        rows = []
        for it in items:
            abstract = it.get("abstract", "") or ""
            if len(abstract) > ABSTRACT_CUT:
                abstract = abstract[:ABSTRACT_CUT].rstrip() + "…"
            meta = " · ".join(x for x in [it.get("source", ""), it.get("publish_date", ""),
                                          (it.get("authors", "") or "")[:120]] if x)
            link = it.get("url", "") or "#"
            rows.append(f"""
            <div style="margin:0 0 16px;padding:14px 16px;border-left:4px solid {color};
                        background:#f8fafc;border-radius:6px;">
              <div style="font-size:15px;font-weight:600;margin-bottom:6px;">
                <a href="{html.escape(link)}" style="color:#1d4ed8;text-decoration:none;">
                  {html.escape(it.get('title','(无标题)'))}</a>
              </div>
              <div style="font-size:12px;color:#64748b;margin-bottom:6px;">{html.escape(meta)}</div>
              <div style="font-size:13px;color:#334155;line-height:1.6;">{html.escape(abstract) or '<i>无摘要</i>'}</div>
            </div>""")
        return (f'<h2 style="font-size:16px;color:{color};margin:20px 0 10px;">'
                f'{title}（{len(items)}）</h2>' + "".join(rows))

    empty_tip = ""
    if not papers and not wechats:
        empty_tip = ('<div style="padding:18px;background:#f1f5f9;border-radius:8px;'
                     'color:#475569;font-size:14px;">今日检索窗口内暂无新增 T2T 文献/资讯。</div>')
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><body style="margin:0;background:#eef2f7;padding:20px;">
  <div style="max-width:760px;margin:0 auto;background:#ffffff;border-radius:10px;
              padding:24px 28px;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;">
    <h1 style="font-size:20px;margin:0 0 4px;color:#0f172a;">T2T（端粒到端粒）基因组学术日报</h1>
    <div style="font-size:13px;color:#64748b;margin-bottom:8px;">{today.isoformat()} · 数据自动汇总，正式论文来自 Europe PMC / PubMed</div>
    {empty_tip}
    {block("📄 正式发表论文", papers, "#2563eb")}
    {block("💬 微信公众号 / 中文资讯", wechats, "#d97706")}
    <hr style="border:none;border-top:1px solid #e2e8f0;margin:22px 0 10px;">
    <div style="font-size:12px;color:#94a3b8;">本邮件由 GitHub Actions 自动发送；回复本邮件无效。</div>
  </div>
</body></html>"""


def render_text(today, papers, wechats):
    lines = [f"T2T 基因组学术日报 {today.isoformat()}", "=" * 40, ""]

    def sec(name, items):
        if not items:
            return
        lines.append(f"【{name}】{len(items)} 条")
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it.get('title','')}")
            lines.append(f"   来源: {it.get('source','')} | 发表: {it.get('publish_date','')}")
            if it.get("url"):
                lines.append(f"   链接: {it['url']}")
            ab = (it.get("abstract") or "").replace("\n", " ")
            if ab:
                lines.append(f"   摘要: {ab[:300]}")
            lines.append("")

    if not papers and not wechats:
        lines.append("今日检索窗口内暂无新增 T2T 文献/资讯。")
    sec("正式发表论文", papers)
    sec("微信公众号/中文资讯", wechats)
    return "\n".join(lines)


def send_mail(subject, html_body, text_body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    mail_from = os.environ.get("MAIL_FROM", user)
    rcpts = [x.strip() for x in os.environ["MAIL_TO"].split(",") if x.strip()]
    if not rcpts:
        raise RuntimeError("MAIL_TO 为空")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("T2T 学术日报", "utf-8")), mail_from))
    msg["To"] = ", ".join(rcpts)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.sendmail(mail_from, rcpts, msg.as_string())
    else:  # 587 等走 STARTTLS
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(user, password)
            s.sendmail(mail_from, rcpts, msg.as_string())
    return rcpts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只生成预览 HTML，不发送、不更新状态")
    args = ap.parse_args()

    today = today_cn()
    last = read_last_date()
    papers, wechats = pick_new_items(last)
    total = len(papers) + len(wechats)
    print(f"[info] 自 {last.isoformat()} 以来新增: 论文 {len(papers)} 条, 微信/资讯 {len(wechats)} 条")

    html_body = render_html(today, papers, wechats)
    text_body = render_text(today, papers, wechats)
    subject = f"T2T 基因组学术日报 {today.isoformat()}（新增 {total} 条）"

    os.makedirs(STATE_DIR, exist_ok=True)
    if args.dry_run:
        with open(PREVIEW_FILE, "w", encoding="utf-8") as f:
            f.write(html_body)
        print(f"[dry-run] 预览已生成: {PREVIEW_FILE}")
        print(text_body)
        return

    if total == 0 and os.environ.get("SKIP_IF_EMPTY") == "1":
        print("[ok] 今日无新增且 SKIP_IF_EMPTY=1，跳过发送")
        write_last_date(today)
        return

    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"[error] 缺少环境变量/Secrets: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    rcpts = send_mail(subject, html_body, text_body)
    write_last_date(today)  # 发送成功才推进日期游标，失败则下次重发
    print(f"[ok] 日报已发送（{len(rcpts)} 个收件人，地址已脱敏不打印）; 游标推进至 {today.isoformat()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
