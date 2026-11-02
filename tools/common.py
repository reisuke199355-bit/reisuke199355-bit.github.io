# -*- coding: utf-8 -*-
"""日記ブログ共通処理：フロントマター読み書き、設定読み込み、HTML補助。"""
import json
import os
import re
import sys
import urllib.parse
import html as htmllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTENT_POSTS = os.path.join(ROOT, "content", "posts")
CONTENT_PAGES = os.path.join(ROOT, "content", "pages")
TEMPLATES = os.path.join(ROOT, "templates")
DOCS = os.path.join(ROOT, "docs")

LIST_KEYS = ("categories", "tags")


def use_utf8_stdout():
    """画面に出す文字を UTF-8 にする（Windows の cp932 の画面でも化けないように）。

    何度呼んでも安全。以前は各ツールが sys.stdout を新しい TextIOWrapper に
    差し替えていたが、ツールを部品として読み込むと差し替えが重なり、
    先に作った受け皿が片づけられるときに出力そのものが閉じてしまっていた。
    reconfigure は受け皿を作り直さないので、この問題が起きない。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        current = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if current == "utf8":
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def parse_front_matter(text):
    """先頭の --- で囲まれたフロントマターを dict にして、本文と一緒に返す。

    「key: value」の形になっていない行は読み飛ばすが、黙って捨てると
    「title:A」のような書き間違いに気づけないため、_dropped に控えておく。
    （_ で始まるキーは書き出しのときに除かれるので、記事ファイルには入らない）
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta = {}
    dropped = []
    for line in head.split("\n"):
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ": " in line:
            key, value = line.split(": ", 1)
        elif line.endswith(":"):
            key, value = line[:-1], ""
        else:
            dropped.append(line.strip())
            continue
        key = key.strip()
        value = value.strip()
        if key in LIST_KEYS:
            value = value.strip("[]")
            meta[key] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            meta[key] = value
    if dropped:
        meta["_dropped"] = dropped
    return meta, body


def dump_front_matter(meta, body):
    lines = ["---"]
    for key, value in meta.items():
        if key.startswith("_"):
            continue            # 読み込みのときだけ使う覚え書き。ファイルには書かない
        if key in LIST_KEYS:
            lines.append("%s: [%s]" % (key, ", ".join(value)))
        else:
            lines.append("%s: %s" % (key, value))
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.rstrip() + "\n"


def read_entry(path):
    """記事ファイルを読む。文字コードが違うときは日本語で知らせて止まる。

    メモ帳などで「ANSI」のまま保存すると UTF-8 ではなくなる。
    そのままだと Python のトレースバックで終わってしまい、どのファイルを
    どう直せばよいのか分からないため、ここで案内を出す。
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        where = os.path.relpath(path, ROOT).replace(os.sep, "/")
        print("\n*** 中断します：記事ファイルの文字コードが UTF-8 ではありません ***")
        print("    ファイル: %s" % where)
        print("    メモ帳で開き、［名前を付けて保存］で文字コードに「UTF-8」を")
        print("    選んで保存し直してください。")
        raise SystemExit(1)
    meta, body = parse_front_matter(text)
    meta["_path"] = path
    meta["_body"] = body
    return meta


def write_entry(path, meta, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_front_matter(meta, body))


def load_posts():
    """content/posts の全記事を読み込んで、設定の並び順で返す。

    カテゴリー別のサブフォルダに置かれた記事も読む。
    日付が同じ記事の前後関係は URL（slug）で決める。そうしないと、
    記事ファイルの名前を変えただけで公開済みの「前の記事／次の記事」の
    リンクが入れ替わってしまう。
    """
    cfg = load_config()
    posts = []
    for folder, dirs, names in os.walk(CONTENT_POSTS):
        dirs.sort()
        for name in sorted(names):
            if name.endswith(".html"):
                posts.append(read_entry(os.path.join(folder, name)))
    key = (lambda p: (p.get("date", ""), p.get("slug", ""))) \
        if cfg.get("sort_by") == "post_date" \
        else (lambda p: ((p.get("diary_date") or p.get("date", ""))[:10], p.get("slug", "")))
    posts.sort(key=key)
    return posts


def load_pages():
    """content/pages の固定ページを名前順に読み込む。"""
    pages = []
    if not os.path.isdir(CONTENT_PAGES):
        return pages
    for name in sorted(os.listdir(CONTENT_PAGES)):
        if name.endswith(".html"):
            pages.append(read_entry(os.path.join(CONTENT_PAGES, name)))
    return pages


# 旧サイトのトップページ。記事ではないので転送ページを作らない。
LEGACY_HOME_NAMES = ("ホーム",)

# 旧サイトの「日記」一覧ページは、新サイトの全記事一覧へ向ける。
LEGACY_ARCHIVE_PAIR = ("日記", "archive/", "全記事一覧")


def legacy_pairs(posts, pages):
    """旧URL（パーセント復号済み）から新しい場所への対応を作る。

    build.py が転送ページを作るとき、baseline.py が記録するとき、
    check.py が点検するときで食い違わないよう、ここ1か所で決める。
    戻り値は (旧URL, 新しい相対URL, 題) の一覧。
    """
    pairs = []
    for post in posts:
        legacy = urllib.parse.unquote(post.get("legacy_slug", "") or "")
        if legacy:
            pairs.append((legacy, "posts/%s/" % post.get("slug", ""),
                          post.get("title", "")))
    for page in pages:
        legacy = urllib.parse.unquote(page.get("legacy_slug", "") or "")
        if legacy and legacy not in LEGACY_HOME_NAMES:
            pairs.append((legacy, "%s/" % page.get("slug", ""), page.get("title", "")))
    pairs.append(LEGACY_ARCHIVE_PAIR)
    return pairs


def url_name(name, mapping):
    """カテゴリー名・タグ名から、URL に使う名前を決める。

    設定にアルファベットの名前があればそれを使い、無ければ日本語のまま使う。
    日本語のフォルダ名は GitHub Pages でもそのまま使える（旧URLの転送ページで実証済み）。
    """
    name = (name or "").strip()
    if not name:
        return ""
    mapped = (mapping or {}).get(name)
    if mapped:
        return mapped.strip("/")
    # URL やファイル名に使えない文字だけは避ける
    for bad in r'\/:*?"<>|':
        name = name.replace(bad, "-")
    return name.strip(". ")


def quote_path(path):
    """リンクに書くときの文字列。日本語などはパーセントエンコードする。"""
    return "/".join(urllib.parse.quote(part) for part in (path or "").split("/"))


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
