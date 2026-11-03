# -*- coding: utf-8 -*-
"""クラウド投稿の予行演習。本番のファイルには一切触らずに通して試す。

使い方:
    python rehearse_post.py        通しで試して、結果を日本語で出す
    python rehearse_post.py -v     ツールの出力もそのまま見せる

やっていること:
    一時フォルダにブログを丸ごと複製し、そこで
        inbox の TXT から記事を作る       （tools/import_inbox.py）
        投稿フォームから記事を作る        （tools/post_from_issue.py）
        組み立て → 総合検査 → リンク点検  （build.py / check.py / verify.py）
    を実際に走らせ、できた記事の中身を確かめる。

    リポジトリ本体は読むだけ。最後に「1バイトも変わっていないこと」も確かめる。
    使う文章はこのファイルの中の作り物で、実際の日記は使わない。

    GitHub Actions（.github/workflows/post-rehearsal.yml）からも、
    手元の Windows からも、同じものが走る。
"""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import ingest

common.use_utf8_stdout()

ROOT = common.ROOT
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")
VERBOSE = "-v" in sys.argv[1:]

# 予行演習で使う作り物の文章。実際の日記は使わない。
INBOX_TEXT = """# 予行演習（inbox から）
タグ: 予行演習

これは仕組みを確かめるための作り物の文章です。公開はしません。
空行で段落が分かれることも、ここで確かめています。

二段落目です。**太字**も確かめます。
"""

# 投稿フォームの本文。GitHub が作る形（### 見出し のあとに値）をまねている。
# わざと、コマンドや HTML として危ない形の文字を入れてある。
# これがそのまま実行されたり、タグとして効いてしまわないことを確かめる。
# 投稿フォームに入れる作り物の本文。わざと、コマンドや HTML として
# 危ない形の文字を入れてある。これがそのまま実行されたり、タグとして
# 効いてしまわないことを確かめる。
ISSUE_BODY_TEXT = (
    "これは投稿フォームから送られた作り物の文章です。公開はしません。\n"
    "危ない形の文字も混ぜています: $(echo abc) `echo def` "
    "<script>alert(1)</script> & < >\n"
    "\n"
    "二段落目です。ここも段落として分かれるはずです。"
)

ISSUE_TEXT = """### 本文

%(body)s

### 日付

%(day)s

### タイトル

予行演習（投稿フォームから）

### カテゴリー

感想

### タグ

予行演習, 仕組み

### 公開の確認

- [X] この内容を公開してよい（送った時点で第三者に読まれ、取り消せないことを分かっています）
"""


def run(cwd, script, *args, env=None):
    merged = dict(os.environ)
    merged.pop("GITHUB_STEP_SUMMARY", None)   # 予行演習の結果は要約に書かない
    if env:
        merged.update(env)
    result = subprocess.run(
        [sys.executable, os.path.join("tools", script)] + list(args),
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=merged)
    if VERBOSE:
        print("--- %s %s ---" % (script, " ".join(args)))
        print(result.stdout)
    return result


def git(cwd, *args):
    """git を動かす。git が入っていない環境では何もしない（None を返す）。"""
    try:
        return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None


def tree_digest(root):
    out = {}
    if not os.path.isdir(root):
        return out
    for folder, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            if name == ".build-manifest.json":
                continue
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root).replace(os.sep, "/")] = \
                    hashlib.sha256(f.read()).hexdigest()
    return out


def prepare(blog):
    """ブログを複製して、組み立てと凍結まで済ませる。"""
    os.makedirs(blog)
    for name in ("content", "templates", "tools"):
        shutil.copytree(os.path.join(ROOT, name), os.path.join(blog, name),
                        ignore=IGNORE)
    for name in ("config.json", ".gitattributes", ".gitignore"):
        src = os.path.join(ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, blog)
    docs = os.path.join(blog, "docs")
    os.makedirs(docs)
    for prefix in ("images", "assets"):
        src = os.path.join(ROOT, "docs", prefix)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(docs, prefix))
    os.makedirs(os.path.join(blog, "inbox"))
    built = run(blog, "build.py")
    if built.returncode != 0:
        raise RuntimeError("複製の組み立てに失敗しました:\n%s" % built.stdout)
    base = run(blog, "baseline.py", "--update", "--はい")
    if base.returncode != 0:
        raise RuntimeError("複製の凍結に失敗しました:\n%s" % base.stdout)
    # 衛生の点検（公開してはいけないファイル）は git の情報を使うので、
    # ここで git リポジトリにしておく。
    git(blog, "init", "-q", ".")
    git(blog, "add", "-A")
    git(blog, "-c", "user.email=rehearsal@example.invalid", "-c",
        "user.name=rehearsal", "commit", "-qm", "rehearsal")


def entry_of(blog, slug):
    path = os.path.join(blog, "content", "posts", *(slug.split("/")))
    return common.read_entry(path + ".html")


class Failure(Exception):
    pass


def expect(condition, message):
    if not condition:
        raise Failure(message)


