#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分专题科研文献每日邮件摘要 (SMTP)
================================
读取 literature.json, 选出“自上次发送以来新增”的文献, 按 5 个板块分组,
生成 HTML + 纯文本邮件并通过 SMTP 发送。

设计要点:
- 按板块分组; 某板块当天无新增则该板块整段不出现(不打扰)
- 每条展示: 标题链接 / 研究物种(中文·英文·拉丁) / 一句话总结 / 主要结论 / 元信息 / 检索源
- state/last_digest_date.txt 记录上次发送日期, 只发之后新增
- 当日幂等: 同一天重复运行(双 schedule 兜底)不会重复发送
- --dry-run 只生成预览文件 state/digest_preview.html 与文本到 stdout, 不发信

环境变量(由 GitHub Actions 注入):
  SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_FROM/MAIL_TO  必填
  SKIP_IF_EMPTY=1   当天无新增时跳过不发(默认发一封“今日无新增”)
"""
import argparse
import html
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

CN_TZ = timezone(timedelta(hours=8))
STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "state")
STATE_FILE = os.path.join(STATE_DIR, "last_digest_date.txt")
PREVIEW_FILE = os.path.join(STATE_DIR, "digest_preview.html")

# 板块展示顺序 / 名称 / 主题色(与 fetch_literature.py、index.html 保持一致)
BOARD_ORDER = ["t2t", "cmed", "sexdet", "ppi", "insecticide"]
BOARD_NAMES = {
    "t2t": "T2T 端粒到端粒基因组",
    "cmed": "稻纵卷叶螟功能基因组学",
    "sexdet": "昆虫性别决定演化机制",
    "ppi": "蛋白互作预测",
    "insecticide": "新型杀虫剂",
}
BOARD_COLOR = {
    "t2t": "#2563eb",
    "cmed": "#059669",
    "sexdet": "#7c3aed",
    "ppi": "#0891b2",
    "insecticide": "#d97706",
}


def today_cn():
    return datetime.now(CN_TZ).date()


def load_items(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_last_date():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def write_last_date(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(d)


def pick_new_by_board(items, last_date, today):
    """选出 added_at 晚于游标(且不晚于今天)的条目, 按板块分组。
    一篇可属多个板块, 会在相应板块各出现一次。"""
    groups = {k: [] for k in BOARD_ORDER}
    for it in items:
        added = (it.get("added_at") or "")[:10]
        if not added:
            continue
        if last_date and added <= last_date:
            continue
        if added > today:
            continue
        for b in it.get("boards", ["t2t"]):
            if b in groups:
                groups[b].append(it)
    for k in groups:
        groups[k].sort(key=lambda x: (x.get("publish_date", ""), x.get("added_at", "")),
                       reverse=True)
    return groups


def species_line(it):
    zh, en, lat = it.get("species_zh", ""), it.get("species_en", ""), it.get("species_latin", "")
    parts = [p for p in (zh, en, (f"<i>{lat}</i>" if lat else "")) if p]
    return " · ".join(parts) if parts else ""


def esc(s):
    return html.escape(str(s or ""))


def render_text(groups, today):
    lines = [f"科研学术日报 {today}", "=" * 60]
    total = sum(len(v) for v in groups.values())
    lines.append(f"今日共新增 {total} 篇已发表文献 / 资讯，按研究专题分组如下。")
    for b in BOARD_ORDER:
        its = groups[b]
        if not its:
            continue
        lines.append("")
        lines.append(f"■ {BOARD_NAMES[b]}（{len(its)} 条）")
        lines.append("-" * 50)
        for i, it in enumerate(its, 1):
            lines.append(f"[{i}] {it.get('title','')}")
            meta = " / ".join(p for p in [it.get("source", ""), it.get("publish_date", ""),
                                          ("来源:" + it.get("via", "")) if it.get("via") else ""] if p)
            if meta:
                lines.append(f"    期刊/来源: {meta}")
            sp = " · ".join(p for p in (it.get("species_zh", ""), it.get("species_en", ""),
                                        it.get("species_latin", "")) if p)
            if sp:
                lines.append(f"    研究物种: {sp}")
            if it.get("takeaway"):
                lines.append(f"    一句话总结: {it['takeaway']}")
            if it.get("conclusion"):
                lines.append(f"    主要结论: {it['conclusion']}")
            if it.get("url"):
                lines.append(f"    链接: {it['url']}")
            lines.append("")
    return "\n".join(lines)


def render_html(groups, today):
    total = sum(len(v) for v in groups.values())
    parts = ["""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>科研学术日报</title></head><body style="margin:0;background:#f4f5f7;">
