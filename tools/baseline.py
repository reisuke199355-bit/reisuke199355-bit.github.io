# -*- coding: utf-8 -*-
"""公開済みの記事・旧URL・手で用意した公開ファイルを「基準線」として記録する。

使い方:
    python baseline.py                  いまの基準線と中身を見比べて表示する
    python baseline.py --update         基準線を作り直す（確認を求める）
    python baseline.py --update --はい  確認を省いて作り直す
    python baseline.py --修正許可 2026-08-06
                                        過去記事を直すために、その記事だけ一時的に開ける
    python baseline.py --修正許可の解除   開けたものをすべて閉じる

なぜ必要か:
    公開済みの記事は、本文・日付・URL を変えてはいけない。
    うっかり書き換えたり消したりしても、見た目では気づけないので、
    記事ごとの指紋（SHA-256）を baseline.json に残して見張る。

    新しい記事は基準線に無くてよい（「新しい記事が N 件」と知らせるだけ）。
    毎日書き足す運用と、過去記事の凍結を、両方とも成り立たせるため。

過去記事を直したいとき:
    1. python baseline.py --修正許可 <記事のURL>
    2. content/posts の記事を直す（または import_txt.py --force で入れ直す）
    3. python build.py
    4. python baseline.py --update        ← 変わった中身を見て「はい」と答える
    5. python check.py                    ← 緑になることを確かめる

    手順1で作る控え（.baseline-unlock）は GitHub へ送られない。
    そのため、GitHub 側の自動検査はいつも厳しいままで、
    「直したのに基準線を作り直していない」状態をきちんと見つけてくれる。
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import rules

common.use_utf8_stdout()

BASELINE = os.path.join(common.ROOT, "baseline.json")
UNLOCK = os.path.join(common.ROOT, ".baseline-unlock")

# 記事ごとに見張る項目。ここを変えてよいのは、人が --update を実行したときだけ。
WATCHED_FIELDS = ("title", "date", "diary_date", "categories", "legacy_slug")


def body_digest(body):
    """本文の指紋。改行の書き方のちがいだけは吸収する。

    タグを外さずにそのまま数えるのが大事。verify.py の本文照合はタグを
    全部外してから比べるため、画像や <strong> がまるごと消えても気づけない。
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.lstrip("\n").rstrip() + "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def post_record(post):
    rec = {"body_sha256": body_digest(post.get("_body", ""))}
    for field in WATCHED_FIELDS:
        value = post.get(field)
        if isinstance(value, list):
            rec[field] = list(value)
        elif value:
            rec[field] = value
    return rec


def static_files():
    """ビルドが作らない公開ファイル（手で用意したもの）の指紋。"""
    out = {}
    for prefix in rules.NON_GENERATED_DOCS_PREFIX:
        root = os.path.join(common.DOCS, prefix.rstrip("/"))
        if not os.path.isdir(root):
            continue
        for folder, dirs, names in os.walk(root):
            dirs.sort()
            for name in sorted(names):
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, common.DOCS).replace(os.sep, "/")
                out[rel] = file_digest(path)
    return out


def snapshot():
    """いまのリポジトリの中身から基準線を組み立てる。"""
    posts = common.load_posts()
    pages = common.load_pages()
    legacy = {}
    for name, target, _title in common.legacy_pairs(list(reversed(posts)), pages):
        legacy[name] = target
    return {
        "note": "公開済みの記事・旧URL・手で用意した公開ファイルの記録。"
                "tools/baseline.py で作る。手で書き換えないこと。",
        "how_to_update": "python tools/baseline.py --update",
        "counts": {
            "posts": len(posts),
            "pages": len(pages),
            "legacy_urls": len(legacy),
            "static_files": len(static_files()),
        },
        "posts": {p["slug"]: post_record(p) for p in posts if p.get("slug")},
        "legacy_urls": legacy,
        "static_files": static_files(),
    }


def load_baseline():
    if not os.path.isfile(BASELINE):
        return None
    with open(BASELINE, encoding="utf-8") as f:
        return json.load(f)


def save_baseline(data):
    with open(BASELINE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def load_unlocked():
    """一時的に編集を許した記事のURL一覧。GitHub へは送られない控えから読む。"""
    if not os.path.isfile(UNLOCK):
        return set()
    try:
        with open(UNLOCK, encoding="utf-8") as f:
            return set(json.load(f).get("slugs", []))
    except (ValueError, OSError):
        return set()


def save_unlocked(slugs):
    if not slugs:
        if os.path.isfile(UNLOCK):
            os.remove(UNLOCK)
        return
    with open(UNLOCK, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"note": "過去記事の編集を一時的に許している記事。"
                           "GitHub には送られない。",
                   "slugs": sorted(slugs)}, f, ensure_ascii=False, indent=1)
        f.write("\n")


