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
    登録済みのカテゴリー（config.json の categories）のフォルダだけを取り込む。
    見慣れないフォルダ名が出てきたら、その場で登録するかどうかを尋ねる。

TXT の書き方:
    ファイル名に日付を入れる（2026-11-01.txt / 20261101.txt / 2026年11月1日.txt）。
    1行目を「# タイトル」にすると、その行がタイトルになる。
    「タグ: AI, 料理」の行を書くとタグが付く（省略可）。
    空行で段落が分かれ、段落内の改行はそのまま改行として表示される。

取り込まないもの（まちがいを防ぐため）:
    ファイル名に日付が入っていないもの（--更新日時を使う で取り込める）
    説明書・使い方・メモ・README などの名前のもの（--強制取り込み で取り込める）
    登録済みカテゴリー以外のフォルダに入っているもの（その場で登録を尋ねる）
    .txt 以外、大きすぎるもの、文字化けしているもの、本文が空のもの
    すでにある記事と本文が同じもの（--force で入れ直せる）
    公開済みとして記録されている記事（baseline.py --修正許可 が必要）
"""
import collections
import datetime
import hashlib
import html
import json
import os
import re
import shutil
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baseline
import common
import rules

common.use_utf8_stdout()

ENCODINGS = ("utf-8-sig", "utf-8", "cp932")
TAG_LINE = re.compile(r"^\s*(?:タグ|tags?)\s*[:：]\s*(.*)$", re.IGNORECASE)
# 「2026年10月30日」「2026-10-30」だけの行は、本文ではなくタイトルとみなす
DATE_ONLY = re.compile(r"^\s*\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?\s*$")
BOLD = re.compile(r"\*\*(.+?)\*\*")
TAG_SEP = re.compile(r"[,，、/／]+")
DONE_DIR_NAME = "_取り込み済み"

# ファイル名から日付を読む決まりは rules.py に置いてある（検査側と揃えるため）。
date_from_name = rules.date_from_name
DATE_PATTERNS = rules.DATE_PATTERNS


def read_text(path):
    """文字コードを順に試して読む。あわせて中身のハッシュも返す。

    どの文字コードでも読めなかった場合は replaced=True を返す。
    置換文字（文字化け）のまま記事にしてしまわないよう、呼び出し側で弾く。
    """
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc, digest, False
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(一部置換)", digest, True


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


def body_key(text):
    """本文の中身で重複を見分けるための指紋。

    改行の書き方・BOM・空白のちがいは同じものとして扱う。
    以前はバイト単位の一致しか見ていなかったため、末尾に改行を1つ足しただけで
    同じ日記が二重に増えてしまっていた。

    空白はすべて取り除く（1つにまとめるのではない）。これは、もとの TXT では
    改行だった場所が、記事では <br> になるため。空白1つにまとめる数えかたでは、
    同じ日記なのに TXT 側と記事側で指紋が変わってしまい、重複を見逃していた。
    check.py の本文照合と同じ数えかたに揃えている。
    """
    normalized = re.sub(r"\s+", "", (text or "").replace("﻿", ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def articles_dir(cfg):
    return os.path.normpath(os.path.join(
        common.ROOT, cfg.get("articles_dir") or "../10_記事管理"))


def legacy_dir(cfg):
    return os.path.normpath(os.path.join(
        common.ROOT, cfg.get("drafts_dir") or "../10_下書き"))


def ask(question):
    """publish.py と同じ作法で確認を求める。画面が使えないときは「いいえ」。"""
    if not sys.stdin.isatty():
        return False
    print("\n" + question)
    print('  続けるなら「はい」と入力して Enter（やめるなら、そのまま Enter）: ', end="")
    sys.stdout.flush()
    try:
        return sys.stdin.readline().strip() in ("はい", "ハイ", "yes", "y", "Y")
    except (EOFError, KeyboardInterrupt):
        return False


def skip(label, reason, hint=None):
    print("  [スキップ] %s" % label)
    print("             %s" % reason)
    if hint:
        for line in hint.split("\n"):
            print("             %s" % line)


def collect(args, cfg):
    """取り込む (TXTのパス, カテゴリー名) の一覧を作る。

    ファイルを直接指定された場合も、フォルダを順に見る場合と同じ決まりを当てる。
    以前は直接指定のときだけ、親フォルダ名を無検査でカテゴリーにしていたため、
    デスクトップの説明書をうっかり渡すと「デスクトップ」というカテゴリーが
    できてしまっていた。
    """
    explicit = [a for a in args if not a.startswith("--")]
    root = articles_dir(cfg)
    first_category = (rules.allowed_categories(cfg) or ["日記"])[0]

    if explicit:
        found = []
        for given in explicit:
            path = os.path.abspath(given.rstrip("/\\") or given)
            label = os.path.basename(path) or given
            if not os.path.exists(path):
                skip(label, "そのファイルが見つかりません: %s" % given)
                continue
            if os.path.isdir(path):
                skip(label, "フォルダが指定されています。TXT ファイルを指定してください。")
                continue
            if not path.lower().endswith(".txt"):
                skip(label, "TXT ファイルではありません（記事になるのは .txt だけです）。")
                continue
            if not rules.under(root, path):
                skip(label, "記事管理フォルダの外にあります。",
                     "記事管理フォルダ: %s\n"
                     "カテゴリーのフォルダ（例「%s」）へ移してから取り込んでください。"
                     % (root, first_category))
                continue
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            parts = relative.split("/")
            if len(parts) < 2:
                skip(label, "カテゴリーのフォルダに入っていません。",
                     "例: %s" % os.path.join(root, first_category, label))
                continue
            if any(p.startswith("_") for p in parts[:-1]):
                skip(label,
                     "「_」で始まるフォルダ（%s など）の中は取り込みません。" % DONE_DIR_NAME,
                     "ここは取り込んだときの控えを置く場所です。")
                continue
            if len(parts) > 2:
                skip(label, "カテゴリーのフォルダの、さらに下にあります: %s" % relative,
                     "カテゴリーのフォルダの直下に置いてください。")
                continue
            found.append((path, parts[0]))
        return found

    if not os.path.isdir(root):
        print("記事管理フォルダが見つかりません: %s" % root)
        return []

    found = []
    for folder in sorted(os.listdir(root)):
        full = os.path.join(root, folder)
        if not os.path.isdir(full) or folder.startswith("_"):
            continue
        names = sorted(os.listdir(full))
        for name in names:
            path = os.path.join(full, name)
            if name.lower().endswith(".txt") and os.path.isfile(path):
                found.append((path, folder))
        for sub in names:
            subdir = os.path.join(full, sub)
            if not os.path.isdir(subdir) or sub.startswith("_"):
                continue
            inner = [n for n in sorted(os.listdir(subdir)) if n.lower().endswith(".txt")]
            if inner:
                skip("%s / %s" % (folder, sub),
                     "カテゴリーのフォルダの、さらに下に TXT が %d 件あります。" % len(inner),
                     "カテゴリーのフォルダの直下に置いてください"
                     "（フォルダの中のフォルダは見ません）。")

    loose = [n for n in sorted(os.listdir(root))
             if n.lower().endswith(".txt") and os.path.isfile(os.path.join(root, n))]
    for name in loose:
        skip(name, "カテゴリーのフォルダに入っていません。",
             "例: %s" % os.path.join(root, first_category, name))
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


def register_category(cfg, name, pages):
    """config.json の categories に新しいカテゴリーを足す。

    URL に使えない名前は断る。足したあとの設定を書き戻す。
    """
    reserved = rules.reserved_top_level(p.get("slug") for p in pages)
    ok, why = rules.safe_url_name(name, reserved=reserved)
    if not ok:
        print("  [中止] カテゴリー「%s」は登録できません: %s" % (name, why))
        print("         フォルダの名前を変えてから、もう一度お試しください。")
        return False
    for warning in rules.url_name_warnings(name):
        print("  [注意] %s" % warning)

    path = os.path.join(common.ROOT, "config.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    listed = data.get("categories")
    if not isinstance(listed, list):
        listed = rules.allowed_categories(data)
    if name not in listed:
        listed.append(name)
        data["categories"] = listed
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("  [登録] config.json に「%s」を追加しました。" % name)
    cfg["categories"] = listed
    return True


def resolve_categories(files, cfg, pages, dry_run, allow_new):
    """見つかった TXT を、登録済みカテゴリーのものだけに絞る。

    見慣れないフォルダ名があれば、その場で登録するかを尋ねる。
    「やめる」を選ぶと、そのフォルダの TXT は取り込まない。
    """
    allowed = set(rules.allowed_categories(cfg))
    by_category = collections.OrderedDict()
    for path, category in files:
        by_category.setdefault(category, []).append(path)

    kept = []
    for category, paths in by_category.items():
        if category in allowed:
            kept.extend((p, category) for p in paths)
            continue

        shown = "、".join(os.path.basename(p) for p in paths[:3])
        if len(paths) > 3:
            shown += "、ほか %d 件" % (len(paths) - 3)
        print("\n  [確認] 「%s」は登録されていないカテゴリーです。" % category)
        print("         この回に見つかった TXT: %d件（%s）" % (len(paths), shown))
        print("         登録すると、記事は /posts/%s/<日付>/ に入り、" % category)
        print("         カテゴリーページ /categories/%s/ が作られます。" % category)
        print("         （既存の記事・URL には影響しません）")

        if dry_run:
            print("  [確認のみ] --dry-run なので登録しません。")
            continue
        if allow_new or ask("  カテゴリーとして登録しますか？"):
            if register_category(cfg, category, pages):
                allowed.add(category)
                kept.extend((p, category) for p in paths)
            continue
        print("  [スキップ] 「%s」の TXT %d 件は取り込みません。" % (category, len(paths)))
        print("             あとで登録するなら、次のどちらかです。")
        print("               ・config.json の categories に \"%s\" を足す" % category)
        print("               ・python tools/import_txt.py --新カテゴリー許可")
    return kept


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
    allow_new = any(a in ("--新カテゴリー許可", "--allow-new-category") for a in args)
    use_mtime = any(a in ("--更新日時を使う", "--use-file-time") for a in args)
    force_import = any(a in ("--強制取り込み", "--force-import") for a in args)
    cfg = common.load_config()
    default_cat = cfg.get("default_category") or "日記"
    cat_map = cfg.get("category_slug_map", {})

    print("=" * 56)
    print(" 記事管理フォルダの TXT を取り込みます")
    print("=" * 56)
    print("記事管理フォルダ: %s" % articles_dir(cfg))
    print("使えるカテゴリー: %s" % " / ".join(rules.allowed_categories(cfg)))
    print()

    notice_legacy_folder(cfg)

    existing = common.load_posts()
    pages = common.load_pages()
    taken = {p["slug"] for p in existing if p.get("slug")}
    by_hash = {p["source_sha256"]: p for p in existing if p.get("source_sha256")}
    # 本文の中身そのもので重複を見る。既存の記事すべてを相手にする。
    by_body = {}
    for post in existing:
        by_body.setdefault(body_key(common.strip_tags(post.get("_body", ""))), post)
    # 同じファイル名を同じカテゴリーに置き直した場合も、同じ記事として扱う
    by_source = {(tuple(p.get("categories") or [""])[0], p["source_file"]): p
                 for p in existing if p.get("source_file")}

    frozen = set((baseline.load_baseline() or {}).get("posts", {}))
    unlocked = baseline.load_unlocked()
    reserved_top = rules.reserved_top_level(p.get("slug") for p in pages)

    files = collect(args, cfg)
    if files:
        files = resolve_categories(files, cfg, pages, dry_run, allow_new)
    if not files:
        print("\n取り込む TXT はありませんでした。")
        print("カテゴリーのフォルダ（例「%s」）に、日付を入れた名前で TXT を置いてください。"
              % default_cat)
        return 0

    print("\n%d 件の TXT を取り込みます。%s\n"
          % (len(files), "（確認のみ）" if dry_run else ""))
    imported = skipped = 0
    already = []
    today = datetime.date.today()

    for path, category in files:
        name = os.path.basename(path)
        label = "%s / %s" % (category, name)

        size = os.path.getsize(path)
        if size > rules.MAX_TXT_BYTES:
            skip(label, "大きすぎます（%.1f MB）。日記ではないようです。"
                 % (size / 1024 / 1024))
            skipped += 1
            continue

        non_article = rules.non_article_name(name)
        if non_article and not force_import:
            skip(label, non_article,
                 "日記として取り込むなら --強制取り込み を付けてください。")
            skipped += 1
            continue

        text, enc, digest, replaced = read_text(path)
        if replaced:
            skip(label, "文字コードが読み取れず、文字化けしています（%s）。" % enc,
                 "メモ帳で開き、文字コード「UTF-8」で保存し直してください。")
            skipped += 1
            continue
        if "\x00" in text:
            skip(label, "文字ではないデータが入っています（画像や書類の可能性）。")
            skipped += 1
            continue
        if not text.strip():
            skip(label, "中身が空です。")
            skipped += 1
            continue

        seen = by_hash.get(digest)
        if seen and not force:
            already.append(label)
            skipped += 1
            continue

        title, tags, body_text = split_header(text)

        # 見出しとタグ行を取り除いたあとで空かどうかを見る。
        # 先に見ていたため「# タイトルだけ」の1行ファイルが
        # 本文のない記事として公開されてしまっていた。
        if not body_text.strip():
            skip(label, "見出しやタグの行しかなく、本文がありません。",
                 "1行目の次に、空行をはさんで本文を書いてください。")
            skipped += 1
            continue

        body_id = body_key(body_text)
        same_body = by_body.get(body_id)
        if same_body and not force:
            skip(label, "すでにある記事と本文が同じです（/posts/%s/）。"
                 % same_body.get("slug", "?"),
                 "入れ直すなら --force を付けてください。")
            skipped += 1
            continue

        if not seen:
            same_name = by_source.get((category, name))
            if same_name and not force:
                skip(label, "同じ名前で取り込み済みです（/posts/%s/ ／ 中身が違います）。"
                     % same_name["slug"],
                     "書き直したものに差し替えるなら --force を付けてください。")
                skipped += 1
                continue
            if same_name and force:
                seen = same_name

        day = date_from_name(name) or date_from_name(title or "")
        if day is None:
            if not use_mtime:
                skip(label, "ファイル名に日付が入っていません。",
                     "例: %s.txt / %s.txt / %d年%d月%d日.txt\n"
                     "ファイルの更新日時を使うなら --更新日時を使う を付けてください。"
                     % (today.isoformat(), today.strftime("%Y%m%d"),
                        today.year, today.month, today.day))
                skipped += 1
                continue
            day = datetime.date.fromtimestamp(os.path.getmtime(path))
            print("  [注意] %s から日付を読み取れないため、更新日時 %s を使います。"
                  % (label, day.isoformat()))

        out_of_range = rules.date_in_range(day, today)
        if out_of_range and not force_import:
            skip(label, "%s（%s）" % (out_of_range, day.isoformat()),
                 "この日付で取り込むなら --強制取り込み を付けてください。")
            skipped += 1
            continue

        if not title:
            title = "%d年%d月%d日" % (day.year, day.month, day.day)

        added_suffix = None
        if seen and force:
            slug = seen["slug"]
            out_path = seen["_path"]
        else:
            base = day.isoformat()
            if category != default_cat:
                cat_url = common.url_name(category, cat_map)
                ok, why = rules.safe_url_name(cat_url, reserved=reserved_top)
                if not ok:
                    skip(label, "カテゴリー「%s」は URL に使えません: %s" % (category, why),
                         "フォルダの名前を変えてください。")
                    skipped += 1
                    continue
                base = "%s/%s" % (cat_url, base)
            slug = unique_slug(base, taken)
            if slug != base:
                added_suffix = base
            ok, why = rules.safe_slug(slug, reserved=())
            if not ok:
                skip(label, "記事の URL を作れません: %s" % why)
                skipped += 1
                continue
            out_path = os.path.join(common.CONTENT_POSTS, slug + ".html")

        # 書き込む先が content/posts の中に収まっているか、必ず確かめる。
        if not rules.under(common.CONTENT_POSTS, os.path.dirname(out_path)):
            skip(label, "記事の置き場所がフォルダの外になります: %s" % out_path,
                 "カテゴリーのフォルダ名を見直してください。")
            skipped += 1
            continue

        # 公開済みとして記録されている記事は、どんな経路でも上書きしない。
        # 修正したいときは、人が baseline.py --修正許可 を実行する必要がある。
        if slug in frozen and slug not in unlocked:
            skip(label, "/posts/%s/ は公開済みとして記録されている記事です。" % slug,
                 "過去記事を直すときは、先に次を実行してください。\n"
                 "  python tools/baseline.py --修正許可 %s" % slug)
            skipped += 1
            continue

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
        if added_suffix:
            print("             [注意] /posts/%s/ はすでにあるため、別の記事として足します。"
                  % added_suffix)
            print("                    同じ日に何本でも書ける決まりです。"
                  "既存の記事を書き直したいときは、先に")
            print("                    python tools/baseline.py --修正許可 %s"
                  % added_suffix)
            print("                    を実行してから --force を付けてください。")
        if chars < rules.SHORT_BODY_CHARS:
            print("             [注意] 本文が %d 字しかありません。" % chars)

        # 確認のみのときも、同じ実行の中での重なりを正しく見せるため控えておく
        taken.add(slug)
        by_hash[digest] = {"slug": slug, "_path": out_path}
        by_source[(category, name)] = {"slug": slug, "_path": out_path}
        by_body[body_id] = {"slug": slug, "_path": out_path}

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