def rehearse(blog):
    """複製の中で、2つの入口から記事を作って確かめる。戻り値は報告の行。"""
    lines = []
    cfg = common.load_config()
    day = ingest.local_today(cfg)
    before_posts = len([p for p in tree_digest(os.path.join(blog, "content", "posts"))
                        if p.endswith(".html")])

    # ---- 1. inbox から ----
    # 名前を「<きょう>.txt」にしてはいけない。
    # 本人がきょう inbox から日記を入れていると、取り込み済みの記事と
    # 同じ名前になり、二重投稿の防止で止まってしまう
    # （二重投稿の判定は（カテゴリー, 元のファイル名）で見ている）。
    # 実機で1回通したとき、これで予行演習が落ちた。
    # 日付は名前から読まれるので、後ろに目印を付けてもきょうの記事になる。
    # この決まりは selftest.py の test_106 が見張っている。
    inbox_name = "%s-予行演習.txt" % day.isoformat()
    with open(os.path.join(blog, "inbox", inbox_name), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(INBOX_TEXT)
    result = run(blog, "import_inbox.py", "--json",
                 os.path.join(blog, "inbox-result.json"))
    expect(result.returncode == 0,
           "inbox からの取り込みが失敗しました:\n%s" % result.stdout)
    with open(os.path.join(blog, "inbox-result.json"), encoding="utf-8") as f:
        inbox_info = json.load(f)
    os.remove(os.path.join(blog, "inbox-result.json"))
    expect(len(inbox_info["imported"]) == 1,
           "inbox から取り込めた件数が 1 件ではありません: %r" % inbox_info)
    inbox_slug = inbox_info["imported"][0]["slug"]
    entry = entry_of(blog, inbox_slug)
    expect(entry["title"] == "予行演習（inbox から）", "題が違います: %r" % entry["title"])
    expect(entry["diary_date"] == day.isoformat(),
           "日記の日付が違います: %r" % entry["diary_date"])
    expect(entry["categories"] == [cfg.get("default_category") or "日記"],
           "カテゴリーが違います: %r" % entry["categories"])
    expect(entry["tags"] == ["予行演習"], "タグが違います: %r" % entry["tags"])
    expect(entry["source"] == "inbox", "取り込み元が違います: %r" % entry["source"])
    expect("<strong>太字</strong>" in entry["_body"], "太字が反映されていません")
    expect(not os.path.exists(os.path.join(blog, "inbox", inbox_name)),
           "取り込んだあとも inbox に TXT が残っています")
    lines.append("inbox の TXT から記事ができた … /posts/%s/" % inbox_slug)

    # 二重投稿の防止（同じ TXT をもう一度置く）
    with open(os.path.join(blog, "inbox", inbox_name), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(INBOX_TEXT)
    again = run(blog, "import_inbox.py")
    expect(again.returncode != 0, "同じ TXT が二度目も通ってしまいました")
    expect("すでに" in again.stdout, "二重投稿を止めた理由が出ていません:\n%s" % again.stdout)
    after_posts = len([p for p in tree_digest(os.path.join(blog, "content", "posts"))
                       if p.endswith(".html")])
    expect(after_posts == before_posts + 1, "記事が増えてしまいました: %d" % after_posts)
    os.remove(os.path.join(blog, "inbox", inbox_name))
    lines.append("同じ TXT をもう一度置いても増えない（二重投稿の防止）")

    # ---- 2. 投稿フォームから ----
    def form(body=ISSUE_BODY_TEXT, day_text=None, text=ISSUE_TEXT):
        return text % {"body": body, "day": day_text or day.isoformat()}

    issue_body = form()
    env = {"ISSUE_BODY": issue_body, "ISSUE_NUMBER": "9001",
           "ISSUE_TITLE": "日記の投稿"}
    result = run(blog, "post_from_issue.py", "--json",
                 os.path.join(blog, "issue-result.json"),
                 "--pr-body", os.path.join(blog, "issue-pr.md"),
                 "--comment", os.path.join(blog, "issue-comment.md"), env=env)
    expect(result.returncode == 0,
           "投稿フォームからの取り込みが失敗しました:\n%s" % result.stdout)
    with open(os.path.join(blog, "issue-result.json"), encoding="utf-8") as f:
        issue_info = json.load(f)
    for name in ("issue-result.json", "issue-pr.md", "issue-comment.md"):
        os.remove(os.path.join(blog, name))
    issue_slug = issue_info["slug"]
    entry = entry_of(blog, issue_slug)
    expect(entry["title"] == "予行演習（投稿フォームから）",
           "題が違います: %r" % entry["title"])
    expect(entry["categories"] == ["感想"], "カテゴリーが違います: %r" % entry["categories"])
    expect(entry["tags"] == ["予行演習", "仕組み"], "タグが違います: %r" % entry["tags"])
    expect(entry["source"] == "issue", "取り込み元が違います: %r" % entry["source"])
    expect(entry.get("source_ref") == "issue-9001",
           "投稿の目印が違います: %r" % entry.get("source_ref"))
    expect(issue_slug.startswith("review/"),
           "カテゴリー別の場所に入っていません: %r" % issue_slug)
    # 危ない形の文字が、そのままタグや命令として効いていないこと
    expect("<script>" not in entry["_body"], "<script> がそのまま入っています")
    expect("&lt;script&gt;alert(1)&lt;/script&gt;" in entry["_body"],
           "危ない文字が安全な形になっていません")
    expect("$(echo abc)" in entry["_body"], "本文の文字が消えています")
    lines.append("投稿フォームから記事ができた … /posts/%s/" % issue_slug)
    lines.append("危ない形の文字（HTML・コマンド）が、文字のまま扱われている")

    # 同じ Issue からの二度目
    again = run(blog, "post_from_issue.py", env=env)
    expect(again.returncode != 0, "同じ投稿が二度目も通ってしまいました")
    expect("取り込み済み" in again.stdout,
           "二重取り込みを止めた理由が出ていません:\n%s" % again.stdout)
    lines.append("同じ投稿をもう一度送っても増えない（二重投稿の防止）")

    # ---- 3. 組み立てと検査 ----
    built = run(blog, "build.py")
    expect(built.returncode == 0, "組み立てに失敗しました:\n%s" % built.stdout)
    for slug in (inbox_slug, issue_slug):
        page = os.path.join(blog, "docs", "posts", *(slug.split("/")), "index.html")
        expect(os.path.isfile(page), "公開ページができていません: %s" % page)
    checked = run(blog, "check.py")
    expect(checked.returncode == 0, "総合検査が通りませんでした:\n%s" % checked.stdout)
    expect("新しい記事" in checked.stdout,
           "新しい記事があることが知らされていません")
    verified = run(blog, "verify.py")
    expect(verified.returncode == 0, "リンクの点検が通りませんでした:\n%s" % verified.stdout)
    lines.append("組み立て・総合検査・リンク点検が、すべて通った")

    # ---- 4. 誤った投稿が止まること ----
    bad = run(blog, "post_from_issue.py",
              env={"ISSUE_BODY": "ふつうの文章です。フォームの形ではありません。",
                   "ISSUE_NUMBER": "9002"})
    expect(bad.returncode != 0, "フォームの形でないものが通ってしまいました")
    expect("投稿フォームの形" in bad.stdout, "理由が出ていません:\n%s" % bad.stdout)

    # ここから先は「別の本文」で試す。同じ本文のままだと、確かめたい理由より
    # 先に「本文が同じ」で止まってしまい、何を試したのか分からなくなるため。
    other = "これは別の作り物の文章です。止まることを確かめるために使います。"
    no_mark = form(body=other, text=ISSUE_TEXT.replace("- [X]", "- [ ]"))
    bad = run(blog, "post_from_issue.py",
              env={"ISSUE_BODY": no_mark, "ISSUE_NUMBER": "9003"})
    expect(bad.returncode != 0, "公開の確認が無いものが通ってしまいました")
    expect("公開の確認" in bad.stdout, "理由が出ていません:\n%s" % bad.stdout)

    unknown = form(body=other,
                   text=ISSUE_TEXT.replace("### カテゴリー\n\n感想",
                                           "### カテゴリー\n\n知らないぶんるい"))
    bad = run(blog, "post_from_issue.py",
              env={"ISSUE_BODY": unknown, "ISSUE_NUMBER": "9004"})
    expect(bad.returncode != 0, "登録されていないカテゴリーが通ってしまいました")
    expect("登録されていないカテゴリー" in bad.stdout,
           "理由が出ていません:\n%s" % bad.stdout)
    lines.append("フォームの形・公開の確認・カテゴリーの誤りは、きちんと止まる")
    return lines


def main():
    print("=" * 56)
    print(" クラウド投稿の予行演習")
    print("=" * 56)
    print("本番のファイルには触りません（一時フォルダで試します）。")
    print()

    before_content = tree_digest(os.path.join(ROOT, "content"))
    before_docs = tree_digest(os.path.join(ROOT, "docs"))
    before_inbox = tree_digest(os.path.join(ROOT, "inbox"))

    tmp = tempfile.mkdtemp(prefix="blog-rehearse-")
    try:
        blog = os.path.join(tmp, "blog")
        prepare(blog)
        lines = rehearse(blog)
    except Failure as exc:
        print("*** 予行演習で問題が見つかりました ***")
        print(str(exc))
        return 1
    except RuntimeError as exc:
        print("*** 予行演習の下ごしらえに失敗しました ***")
        print(str(exc))
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if (before_content != tree_digest(os.path.join(ROOT, "content"))
            or before_docs != tree_digest(os.path.join(ROOT, "docs"))
            or before_inbox != tree_digest(os.path.join(ROOT, "inbox"))):
        print("*** 予行演習が本番のファイルを書き換えました（あってはいけません） ***")
        return 1

    for line in lines:
        print("  [OK] %s" % line)
    print()
    print("=" * 56)
    print("予行演習はすべて通りました（%d 項目）。" % len(lines))
    print("本番のファイルは1バイトも変わっていません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