FIELD_LABEL = {
    "title": "題",
    "date": "投稿日",
    "diary_date": "日記の日付",
    "categories": "カテゴリー",
    "legacy_slug": "旧URL",
    "body_sha256": "本文",
}


def compare(base, now, unlocked=None):
    """基準線といまの中身を見比べて、問題と注意と知らせを返す。

    戻り値は (問題, 注意, 知らせ) の3つの一覧。いずれも日本語の文字列。
    unlocked に入っている記事の変化は、問題ではなく注意として扱う。
    """
    unlocked = unlocked or set()
    errors, warnings, notes = [], [], []

    old_posts = base.get("posts", {})
    new_posts = now.get("posts", {})

    for slug in sorted(set(old_posts) - set(new_posts)):
        title = old_posts[slug].get("title", "")
        errors.append("公開済みの記事が無くなっています: /posts/%s/ 「%s」" % (slug, title))

    added = sorted(set(new_posts) - set(old_posts))
    if added:
        notes.append("新しい記事が %d 件あります: %s"
                     % (len(added), "、".join("/posts/%s/" % s for s in added[:5])
                        + ("、ほか %d 件" % (len(added) - 5) if len(added) > 5 else "")))

    for slug in sorted(set(old_posts) & set(new_posts)):
        old, new = old_posts[slug], new_posts[slug]
        changed = []
        for field in ("body_sha256",) + WATCHED_FIELDS:
            if old.get(field) != new.get(field):
                label = FIELD_LABEL.get(field, field)
                if field == "body_sha256":
                    changed.append("本文")
                else:
                    changed.append("%s（%r → %r）"
                                   % (label, old.get(field), new.get(field)))
        if not changed:
            continue
        title = new.get("title") or old.get("title") or ""
        line = "公開済みの記事が変わっています: /posts/%s/ 「%s」 … %s" \
               % (slug, title, " / ".join(changed))
        if slug in unlocked:
            warnings.append(line + "  ［修正許可中］"
                            "直し終わったら python tools/baseline.py --update を実行してください")
        else:
            errors.append(line)

    old_legacy = base.get("legacy_urls", {})
    new_legacy = now.get("legacy_urls", {})
    for name in sorted(set(old_legacy) - set(new_legacy)):
        errors.append("旧URLからの転送が無くなっています: /%s/ （→ %s）"
                      % (name, old_legacy[name]))
    for name in sorted(set(old_legacy) & set(new_legacy)):
        if old_legacy[name] != new_legacy[name]:
            errors.append("旧URLの転送先が変わっています: /%s/ : %s → %s"
                          % (name, old_legacy[name], new_legacy[name]))
    added_legacy = sorted(set(new_legacy) - set(old_legacy))
    if added_legacy:
        notes.append("旧URLからの転送が %d 件増えています: %s"
                     % (len(added_legacy), "、".join("/%s/" % n for n in added_legacy[:5])))

    old_static = base.get("static_files", {})
    new_static = now.get("static_files", {})
    for rel in sorted(set(old_static) - set(new_static)):
        errors.append("手で用意した公開ファイルが無くなっています: docs/%s" % rel)
    for rel in sorted(set(old_static) & set(new_static)):
        if old_static[rel] != new_static[rel]:
            errors.append("手で用意した公開ファイルの中身が変わっています: docs/%s" % rel)
    added_static = sorted(set(new_static) - set(old_static))
    if added_static:
        notes.append("公開ファイルが %d 件増えています: %s"
                     % (len(added_static), "、".join("docs/%s" % r for r in added_static[:5])))

    return errors, warnings, notes


def ask(question):
    """publish.py と同じ作法で確認を求める。"""
    print("\n" + question)
    print('  続けるなら「はい」と入力して Enter（やめるなら、そのまま Enter）: ', end="")
    sys.stdout.flush()
    try:
        return sys.stdin.readline().strip() in ("はい", "ハイ", "yes", "y", "Y")
    except (EOFError, KeyboardInterrupt):
        return False


