# -*- coding: utf-8 -*-
"""inbox フォルダに置かれた TXT を記事として取り込む（スマホ・クラウドから）。

使い方:
    python import_inbox.py               inbox の TXT をすべて取り込む
    python import_inbox.py --dry-run     取り込まず、何が起きるかだけ表示する
    python import_inbox.py --残す        取り込んだ TXT を inbox に残す
    python import_inbox.py --json <先>   結果を JSON で書き出す（Actions が読む）

inbox の決まり:
    inbox/2026-11-02.txt            → カテゴリーは既定（config.json の default_category）
    inbox/感想/2026-11-02.txt       → フォルダ名がカテゴリーになる
    .txt 以外のファイルは見ない（README.md などは置いたままで大丈夫）。

TXT の書き方（10_記事管理 に置く TXT とまったく同じ）:
    ファイル名に日付を入れる（2026-11-02.txt / 20261102.txt / 2026年11月2日.txt）。
    1行目を「# タイトル」にすると、その行がタイトルになる。
    「タグ: AI, 料理」の行を書くとタグが付く（省略可）。
    空行で段落が分かれ、段落内の改行はそのまま改行として表示される。

取り込まないもの（まちがいを防ぐため）:
    ファイル名に日付が入っていないもの
    説明書・使い方・メモ・README などの名前のもの
    登録済みカテゴリー以外のフォルダに入っているもの（ここでは尋ねない）
    大きすぎるもの、文字化けしているもの、本文が空のもの
    すでにある記事と本文が同じもの（二重投稿の防止）
    公開済みとして記録されている記事（baseline.py --修正許可 が必要）

このツールは画面からの入力を求めない（GitHub Actions でも動くように）。
新しいカテゴリーを勝手に登録することはしない。
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import ingest
import rules

common.use_utf8_stdout()

INBOX = os.path.join(common.ROOT, "inbox")

# 取り込めなかったときの、直しかたの案内。
HINTS = {
    "empty-body": "1行目の次に、空行をはさんで本文を書いてください。",
    "too-long": "日記として大きすぎます。分けて投稿してください。",
    "same-body": "すでに公開（または取り込み）済みです。二重投稿はしません。",
    "same-source": "同じ名前で取り込み済みです。ファイル名を変えてください。",
    "same-ref": "同じ投稿から取り込み済みです。二重投稿はしません。",
    "no-date": "ファイル名に日付を入れてください（例 2026-11-02.txt）。",
    "date-out-of-range": "ファイル名の日付を直してください。",
    "unknown-category": "inbox のフォルダ名を、登録済みのカテゴリーに合わせてください。",
    "bad-tag": "「タグ:」の行を直してください。",
    "bad-category-url": "inbox のフォルダ名を変えてください。",
    "bad-slug": "ファイル名を日付だけの形にしてください。",
    "outside": "inbox のフォルダ名を見直してください。",
    "frozen": "過去記事を直すときは、手元で baseline.py --修正許可 を実行してください。",
}


def found_files():
    """inbox の中の TXT を (パス, カテゴリー, 見出し用の名前) で返す。

    1段だけ見る。それより深いところに置かれた TXT は見送る（理由を返す）。
    """
    items, deep = [], []
    if not os.path.isdir(INBOX):
        return items, deep
    for folder, dirs, names in os.walk(INBOX):
        dirs.sort()
        rel_folder = os.path.relpath(folder, INBOX).replace(os.sep, "/")
        parts = [] if rel_folder == "." else rel_folder.split("/")
        for name in sorted(names):
            if not name.lower().endswith(".txt"):
                continue
            path = os.path.join(folder, name)
            label = "/".join(parts + [name])
            if len(parts) > 1:
                deep.append(label)
                continue
            items.append((path, parts[0] if parts else None, label))
    return items, deep


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    keep = any(a in ("--残す", "--keep") for a in args)
    json_out = None
    if "--json" in args:
        i = args.index("--json")
        if i + 1 < len(args):
            json_out = args[i + 1]

    cfg = common.load_config()
    catalog = ingest.Catalog(cfg)
    today = ingest.local_today(cfg)

    print("=" * 56)
    print(" inbox の TXT を取り込みます")
    print("=" * 56)
    print("inbox: %s" % INBOX)
    print("使えるカテゴリー: %s" % " / ".join(catalog.allowed_categories()))
    print()

    items, deep = found_files()
    result = {"imported": [], "skipped": [], "dry_run": dry_run}

    for label in deep:
        print("  [スキップ] %s" % label)
        print("             inbox のフォルダの、さらに下にあります。")
        print("             inbox/<日付>.txt か inbox/<カテゴリー>/<日付>.txt に置いてください。")
        result["skipped"].append({"file": label, "code": "too-deep",
                                  "reason": "inbox のフォルダの、さらに下にあります。"})

    if not items:
        if not deep:
            print("取り込む TXT はありませんでした。")
            print("inbox に「2026-11-02.txt」のような名前で TXT を置いてください。")
        result["ok"] = not deep
        if json_out:
            write_json(json_out, result)
        return 0 if not deep else 1

    print("%d 件の TXT を取り込みます。%s\n"
          % (len(items), "（確認のみ）" if dry_run else ""))

    for path, folder, label in items:
        name = os.path.basename(path)
        category = folder or catalog.default_category

        skipped = handle(catalog, path, name, label, category, today,
                         dry_run, keep, result)
        if skipped:
            continue

    print("\n取り込み %d 件 / スキップ %d 件。"
          % (len(result["imported"]), len(result["skipped"])))
    if result["imported"] and not dry_run:
        print("続けて build.py を実行すると、サイトに反映されます。")
    result["ok"] = not result["skipped"]
    if json_out:
        write_json(json_out, result)
    return 0 if result["ok"] else 1


def handle(catalog, path, name, label, category, today, dry_run, keep, result):
    """1件ぶんを取り込む。見送ったときは True を返す。"""
    def skip(reason, code):
        print("  [スキップ] %s" % label)
        print("             %s" % reason)
        hint = HINTS.get(code)
        if hint:
            for line in hint.split("\n"):
                print("             %s" % line)
        result["skipped"].append({"file": label, "code": code, "reason": reason})
        return True

    size = os.path.getsize(path)
    if size > rules.MAX_TXT_BYTES:
        return skip("大きすぎます（%.1f MB）。日記ではないようです。"
                    % (size / 1024 / 1024), "too-long")

    non_article = rules.non_article_name(name)
    if non_article:
        return skip(non_article, "non-article")

    text, enc, digest, replaced = ingest.read_text(path)
    if replaced:
        return skip("文字コードが読み取れず、文字化けしています（%s）。" % enc, "bad-encoding")
    if "\x00" in text:
        return skip("文字ではないデータが入っています（画像や書類の可能性）。", "binary")
    if not text.strip():
        return skip("中身が空です。", "empty")

    title, tags, body_text = ingest.split_header(text)
    day = rules.date_from_name(name) or rules.date_from_name(title or "")

    plan = catalog.plan(title=title, body_text=body_text, day=day,
                        category=category, tags=tags, source="inbox",
                        source_name=name, digest=digest, today=today)

    if plan.code == "already":
        return skip("すでに同じ TXT を取り込んでいます（二重投稿はしません）。", "already")
    if not plan.ok:
        return skip(plan.reason, plan.code)

    print("  [取り込み] %s → /posts/%s/  「%s」 %d字%s"
          % (label, plan.slug, plan.title, plan.chars,
             "  タグ: " + "・".join(plan.tags) if plan.tags else ""))
    if plan.added_suffix:
        print("             [注意] /posts/%s/ はすでにあるため、別の記事として足します。"
              % plan.added_suffix)
        print("                    同じ日に何本でも書ける決まりです。")
    if plan.chars < rules.SHORT_BODY_CHARS:
        print("             [注意] 本文が %d 字しかありません。" % plan.chars)

    catalog.remember(plan)
    result["imported"].append({
        "file": label, "slug": plan.slug, "title": plan.title,
        "diary_date": plan.day.isoformat(), "category": plan.category,
        "tags": plan.tags, "chars": plan.chars,
        "path": os.path.relpath(plan.out_path, common.ROOT).replace(os.sep, "/"),
    })

    if dry_run:
        return False

    catalog.write(plan)
    if not keep:
        try:
            os.remove(path)
        except OSError as exc:
            # Windows で、その TXT をメモ帳などで開いたままだと消せない。
            # 記事はもうできているので、ここでは知らせるだけにする。
            print("             [注意] inbox の TXT を消せませんでした: %s" % exc)
            print("                    開いているソフトを閉じて、手で消してください。")
        else:
            print("             inbox から取り除きました（git の履歴には残ります）。")
            prune_empty(os.path.dirname(path))
    return False


def prune_empty(folder):
    """空になった inbox のサブフォルダを片づける（inbox 自体は消さない）。"""
    if os.path.realpath(folder) == os.path.realpath(INBOX):
        return
    if not rules.under(INBOX, folder):
        return
    try:
        if not os.listdir(folder):
            os.rmdir(folder)
    except OSError:
        pass


def write_json(path, result):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
        f.write("\n")


if __name__ == "__main__":
    sys.exit(main())
