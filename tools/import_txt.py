# -*- coding: utf-8 -*-
"""記事管理フォルダに置いた TXT を記事として取り込む。

使い方:
    python import_txt.py              記事管理フォルダの TXT をすべて取り込む
    python import_txt.py --dry-run    取り込まず、何が起きるかだけ表示する
    python import_txt.py --force      同じ内容の TXT でも取り込み直す（上書き）
    python import_txt.py <file.txt>   ファイルを指定して取り込む

フォルダの決まり:
    10_記事管理 の直下のフォルダ名が、そのままカテゴリー名になる。
    「_」で始まるフォルダ（_取り込み済み など）は対象外。
    フォルダを足すだけで新しいカテゴリーとして扱われる。

TXT の書き方:
    ファイル名に日付を入れる（2026-11-01.txt / 20261101.txt / 2026年11月1日.txt）。
    1行目を「# タイトル」にすると、その行がタイトルになる。
    「タグ: AI, 料理」の行を書くとタグが付く（省略可）。
    空行で段落が分かれ、段落内の改行はそのまま改行として表示される。
"""
import collections
import datetime
import hashlib
import html
import io
import os
import re
import shutil
import stat
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
TAG_LINE = re.compile(r"^\s*(?:タグ|tags?)\s*[:：]\s*(.*)$", re.IGNORECASE)
# 「2026年10月30日」「2026-10-30」だけの行は、本文ではなくタイトルとみなす
DATE_ONLY = re.compile(r"^\s*\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?\s*$")
BOLD = re.compile(r"\*\*(.+?)\*\*")
TAG_SEP = re.compile(r"[,，、/／]+")
DONE_DIR_NAME = "_取り込み済み"


def read_text(path):
    """文字コードを順に試して読む。あわせて中身のハッシュも返す。"""
    raw = open(path, "rb").read()
    digest = hashlib.sha256(raw).hexdigest()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc, digest
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(一部置換)", digest


def date_from_name(name):
    for pattern in DATE_PATTERNS:
        m = pattern.search(name)
        if m:
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None


def split_header(text):
    """本文より前の「# タイトル」「タグ:」を取り出して、残りを本文として返す。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title, tags, i = None, [], 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            if title is None and not tags:
                break          # 先頭の空行は本文の一部として扱う
            i += 1
            break              # 見出しの終わり
        if title is None and line.startswith("#"):
            title = line.lstrip("#").strip()
            i += 1
            continue
        if title is None and DATE_ONLY.match(line):
            # 1行目が日付だけ。WordPress 時代の日記と同じ形なのでタイトルにする
            title = line
            i += 1
            continue
        m = TAG_LINE.match(line)
        if m:
            tags = [t.strip() for t in TAG_SEP.split(m.group(1)) if t.strip()]
            i += 1
            continue
        break                  # 見出しではない＝ここから本文
    return title, tags, "\n".join(lines[i:])


def to_html(text):
    """プレーンテキストを段落と改行の HTML にする。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", text) if b.strip()]
    out = []
    for block in blocks:
        lines = [BOLD.sub(r"<strong>\1</strong>", html.escape(line.strip()))
                 for line in block.split("\n") if line.strip()]
        if lines:
            out.append("<p>%s</p>" % "<br>".join(lines))
    return "\n\n".join(out) + "\n"


def articles_dir(cfg):
    return os.path.normpath(os.path.join(
        common.ROOT, cfg.get("articles_dir") or "../10_記事管理"))


def legacy_dir(cfg):
    return os.path.normpath(os.path.join(
        common.ROOT, cfg.get("drafts_dir") or "../10_下書き"))


def collect(args, cfg):
    """取り込む (TXTのパス, カテゴリー名) の一覧を作る。"""
    explicit = [a for a in args if not a.startswith("--")]
    root = articles_dir(cfg)
    if explicit:
        found = []
        for a in explicit:
            path = os.path.abspath(a)
            parent = os.path.basename(os.path.dirname(path))
            found.append((path, parent))
        return found

    if not os.path.isdir(root):
        print("記事管理フォルダが見つかりません: %s" % root)
        return []

    found = []
    for folder in sorted(os.listdir(root)):
        full = os.path.join(root, folder)
        if not os.path.isdir(full) or folder.startswith("_"):
            continue
        for name in sorted(os.listdir(full)):
            path = os.path.join(full, name)
            if name.lower().endswith(".txt") and os.path.isfile(path):
                found.append((path, folder))

    loose = [n for n in sorted(os.listdir(root))
             if n.lower().endswith(".txt") and os.path.isfile(os.path.join(root, n))]
    for name in loose:
        print("  [スキップ] %s はカテゴリーのフォルダに入っていません。" % name)
        print("             例: %s" % os.path.join(root, "日記", name))
    return found


def notice_legacy_folder(cfg):
    """旧「10_下書き」に TXT が残っていたら知らせる（勝手には動かさない）。"""
    old = legacy_dir(cfg)
    if not os.path.isdir(old):
        return
    left = [n for n in sorted(os.listdir(old))
            if n.lower().endswith(".txt") and os.path.isfile(os.path.join(old, n))]
    if not left:
        return
    print("[お知らせ] 旧フォルダ「%s」に TXT が %d 件あります。" % (old, len(left)))
    for n in left[:5]:
        print("           %s" % n)
    if len(left) > 5:
        print("           ... ほか %d 件" % (len(left) - 5))
    print("           こちらは取り込みません。カテゴリーのフォルダへ移してください。\n")