<div style="max-width:820px;margin:0 auto;padding:16px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',Arial,sans-serif;color:#1f2937;">
<h2 style="margin:8px 0;">🧬 T2T &amp; 昆虫功能基因组 · 多专题学术日报</h2>
<div style="color:#6b7280;font-size:13px;margin-bottom:14px;">DATE · 今日共新增 TOTAL 篇已发表文献/资讯，按专题分组（无更新的专题已自动省略）。</div>
""".replace("DATE", today).replace("TOTAL", str(total))]
    for b in BOARD_ORDER:
        its = groups[b]
        if not its:
            continue
        color = BOARD_COLOR[b]
        parts.append(
            f'<div style="margin:18px 0 8px;font-weight:700;font-size:16px;color:#fff;background:{color};'
            f'padding:8px 12px;border-radius:8px;">■ {esc(BOARD_NAMES[b])}（{len(its)} 条）</div>')
        for it in its:
            title = esc(it.get("title", ""))
            url = it.get("url", "")
            title_html = f'<a href="{esc(url)}" style="color:{color};text-decoration:none;font-weight:600;font-size:15px;">{title}</a>' if url else f'<span style="font-weight:600;font-size:15px;">{title}</span>'
            meta = " · ".join(p for p in [esc(it.get("source", "")), esc(it.get("publish_date", "")),
                                          ("检索源: " + esc(it.get("via", ""))) if it.get("via") else ""] if p)
            sp = species_line(it)
            chip = "论文" if it.get("type") == "paper" else "资讯"
            chip_bg = "#e0ecff" if it.get("type") == "paper" else "#e6f7ee"
            chip_fg = "#1d4ed8" if it.get("type") == "paper" else "#047857"
            parts.append(f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;margin:8px 0;">')
            parts.append(f'<div style="margin-bottom:6px;"><span style="background:{chip_bg};color:{chip_fg};font-size:11px;padding:2px 7px;border-radius:10px;margin-right:8px;">{chip}</span>{title_html}</div>')
            if meta:
                parts.append(f'<div style="color:#6b7280;font-size:12px;margin:4px 0;">{meta}</div>')
            if sp:
                parts.append(f'<div style="font-size:13px;margin:6px 0;">🧬 研究物种：{sp}</div>')
            if it.get("takeaway"):
                parts.append(f'<div style="background:#eff6ff;border-left:3px solid {color};padding:7px 10px;border-radius:4px;font-size:13px;margin:6px 0;">📌 一句话总结：{esc(it.get("takeaway"))}</div>')
            if it.get("conclusion"):
                parts.append(f'<div style="background:#f9fafb;border-left:3px solid #9ca3af;padding:7px 10px;border-radius:4px;font-size:13px;margin:6px 0;color:#374151;">🔍 主要结论：{esc(it.get("conclusion"))}</div>')
            parts.append("</div>")
    parts.append("<div style='color:#9ca3af;font-size:12px;margin-top:18px;'>本邮件由 GitHub Actions 自动生成；正式论文检索自 Europe PMC / PubMed / OpenAlex / Scopus，中文资讯由每日补录任务维护。</div></div></body></html>")
    return "\n".join(parts)


def send_mail(subject, text_body, html_body):
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    mail_from = os.environ.get("MAIL_FROM", user).strip()
    mail_to = os.environ.get("MAIL_TO", "").strip()
    if not all([host, user, password, mail_from, mail_to]):
        raise RuntimeError("SMTP 环境变量未配置完整 (SMTP_HOST/USER/PASS/MAIL_FROM/MAIL_TO)")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr([str(Header("科研文献日报", "utf-8")), mail_from])
    msg["To"] = mail_to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(user, password)
            server.sendmail(mail_from, [a.strip() for a in mail_to.split(",") if a.strip()], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(user, password)
            server.sendmail(mail_from, [a.strip() for a in mail_to.split(",") if a.strip()], msg.as_string())


def main():
    ap = argparse.ArgumentParser(description="按板块生成并发送每日文献邮件")
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "literature.json"))
    ap.add_argument("--dry-run", action="store_true", help="只生成预览, 不实际发信")
    args = ap.parse_args()

    today = today_cn().isoformat()
    last = read_last_date()

    # 当日硬幂等: 游标已到今天说明主触发已发过, 兜底触发直接退出
    if last >= today and not args.dry_run:
        print(f"[skip] 今日({today})日报已发送过(last={last}), 本次兜底触发不重复发送")
        return

    items = load_items(os.path.abspath(args.data))
    groups = pick_new_by_board(items, last, today)
    total = sum(len(v) for v in groups.values())
    print("[info] 本次各板块新增:", {BOARD_NAMES[k]: len(v) for k, v in groups.items()})

    if total == 0 and os.environ.get("SKIP_IF_EMPTY", "").strip() == "1" and not args.dry_run:
        print("[skip] SKIP_IF_EMPTY=1 且今日无新增, 不发送邮件, 但推进游标避免重复扫描")
        write_last_date(today)
        return

    subject = f"科研学术日报 {today}（共新增 {total} 条）"
    text_body = render_text(groups, today)
    html_body = render_html(groups, today)

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PREVIEW_FILE, "w", encoding="utf-8") as f:
        f.write(html_body)

    if args.dry_run:
        print("[dry-run] 不发送邮件。HTML 预览:", os.path.abspath(PREVIEW_FILE))
        print("-" * 70)
        print(text_body)
        return

    send_mail(subject, text_body, html_body)
    write_last_date(today)
    print(f"[ok] 邮件已发送, 共 {total} 条; 游标更新为 {today}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
