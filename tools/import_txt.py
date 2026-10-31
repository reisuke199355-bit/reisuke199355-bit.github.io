# -*- coding: utf-8 -*-
"""下書きフォルダに置いた TXT の日記を記事として取り込む。

使い方:
    python import_txt.py              下書きフォルダの TXT をすべて取り込む
    python import_txt.py --dry-run    取り込まず、何が起きるかだけ表示する
    python import_txt.py --force      同じ日付の記事がすでにあっても上書きする
    python import_txt.py <file.txt>   ファイルを指定して取り込む

TXT の書き方:
    ファイル名に日付を入れる（2026-11-01.txt / 20261101.txt / 2026年11月1日.txt）。
    1行目を「# タイトル」にすると、その行がタイトルになる。
    省略した場合はファイル名の日付から「2026年11月1日」というタイトルを作る。
    空行で段落が分かれ、段落内の改行はそのまま改行として表示される。
"""
import collections
import datetime
import html
import io
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ENCODINGS = ("utf-8-sig", "utf-8", "cp932")
DATE_PATTERNS = (
    re.compile(r"(\d{4})[-_.](\d{1,2})[-_.](\d{1,2})"),
    re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日"),
    re.compile(r"(\d{4})(\d{2})(\d{2})"),
)


def read_text(path):
    """文字コードを順に試して読む。"""
    raw = open(path, "rb").read()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(一部置換)"


def date_from_name(name):
    for pattern in DATE_PATTERNS:
        m = pattern.search(name)
        if m:
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None


def to_html(text):
    """プレーンテキストを段落と改行の HTML にする。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", text) if b.strip()]
    out = []
    for block in blocks:
        lines = [html.escape(line.strip()) for line in block.split("\n") if line.strip()]
        if lines:
            out.append("<p>%s</p>" % "<br>".join(lines))
    return "\n\n".join(out) + "\n"


def drafts_dir():
    cfg = common.load_config()
    rel = cfg.get("drafts_dir") or "../10_下書き"
    return os.path.normpath(os.path.join(common.ROOT, rel))


def collect(args):
    explicit = [a for a in args if not a.startswith("--")]
    if explicit:
        return [os.path.abspath(a) for a in explicit]
    folder = drafts_dir()
    if not os.path.isdir(folder):
        print("下書きフォルダが見つかりません: %s" % folder)
        return []
    return [os.path.join(folder, n) for n in sorted(os.listdir(folder))
            if n.lower().endswith(".txt") and os.path.isfile(os.path.join(folder, n))]


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args

    print("=" * 56)
    print(" 下書きフォルダの TXT 日記を取り込みます")
    print("=" * 56)
    print("下書きフォルダ: %s" % drafts_dir())
    print()

    files = collect(args)
    if not files:
        print("取り込む TXT はありませんでした。")
        print("日付を入れたファイル名（例 2026-11-01.txt）で TXT を置いてください。")
        return 0

    print("%d 件の TXT を見つけました。%s\n" % (len(files), "（確認のみ）" if dry_run else ""))
    done_dir = os.path.join(drafts_dir(), "取り込み済み")
    imported = skipped = 0

    for path in files:
        name = os.path.basename(path)
        text, enc = read_text(path)
        if not text.strip():
            print("  [スキップ] %s は中身が空です。" % name)
            skipped += 1
            continue

        lines = text.replace("\r\n", "\n").split("\n")
        title = None
        if lines and lines[0].strip().startswith("#"):
            title = lines[0].strip().lstrip("#").strip()
            text = "\n".join(lines[1:])

        day = date_from_name(name) or date_from_name(title or "")
        if day is None:
            day = datetime.date.fromtimestamp(os.path.getmtime(path))
            print("  [注意] %s から日付を読み取れないため、更新日時 %s を使います。"
                  % (name, day.isoformat()))

        if not title:
            title = "%d年%d月%d日" % (day.year, day.month, day.day)
        slug = common.diary_date_from_title(title) or day.isoformat()
        out_path = os.path.join(common.CONTENT_POSTS, slug + ".html")

        if os.path.exists(out_path) and not force:
            print("  [スキップ] %s → すでに記事 %s があります（上書きするなら --force）。"
                  % (name, slug))
            skipped += 1
            continue

        body = to_html(text)
        meta = collections.OrderedDict()
        meta["title"] = title
        meta["date"] = datetime.datetime.combine(
            day, datetime.time(21, 0)).strftime("%Y-%m-%d %H:%M:%S")
        meta["slug"] = slug
        meta["diary_date"] = day.isoformat()
        meta["categories"] = ["日記"]
        meta["tags"] = ["日記"]
        meta["source"] = "txt"
        meta["source_file"] = name

        chars = len(common.strip_tags(body))
        print("  [取り込み] %s（%s）→ /posts/%s/  「%s」 %d字"
              % (name, enc, slug, title, chars))

        if dry_run:
            imported += 1
            continue

        common.write_entry(out_path, meta, body)
        os.makedirs(done_dir, exist_ok=True)
        moved = os.path.join(done_dir, name)
        if os.path.exists(moved):
            stem, ext = os.path.splitext(name)
            moved = os.path.join(done_dir, "%s_%s%s"
                                 % (stem, datetime.datetime.now().strftime("%H%M%S"), ext))
        shutil.move(path, moved)
        imported += 1

    print("\n取り込み %d 件 / スキップ %d 件。" % (imported, skipped))
    if imported and not dry_run:
        print("元の TXT は「%s」へ移しました。" % done_dir)
        print("続けて build.py を実行すると、サイトに反映されます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