def unique_slug(base, taken):
    slug = base
    n = 2
    while slug in taken:
        slug = "%s-%d" % (base, n)
        n += 1
    return slug


def protect(path):
    """取り込み済みの原本を読み取り専用にする。"""
    try:
        os.chmod(path, stat.S_IREAD)
    except OSError:
        pass


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args
    cfg = common.load_config()
    default_cat = cfg.get("default_category") or "日記"
    cat_map = cfg.get("category_slug_map", {})

    print("=" * 56)
    print(" 記事管理フォルダの TXT を取り込みます")
    print("=" * 56)
    print("記事管理フォルダ: %s" % articles_dir(cfg))
    print()

    notice_legacy_folder(cfg)

    existing = common.load_posts()
    taken = {p["slug"] for p in existing}
    by_hash = {p["source_sha256"]: p for p in existing if p.get("source_sha256")}
    # 同じファイル名を同じカテゴリーに置き直した場合も、同じ記事として扱う
    by_source = {(tuple(p.get("categories") or [""])[0], p["source_file"]): p
                 for p in existing if p.get("source_file")}

    files = collect(args, cfg)
    if not files:
        print("取り込む TXT はありませんでした。")
        print("カテゴリーのフォルダ（例「日記」）に、日付を入れた名前で TXT を置いてください。")
        return 0

    print("%d 件の TXT を見つけました。%s\n" % (len(files), "（確認のみ）" if dry_run else ""))
    imported = skipped = 0
    already = []

    for path, category in files:
        name = os.path.basename(path)
        label = "%s / %s" % (category, name)
        text, enc, digest = read_text(path)
        if not text.strip():
            print("  [スキップ] %s は中身が空です。" % label)
            skipped += 1
            continue

        seen = by_hash.get(digest)
        if seen and not force:
            already.append(label)
            skipped += 1
            continue

        if not seen:
            same_name = by_source.get((category, name))
            if same_name and not force:
                print("  [スキップ] %s は同じ名前で取り込み済みです（/posts/%s/ ／ 中身が違います）。"
                      % (label, same_name["slug"]))
                print("             書き直したものに差し替えるなら --force を付けてください。")
                skipped += 1
                continue
            if same_name and force:
                seen = same_name

        title, tags, body_text = split_header(text)
        day = date_from_name(name) or date_from_name(title or "")
        if day is None:
            day = datetime.date.fromtimestamp(os.path.getmtime(path))
            print("  [注意] %s から日付を読み取れないため、更新日時 %s を使います。"
                  % (label, day.isoformat()))
        if not title:
            title = "%d年%d月%d日" % (day.year, day.month, day.day)

        if seen and force:
            slug = seen["slug"]
            out_path = seen["_path"]
        else:
            base = day.isoformat()
            if category != default_cat:
                base = "%s/%s" % (common.url_name(category, cat_map), base)
            slug = unique_slug(base, taken)
            out_path = os.path.join(common.CONTENT_POSTS, slug + ".html")

        meta = collections.OrderedDict()
        meta["title"] = title
        meta["date"] = datetime.datetime.combine(
            day, datetime.time(21, 0)).strftime("%Y-%m-%d %H:%M:%S")
        meta["slug"] = slug
        meta["diary_date"] = day.isoformat()
        meta["categories"] = [category]
        meta["tags"] = tags
        meta["source"] = "txt"
        meta["source_file"] = name
        meta["source_sha256"] = digest

        body = to_html(body_text)
        chars = len(common.strip_tags(body))
        print("  [%s] %s → /posts/%s/  「%s」 %d字%s"
              % ("取り込み直し" if seen else "取り込み", label, slug, title, chars,
                 "  タグ: " + "・".join(tags) if tags else ""))

        # 確認のみのときも、同じ実行の中での重なりを正しく見せるため控えておく
        taken.add(slug)
        by_hash[digest] = {"slug": slug, "_path": out_path}
        by_source[(category, name)] = {"slug": slug, "_path": out_path}

        if dry_run:
            imported += 1
            continue

        if os.path.exists(out_path):
            backup = out_path + ".bak"
            shutil.copy2(out_path, backup)
            print("             （元の記事は %s に控えました）" % os.path.basename(backup))

        common.write_entry(out_path, meta, body)

        # 原本は動かさない。取り込んだ時点の控えだけを残す
        done_dir = os.path.join(articles_dir(cfg), DONE_DIR_NAME, category)
        os.makedirs(done_dir, exist_ok=True)
        copied = os.path.join(done_dir, name)
        if os.path.exists(copied):
            os.chmod(copied, stat.S_IWRITE)
        shutil.copy2(path, copied)
        protect(copied)
        imported += 1

    if already:
        print("  [そのまま] 取り込み済みの TXT が %d 件ありました（二重投稿はしません）。"
              % len(already))
        for label in already[:3]:
            print("             %s" % label)
        if len(already) > 3:
            print("             ... ほか %d 件" % (len(already) - 3))

    print("\n取り込み %d 件 / スキップ %d 件。" % (imported, skipped))
    if imported and not dry_run:
        print("元の TXT はそのまま残してあります（動かしていません）。")
        print("取り込んだ時点の控えを「%s」に読み取り専用で保存しました。"
              % os.path.join(articles_dir(cfg), DONE_DIR_NAME))
        print("続けて build.py を実行すると、サイトに反映されます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
