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
    日付を入れない自由な名前（ローカルテスト.txt など）でもよい。
    そのときは、そのファイルを名指しで渡したときだけ取り込み、
    取り込んだ日（日本時間）の日記になる。
    1行目を「# タイトル」にすると、その行がタイトルになる。
    見出しが無く、名前に日付も無いときは、ファイル名がそのまま題になる。
    「タグ: AI, 料理」の行を書くとタグが付く（省略可）。
    空行で段落が分かれ、段落内の改行はそのまま改行として表示される。

取り込まないもの（まちがいを防ぐため）:
    まとめて見たときに、ファイル名に日付が入っていないもの
    （そのファイルを名指しで渡すか、--更新日時を使う を付ける）
    説明書・使い方・メモ・README などの名前のもの（--強制取り込み で取り込める。
    その語がカテゴリー名にも入っているときは、名前を疑わない）
    登録済みカテゴリー以外のフォルダに入っているもの（その場で登録を尋ねる）
    .txt 以外、大きすぎるもの、文字化けしているもの、本文が空のもの
    すでにある記事と本文が同じもの（--force で入れ直せる）
    公開済みとして記録されている記事（baseline.py --修正許可 が必要）
"""
import collections
import datetime
import json
import os
import shutil
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import ingest
import rules

common.use_utf8_stdout()

DONE_DIR_NAME = "_取り込み済み"

# 画面の案内で名前を出す BAT（プロジェクトの根にある）。
IMPORT_BAT = "1_日記を取り込む.bat"

# ファイル名から日付を読む決まりは rules.py に置いてある（検査側と揃えるため）。
date_from_name = rules.date_from_name
DATE_PATTERNS = rules.DATE_PATTERNS

# 題とタグの読み取り、本文の HTML 化、本文の指紋、URL の重なり避けは
# tools/ingest.py に移した。スマホからの投稿でも、まったく同じ処理を通す。
# ここでは、これまでと同じ名前でも呼べるようにしておく（出方は変わらない）。
TAG_LINE = ingest.TAG_LINE
DATE_ONLY = ingest.DATE_ONLY
BOLD = ingest.BOLD
TAG_SEP = ingest.TAG_SEP
split_header = ingest.split_header
to_html = ingest.to_html
body_key = ingest.body_key
unique_slug = ingest.unique_slug
read_text = ingest.read_text
ENCODINGS = ingest.ENCODINGS


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


def expected_in_category(name, category):
    """名前の「記事らしくない語」が、すべてカテゴリー名にも入っているか。

    「テスト」というカテゴリーを作った人が「ローカルテスト.txt」を置くのは
    まちがいではない。その置き場所では「テスト」という語に意味が無いので、
    名前を疑う根拠から外す。

    「使い方のテスト.txt」のように、カテゴリー名に無い語（使い方）も
    入っていれば、これまでどおり見送る。
    編集ソフトの控えの名前（~$ や「- コピー」）は、語ではなく形で分かる。
    カテゴリー名とは関係がないので、ここでは見逃さない。
    """
    base = os.path.basename(name or "")
    stem = os.path.splitext(base)[0]
    if rules.TEMP_NAME.search(stem) or rules.TEMP_NAME.search(base):
        return False
    words = rules.non_article_words(name)
    low = (category or "").lower()
    return bool(words) and all(w.lower() in low for w in words)


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


def skip_hint(plan, today):
    """取り込めなかった理由ごとに、TXT を直すための案内を返す。

    何を直せばよいかは入口ごとに違う（TXT・inbox・投稿フォーム）。
    判定そのものは ingest.py に1か所でまとめ、案内だけをここに置く。
    """
    hints = {
        "empty-body": "1行目の次に、空行をはさんで本文を書いてください。",
        "too-long": "日記として取り込むなら --強制取り込み を付けてください。",
        "same-body": "入れ直すなら --force を付けてください。",
        "same-source": "書き直したものに差し替えるなら --force を付けてください。",
        "no-date": "日付を入れた名前にする（例: %s.txt / %s.txt / %d年%d月%d日.txt）か、\n"
                   "この TXT だけを %s にドラッグ＆ドロップしてください\n"
                   "（そのときは、きょうの日付の日記になります）。\n"
                   "ファイルの更新日時を使うなら --更新日時を使う を付けてください。"
                   % (today.isoformat(), today.strftime("%Y%m%d"),
                      today.year, today.month, today.day, IMPORT_BAT),
        "date-out-of-range": "この日付で取り込むなら --強制取り込み を付けてください。",
        "unknown-category": "config.json の categories に足すか、"
                            "--新カテゴリー許可 を付けてください。",
        "bad-tag": "TXT の「タグ:」の行を直してください。",
        "bad-category-url": "フォルダの名前を変えてください。",
        "outside": "カテゴリーのフォルダ名を見直してください。",
        "frozen": "過去記事を直すときは、先に次を実行してください。\n"
                  "  python tools/baseline.py --修正許可 %s" % (plan.slug or ""),
    }
    return hints.get(plan.code)


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
    # 名指しで渡された TXT があるか。BAT へのドラッグ＆ドロップもここに入る。
    # 「本人がこのファイルを選んだ」ことの印として、日付の決め方に使う。
    chosen = bool([a for a in args if not a.startswith("--")])
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

    catalog = ingest.Catalog(cfg)
    pages = catalog.pages

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
    # 「きょう」は日本時間で数える（ingest.local_today と同じ決まり）。
    # パソコンの時計が別の国に合っていても、日記の日付がずれない。
    # ファイルの作成日・更新日は使わない（--更新日時を使う を付けたときだけ）。
    today = ingest.local_today(cfg)

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
        if non_article and not force_import \
                and not expected_in_category(name, category):
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

        title, tags, body_text = split_header(text)

        # 見出しが無いときの題。日付を入れていない名前なら、その名前を題にする。
        # 「ローカルテスト.txt」が「2026年11月3日」という題になってしまうと、
        # せっかく名前を付けた意味がない。
        # 日付を入れた名前（2026-11-01.txt など）のときは、ここでは何もしない。
        # これまでどおり ingest.py が「2026年11月1日」の形の題を付ける。
        if not title and date_from_name(name) is None:
            title = os.path.splitext(name)[0].strip() or None

        def resolve_day(_path=path, _label=label, _name=name, _title=title):
            """日付を決める。二重投稿の見分けが済んだあとに呼ばれる。

            取り込み済みのものは、日付が読めなくても黙って見送る。
            この順番を保つため、日付を決めるのはここまで待つ。

            日付の決め方は、上から順に次のとおり。
              1. ファイル名の日付（2026-11-01.txt など）
              2. 本文1行目の日付（「# 2026年11月1日」など）
              3. --更新日時を使う を付けたときだけ、ファイルの更新日時
              4. 名指しで渡された TXT なら、取り込んだ日（日本時間）

            4 は、まとめて見るときには使わない。カテゴリーのフォルダに
            置いたままの TXT が、置いた覚えのない日付で記事になってしまう
            のを防ぐため。日付を必須にしていたのは、もともとこの事故を
            防ぐためなので、「本人がこのファイルを選んだ」ときだけに
            限って受け入れる。この決まりは selftest.py の test_32 が見張る。
            """
            day = date_from_name(_name) or date_from_name(_title or "")
            if day is not None:
                return day, None
            if use_mtime:
                day = datetime.date.fromtimestamp(os.path.getmtime(_path))
                print("  [注意] %s から日付を読み取れないため、更新日時 %s を使います。"
                      % (_label, day.isoformat()))
                return day, None
            if not chosen:
                return None, "ファイル名に日付が入っていません。"
            print("  [注意] %s に日付が入っていないため、きょう（%s）の日記にします。"
                  % (_label, today.isoformat()))
            return today, None

        plan = catalog.plan(title=title, body_text=body_text, category=category,
                            tags=tags, source="txt", source_name=name,
                            digest=digest, force=force, force_import=force_import,
                            today=today, resolve_day=resolve_day)

        if plan.code == "already":
            already.append(label)
            skipped += 1
            continue
        if not plan.ok:
            skip(label, plan.reason, skip_hint(plan, today))
            skipped += 1
            continue

        print("  [%s] %s → /posts/%s/  「%s」 %d字%s"
              % ("取り込み直し" if plan.is_reimport else "取り込み", label, plan.slug,
                 plan.title, plan.chars,
                 "  タグ: " + "・".join(plan.tags) if plan.tags else ""))
        if plan.added_suffix:
            print("             [注意] /posts/%s/ はすでにあるため、別の記事として足します。"
                  % plan.added_suffix)
            print("                    同じ日に何本でも書ける決まりです。"
                  "既存の記事を書き直したいときは、先に")
            print("                    python tools/baseline.py --修正許可 %s"
                  % plan.added_suffix)
            print("                    を実行してから --force を付けてください。")
        if plan.chars < rules.SHORT_BODY_CHARS:
            print("             [注意] 本文が %d 字しかありません。" % plan.chars)

        # 確認のみのときも、同じ実行の中での重なりを正しく見せるため控えておく
        catalog.remember(plan)

        if dry_run:
            imported += 1
            continue

        backup = catalog.write(plan)
        if backup:
            print("             （元の記事は %s に控えました）" % os.path.basename(backup))

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
