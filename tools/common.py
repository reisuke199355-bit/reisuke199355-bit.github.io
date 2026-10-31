# -*- coding: utf-8 -*-
"""日記ブログ共通処理：フロントマター読み書き、設定読み込み、HTML補助。"""
import json
import os
import re
import html as htmllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTENT_POSTS = os.path.join(ROOT, "content", "posts")
CONTENT_PAGES = os.path.join(ROOT, "content", "pages")
TEMPLATES = os.path.join(ROOT, "templates")
DOCS = os.path.join(ROOT, "docs")

LIST_KEYS = ("categories", "tags")


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def parse_front_matter(text):
    """先頭の --- で囲まれたフロントマターを dict にして、本文と一緒に返す。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta = {}
    for line in head.split("\n"):
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ": " in line:
            key, value = line.split(": ", 1)
        elif line.endswith(":"):
            key, value = line[:-1], ""
        else:
            continue
        key = key.strip()
        value = value.strip()
        if key in LIST_KEYS:
            value = value.strip("[]")
            meta[key] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            meta[key] = value
    return meta, body


def dump_front_matter(meta, body):
    lines = ["---"]
    for key, value in meta.items():
        if key in LIST_KEYS:
            lines.append("%s: [%s]" % (key, ", ".join(value)))
        else:
            lines.append("%s: %s" % (key, value))
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.rstrip() + "\n"


def read_entry(path):
    with open(path, encoding="utf-8") as f:
        meta, body = parse_front_matter(f.read())
    meta["_path"] = path
    meta["_body"] = body
    return meta


def write_entry(path, meta, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_front_matter(meta, body))


def load_posts():
    """content/posts の全記事を読み込んで、設定の並び順で返す。"""
    cfg = load_config()
    posts = []
    for name in sorted(os.listdir(CONTENT_POSTS)):
        if name.endswith(".html"):
            posts.append(read_entry(os.path.join(CONTENT_POSTS, name)))
    key = (lambda p: p.get("date", "")) if cfg.get("sort_by") == "post_date" \
        else (lambda p: (p.get("diary_date") or p.get("date", ""))[:10])
    posts.sort(key=key)
    return posts


def esc(s):
    return htmllib.escape(s or "", quote=True)


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


JP_DATE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")


def diary_date_from_title(title):
    """「2026年7月18日」のようなタイトルから 2026-07-18 を得る。該当しなければ None。"""
    m = JP_DATE.match((title or "").strip())
    if not m:
        return None
    return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))


WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def format_date_ja(iso_datetime):
    """2026-07-19 23:58:59 -> 2026年7月19日(日)"""
    import datetime
    s = (iso_datetime or "")[:10]
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        return s
    return "%d年%d月%d日(%s)" % (d.year, d.month, d.day, WEEKDAY_JA[d.weekday()])


def relpath_prefix(depth):
    """出力ファイルの階層の深さから ../ の繰り返しを作る。"""
    return "" if depth == 0 else "../" * depth