def show_status():
    now = snapshot()
    base = load_baseline()
    unlocked = load_unlocked()
    print("=" * 56)
    print(" 基準線（公開済みの記事の記録）の状態")
    print("=" * 56)
    print("記事: %d 件 / 固定ページ: %d 件 / 旧URL: %d 件 / 手で用意した公開ファイル: %d 件"
          % (now["counts"]["posts"], now["counts"]["pages"],
             now["counts"]["legacy_urls"], now["counts"]["static_files"]))
    if base is None:
        print("\n基準線がまだありません。")
        print("  python tools/baseline.py --update で作ってください。")
        return 1
    print("記録されている記事: %d 件 / 旧URL: %d 件"
          % (len(base.get("posts", {})), len(base.get("legacy_urls", {}))))
    if unlocked:
        print("\n［修正許可中］%s" % "、".join(sorted(unlocked)))
        print("  直し終わったら python tools/baseline.py --update を実行してください。")
    errors, warnings, notes = compare(base, now, unlocked)
    print()
    for line in notes:
        print("  [知らせ] %s" % line)
    for line in warnings:
        print("  [注意] %s" % line)
    for line in errors:
        print("  [NG] %s" % line)
    print("\n" + "=" * 56)
    if errors:
        print("公開済みの内容に %d 件の変化があります。" % len(errors))
        print("意図した修正なら、python tools/baseline.py --update で記録し直してください。")
        return 1
    print("公開済みの記事・旧URL・公開ファイルは、記録どおりです。")
    return 0


def do_update(args):
    now = snapshot()
    base = load_baseline()
    unlocked = load_unlocked()

    print("=" * 56)
    print(" 基準線を作り直します")
    print("=" * 56)

    if base is None:
        print("はじめての作成です。")
        print("  記事 %d 件 / 旧URL %d 件 / 手で用意した公開ファイル %d 件 を記録します。"
              % (now["counts"]["posts"], now["counts"]["legacy_urls"],
                 now["counts"]["static_files"]))
    else:
        errors, warnings, notes = compare(base, now, unlocked=set())
        if not errors and not notes:
            print("記録と中身は同じです。作り直す必要はありません。")
            if unlocked:
                save_unlocked(set())
                print("修正許可の控えを閉じました。")
            return 0
        print("次の内容を記録し直します。よく確かめてください。\n")
        for line in notes:
            print("  [知らせ] %s" % line)
        for line in errors:
            print("  [変化] %s" % line)

    if "--はい" not in args and "--yes" not in args:
        if not sys.stdin.isatty():
            print("\n画面から確認が取れないため、中断しました。")
            print("確認を省いて実行するなら --はい を付けてください。")
            return 1
        if not ask("この内容で基準線を作り直しますか？"):
            print("中断しました（baseline.json は変えていません）。")
            return 0

    save_baseline(now)
    if unlocked:
        save_unlocked(set())
        print("修正許可の控えを閉じました。")
    print("\nbaseline.json を作り直しました。")
    print("  記事 %d 件 / 旧URL %d 件 / 手で用意した公開ファイル %d 件"
          % (now["counts"]["posts"], now["counts"]["legacy_urls"],
             now["counts"]["static_files"]))
    print("続けて python tools/check.py で確かめてください。")
    return 0


def do_unlock(args):
    base = load_baseline()
    if base is None:
        print("基準線がまだありません。先に python tools/baseline.py --update を実行してください。")
        return 1
    flags = ("--修正許可", "--allow", "--unlock")
    wanted = []
    i = 0
    while i < len(args):
        if args[i] in flags:
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                wanted.append(args[i])
                i += 1
            continue
        i += 1
    if not wanted:
        print("どの記事を直すのか、URL（slug）を指定してください。")
        print("  例: python tools/baseline.py --修正許可 2026-08-06")
        return 1

    known = base.get("posts", {})
    unknown = [s for s in wanted if s not in known]
    if unknown:
        print("基準線に無い記事です（新しい記事は、そのまま直せます）: %s"
              % "、".join(unknown))
        return 1

    unlocked = load_unlocked() | set(wanted)
    save_unlocked(unlocked)
    print("=" * 56)
    print(" 過去記事の修正を許可しました")
    print("=" * 56)
    for slug in sorted(wanted):
        print("  /posts/%s/ 「%s」" % (slug, known[slug].get("title", "")))
    print("\n次の順に進めてください。")
    print("  1. content/posts の記事を直す")
    print("  2. python tools/build.py")
    print("  3. python tools/baseline.py --update   ← 変わった中身を見て「はい」と答える")
    print("  4. python tools/check.py               ← 緑になることを確かめる")
    print("\nこの許可は手元だけのもので、GitHub には送られません。")
    print("手順3をやらずに公開すると、GitHub 側の検査が止めてくれます。")
    return 0


def main():
    args = sys.argv[1:]
    if any(a in ("--修正許可の解除", "--clear-unlock") for a in args):
        save_unlocked(set())
        print("修正許可の控えを閉じました。")
        return 0
    if any(a in ("--修正許可", "--allow", "--unlock") for a in args):
        return do_unlock(args)
    if any(a in ("--update", "--更新") for a in args):
        return do_update(args)
    return show_status()


if __name__ == "__main__":
    sys.exit(main())
