# -*- coding: utf-8 -*-
"""取り込みと検査の仕組みが、ほんとうに働くことを自動で確かめる。

使い方:
    python selftest.py            すべて試す
    python selftest.py -v         1件ずつ名前を出しながら試す

やっていること:
    一時フォルダにブログを丸ごと複製し、そこで実際に
    取り込み → 組み立て → 検査 を走らせて、結果を確かめる。
    リポジトリ本体には一切書き込まない（読むだけ）。

    「ふだんの使い方が壊れていないこと」と
    「まちがいをきちんと止めること」の両方を確かめる。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import rules

common.use_utf8_stdout()

ROOT = common.ROOT
TEMPLATE = None             # 試すたびに複製する、組み立て済みのひな型
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")

NEW_POST = "2026年11月02日\n\n今日は自動検査の仕組みを作った。\nうまく動いてくれるとよい。\n"


def run(cwd, script, *args, stdin="", env=None):
    """ツールを動かして結果を返す。画面入力は与えた文字列だけ。

    env には足したい環境変数だけを渡す（投稿フォームの本文など）。
    いまの環境変数を引き継いだうえで上書きする。
    """
    merged = None
    if env:
        merged = dict(os.environ)
        merged.update(env)
    return subprocess.run(
        [sys.executable, os.path.join("tools", script)] + list(args),
        cwd=cwd, input=stdin, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=merged)


def git(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def tree_digest(root, only_ext=None):
    """フォルダの中身の指紋を、ファイルごとに作る。"""
    out = {}
    if not os.path.isdir(root):
        return out
    for folder, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            if name == ".build-manifest.json":
                continue
            if only_ext and not name.endswith(only_ext):
                continue
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root).replace(os.sep, "/")] = \
                    hashlib.sha256(f.read()).hexdigest()
    return out


def manifest_state(root):
    """作業用の控え（.build-manifest.json）のいまの様子を返す。

    有無・中身・更新日時をまとめて見る。無いときは None。
    この控えは build.py がふだん作るもので、手元には残っているのが
    ふつう。だから「無いこと」ではなく「変わっていないこと」を見る。
    """
    path = os.path.join(root, ".build-manifest.json")
    if not os.path.exists(path):
        return None
    info = os.stat(path)
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return (info.st_size, info.st_mtime_ns, digest)


def setUpModule():
    """ひな型を1回だけ作る（毎回ビルドすると遅いため）。"""
    global TEMPLATE
    TEMPLATE = tempfile.mkdtemp(prefix="blog-selftest-base-")
    blog = os.path.join(TEMPLATE, "blog")
    os.makedirs(blog)
    for name in ("content", "templates", "tools", "inbox"):
        src = os.path.join(ROOT, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(blog, name), ignore=IGNORE)
    os.makedirs(os.path.join(blog, "inbox"), exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "config.json"), blog)
    docs = os.path.join(blog, "docs")
    os.makedirs(docs)
    for prefix in ("images", "assets"):
        src = os.path.join(ROOT, "docs", prefix)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(docs, prefix))
    built = run(blog, "build.py")
    if built.returncode != 0:
        raise RuntimeError("ひな型の組み立てに失敗しました:\n%s" % built.stdout)
    base = run(blog, "baseline.py", "--update", "--はい")
    if base.returncode != 0:
        raise RuntimeError("ひな型の基準線づくりに失敗しました:\n%s" % base.stdout)
    os.makedirs(os.path.join(TEMPLATE, "10_記事管理", "日記"))


def tearDownModule():
    if TEMPLATE:
        shutil.rmtree(TEMPLATE, ignore_errors=True)


class Sandbox:
    """ひな型の複製。blog フォルダと 10_記事管理 フォルダを持つ。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="blog-selftest-")
        shutil.copytree(os.path.join(TEMPLATE, "blog"), os.path.join(self.root, "blog"),
                        ignore=IGNORE)
        shutil.copytree(os.path.join(TEMPLATE, "10_記事管理"),
                        os.path.join(self.root, "10_記事管理"))
        self.blog = os.path.join(self.root, "blog")
        self.articles = os.path.join(self.root, "10_記事管理")

    def put(self, category, name, text):
        folder = os.path.join(self.articles, category)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def put_bytes(self, category, name, data):
        folder = os.path.join(self.articles, category)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def put_inbox(self, name, text, category=None):
        """inbox に TXT を置く（スマホ・クラウドからの投稿のつもり）。"""
        folder = os.path.join(self.blog, "inbox")
        if category:
            folder = os.path.join(folder, category)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def inbox_has(self, name, category=None):
        parts = ["inbox"] + ([category] if category else []) + [name]
        return os.path.exists(os.path.join(self.blog, *parts))

    def blog_path(self, *parts):
        return os.path.join(self.blog, *parts)

    def read(self, *parts):
        with open(self.blog_path(*parts), encoding="utf-8") as f:
            return f.read()

    def write(self, text, *parts):
        with open(self.blog_path(*parts), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def posts(self):
        return tree_digest(self.blog_path("content", "posts"), only_ext=".html")

    def docs(self):
        return tree_digest(self.blog_path("docs"))

    def make_git(self):
        git(self.blog, "init", "-q", ".")
        git(self.blog, "add", "-A")
        git(self.blog, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
            "commit", "-qm", "init")

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Base(unittest.TestCase):
    def sandbox(self):
        box = Sandbox()
        self.addCleanup(box.close)
        return box

    def assertNG(self, result, *fragments):
        self.assertNotEqual(result.returncode, 0,
                            "止まるはずが通ってしまいました:\n%s" % result.stdout)
        both = (result.stdout or "") + (result.stderr or "")
        for fragment in fragments:
            self.assertIn(fragment, both)

    def assertOK(self, result):
        self.assertEqual(result.returncode, 0,
                         "通るはずが止まりました:\n%s\n%s" % (result.stdout, result.stderr))


# ----------------------------------------------------------- 正常系

class NormalUse(Base):
    """ふだんの使い方が壊れていないこと。"""

    def test_01_zero_padded_filename_imports(self):
        """いま使っている「2026年11月02日.txt」の形が取り込めること。"""
        box = self.sandbox()
        box.put("日記", "2026年11月02日.txt", NEW_POST)
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertIn("[取り込み] 日記 / 2026年11月02日.txt", result.stdout)
        self.assertIn("/posts/2026-11-02/", result.stdout)
        entry = common.parse_front_matter(box.read("content", "posts", "2026-11-02.html"))[0]
        self.assertEqual(entry["title"], "2026年11月02日")
        self.assertEqual(entry["date"], "2026-11-02 21:00:00")
        self.assertEqual(entry["slug"], "2026-11-02")
        self.assertEqual(entry["diary_date"], "2026-11-02")
        self.assertEqual(entry["categories"], ["日記"])
        self.assertEqual(entry["source"], "txt")

    def test_02_documented_date_formats(self):
        """説明書に書いてある日付の形が、すべて同じ日として読めること。"""
        for name in ("2026-11-02.txt", "2026_11_02.txt", "2026.11.02.txt",
                     "20261102.txt", "2026年11月02日.txt", "2026年11月2日.txt",
                     "2026年 11月 02日.txt", "2026年11月02日の日記.txt",
                     "日記2026年11月02日.txt", "20261102-旅行.txt"):
            with self.subTest(name=name):
                self.assertEqual(str(rules.date_from_name(name)), "2026-11-02")

    def test_03_existing_articles_stay_untouched(self):
        """取り込みと組み立てで、既存の記事と公開物が1バイトも変わらないこと。"""
        box = self.sandbox()
        before_posts, before_docs = box.posts(), box.docs()
        box.put("日記", "2026年11月02日.txt", NEW_POST)
        self.assertOK(run(box.blog, "import_txt.py"))
        self.assertOK(run(box.blog, "build.py"))
        after_posts, after_docs = box.posts(), box.docs()
        for rel, digest in before_posts.items():
            self.assertIn(rel, after_posts, "既存の記事が消えました: %s" % rel)
            self.assertEqual(digest, after_posts[rel], "既存の記事が変わりました: %s" % rel)
        for rel, digest in before_docs.items():
            self.assertIn(rel, after_docs, "公開物が消えました: %s" % rel)
        self.assertEqual(len(after_posts), len(before_posts) + 1)

    def test_04_check_passes_on_the_repository(self):
        """いまのリポジトリで、総合検査が通ること。"""
        self.assertOK(run(ROOT, "check.py"))

    def test_05_verify_passes_on_the_repository(self):
        """いまのリポジトリで、リンクと転送の点検が通ること。"""
        result = run(ROOT, "verify.py")
        self.assertOK(result)
        self.assertIn("旧URL: 108 件 / 転送ページ: 108 枚", result.stdout,
                      "旧URLの点検が固定ページを含んでいません")

    def test_06_feed_and_sitemap_are_publishable(self):
        """RSS と sitemap は公開を止められないこと（必要な XML を守る）。"""
        self.assertIsNone(rules.never_publish("docs/feed.xml"))
        self.assertIsNone(rules.never_publish("docs/sitemap.xml"))
        out = git(ROOT, "ls-files", "-z").stdout
        tracked = [p for p in out.split("\0") if p]
        self.assertTrue(tracked, "git の一覧が取れませんでした")
        blocked = [(p, rules.never_publish(p)) for p in tracked if rules.never_publish(p)]
        self.assertEqual(blocked, [], "ふつうのファイルが公開を止められています")
        self.assertTrue(any(p == "docs/feed.xml" for p in tracked))
        self.assertTrue(any(p == "docs/sitemap.xml" for p in tracked))

    def test_07_dangerous_files_are_blocked(self):
        """危ないファイルは、名前を変えてあっても止まること。"""
        for path in ("export-2026.xml", "reisuke.WordPress.2026-10-31.xml",
                     "wp-export.XML", "id_rsa", "deploy/id_rsa", "token.json",
                     "secrets.yaml", "credentials.json", ".env", ".env.local",
                     "cert.pfx", "cert.p12", "cert.crt", "data.sqlite", "notes.db",
                     "下書き.docx", "原稿.odt", "backup.tgz", "backup.tar.bz2",
                     "00_Backup/x.txt", "01_Analysis/x.xlsx", "10_下書き/未公開.txt",
                     "20_素材/原本.png", "Thumbs.db", ".DS_Store",
                     "content/posts/x.html.bak"):
            with self.subTest(path=path):
                self.assertIsNotNone(rules.never_publish(path),
                                     "%s が公開を止められていません" % path)

    def test_08_check_writes_nothing(self):
        """検査を走らせても、記事・公開物・作業用の控えが変わらないこと。

        .build-manifest.json を消したりはしない。
        build.py がふだん作る控えなので、手元にあるのがふつう。
        あっても無くても、検査の前後で変わっていなければよい。
        """
        before_posts = tree_digest(os.path.join(ROOT, "content"))
        before_docs = tree_digest(os.path.join(ROOT, "docs"))
        before_manifest = manifest_state(ROOT)
        self.assertOK(run(ROOT, "check.py"))
        self.assertEqual(before_posts, tree_digest(os.path.join(ROOT, "content")),
                         "検査が content/ を書き換えました")
        self.assertEqual(before_docs, tree_digest(os.path.join(ROOT, "docs")),
                         "検査が docs/ を書き換えました")
        self.assertEqual(before_manifest, manifest_state(ROOT),
                         "検査が作業用の控え（.build-manifest.json）を"
                         "作ったか、書き換えました")

    def test_09_legacy_exceptions_are_preserved(self):
        """旧URLが題と一致しない4件が、そのまま残っていること。"""
        posts = {p["slug"]: p for p in common.load_posts()}
        import urllib.parse
        for slug, legacy in rules.LEGACY_EXCEPTIONS.items():
            with self.subTest(slug=slug):
                self.assertIn(slug, posts, "記事が見つかりません: %s" % slug)
                got = urllib.parse.unquote(posts[slug].get("legacy_slug", ""))
                self.assertEqual(got, legacy)
                self.assertTrue(
                    os.path.isfile(os.path.join(ROOT, "docs", legacy, "index.html")),
                    "転送ページがありません: docs/%s/" % legacy)

    def test_10_new_category_can_be_registered(self):
        """新しいカテゴリーを登録して取り込み、サイトが壊れないこと。"""
        box = self.sandbox()
        box.put("読書", "2026年11月02日.txt", "2026年11月02日\n\n本を読んだ。面白かった。\n")
        blocked = run(box.blog, "import_txt.py")
        self.assertOK(blocked)
        self.assertIn("「読書」は登録されていないカテゴリーです", blocked.stdout)
        self.assertIn("取り込みません", blocked.stdout)
        self.assertFalse(os.path.isdir(box.blog_path("content", "posts", "読書")))

        allowed = run(box.blog, "import_txt.py", "--新カテゴリー許可")
        self.assertOK(allowed)
        self.assertIn("config.json に「読書」を追加しました", allowed.stdout)
        self.assertIn("/posts/読書/2026-11-02/", allowed.stdout)
        with open(box.blog_path("config.json"), encoding="utf-8") as f:
            self.assertIn("読書", json.load(f)["categories"])

        self.assertOK(run(box.blog, "build.py"))
        self.assertTrue(os.path.isfile(
            box.blog_path("docs", "posts", "読書", "2026-11-02", "index.html")))
        self.assertOK(run(box.blog, "verify.py"))
        self.assertOK(run(box.blog, "baseline.py", "--update", "--はい"))
        self.assertOK(run(box.blog, "check.py"))

    def test_11_multiple_posts_per_day_are_allowed(self):
        """同じ日に2本目を書けること（そのとき知らせが出ること）。"""
        box = self.sandbox()
        box.put("日記", "2026年10月30日その2.txt",
                "2026年10月30日\n\n同じ日にもう1本書いた。これは別の記事。\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertIn("/posts/2026-10-30-2/", result.stdout)
        self.assertIn("はすでにあるため、別の記事として足します", result.stdout)

    def test_12_edit_past_article_flow(self):
        """過去記事を直す手順が、ひととおり通ること。"""
        box = self.sandbox()
        path = box.blog_path("content", "posts", "2026-08-06.html")
        original = box.read("content", "posts", "2026-08-06.html")

        box.write(original.replace("今日は疲れた。", "今日はとても疲れた。"),
                  "content", "posts", "2026-08-06.html")
        self.assertOK(run(box.blog, "build.py"))
        blocked = run(box.blog, "check.py")
        self.assertNG(blocked, "公開済みの記事が変わっています", "/posts/2026-08-06/")

        self.assertOK(run(box.blog, "baseline.py", "--修正許可", "2026-08-06"))
        allowed = run(box.blog, "check.py")
        self.assertOK(allowed)
        self.assertIn("［修正許可中］", allowed.stdout)

        self.assertOK(run(box.blog, "baseline.py", "--update", "--はい"))
        self.assertOK(run(box.blog, "check.py"))
        self.assertFalse(os.path.exists(box.blog_path(".baseline-unlock")),
                         "修正許可の控えが残っています")
        self.assertTrue(os.path.isfile(path))

    def test_13_check_keeps_an_existing_build_manifest(self):
        """控えが置いてあるとき、検査がそれに触らないこと。

        手元の状態。build.py を走らせたあとは、いつもこうなっている。
        """
        box = self.sandbox()
        self.assertOK(run(box.blog, "build.py"))
        before = manifest_state(box.blog)
        self.assertIsNotNone(before, "build.py が控えを作っていません")
        self.assertOK(run(box.blog, "check.py"))
        after = manifest_state(box.blog)
        self.assertIsNotNone(after, "検査が控えを消しました")
        self.assertEqual(before, after,
                         "検査が控えの中身か更新日時を変えました")

    def test_14_check_does_not_create_a_build_manifest(self):
        """控えが無いとき、検査がそれを作らないこと。

        GitHub から取り出したばかりの状態（控えは公開物に含めていない）。
        検査は読むだけなので、ここで作ってはいけない。
        """
        box = self.sandbox()
        path = box.blog_path(".build-manifest.json")
        if os.path.exists(path):
            os.remove(path)
        self.assertIsNone(manifest_state(box.blog), "控えを消しきれていません")
        self.assertOK(run(box.blog, "check.py"))
        self.assertIsNone(manifest_state(box.blog),
                          "検査がリポジトリ直下に控えを作りました")


# ----------------------------------------------------------- 異常系（取り込み）

class ImportGuards(Base):
    """まちがった取り込みを止めること。"""

    def test_20_manual_outside_articles_folder(self):
        """記事管理フォルダの外のファイルは取り込まない（事故の再現）。"""
        box = self.sandbox()
        desktop = os.path.join(box.root, "デスクトップ")
        os.makedirs(desktop)
        path = os.path.join(desktop, "説明書.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# ブログの使い方\n\nこのファイルは説明書です。\n")
        result = run(box.blog, "import_txt.py", path)
        self.assertOK(result)
        self.assertIn("記事管理フォルダの外にあります", result.stdout)
        self.assertEqual(box.posts(), Sandbox.posts(box),
                         "記事が増えてしまいました")
        self.assertNotIn("デスクトップ", str(box.posts()))

    def test_21_manual_inside_category_folder(self):
        """説明書という名前のファイルは取り込まない（事故の再現）。"""
        box = self.sandbox()
        before = box.posts()
        box.put("日記", "説明書.txt", "# ブログの使い方\n\nこのファイルは説明書です。\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertIn("名前に「説明書」が入っています", result.stdout)
        self.assertEqual(before, box.posts())

    def test_22_no_date_in_filename(self):
        """ファイル名に日付がないものは取り込まない。"""
        box = self.sandbox()
        before = box.posts()
        box.put("日記", "きのうのこと.txt", "きのうは出かけた。\n\n楽しかった。\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertIn("ファイル名に日付が入っていません", result.stdout)
        self.assertEqual(before, box.posts())

    def test_23_heading_only_file(self):
        """見出しだけで本文がないものは取り込まない。"""
        box = self.sandbox()
        before = box.posts()
        box.put("日記", "2026-11-02.txt", "# タイトルだけ\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertIn("本文がありません", result.stdout)
        self.assertEqual(before, box.posts())

    def test_24_binary_file(self):
        """文字でないデータは取り込まない。"""
        box = self.sandbox()
        before = box.posts()
        box.put_bytes("日記", "2026-11-02.txt", b"\x00\x01\x02\xff\xfe binary \x00")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertTrue("文字ではないデータ" in result.stdout
                        or "文字化け" in result.stdout, result.stdout)
        self.assertEqual(before, box.posts())

    def test_25_same_body_in_another_category(self):
        """同じ本文を別のカテゴリーに置いても、二重にならない。

        字下げだけを変えてバイト単位では別物にしてあるので、
        中身で見分ける仕組みが働かないと二重に増えてしまう。
        """
        box = self.sandbox()
        box.put("日記", "2026年11月01日.txt", NEW_POST)
        self.assertOK(run(box.blog, "import_txt.py"))
        after_first = box.posts()
        box.put("感想", "2026年11月02日.txt",
                NEW_POST.replace("\n", "\n ").replace(" \n", "\n"))
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertIn("すでにある記事と本文が同じです", result.stdout)
        self.assertEqual(after_first, box.posts(), "同じ本文の記事が増えました")

    def test_26_trailing_newline_is_not_a_new_article(self):
        """末尾の改行の数が違うだけでは、別の記事にならない。"""
        box = self.sandbox()
        box.put("日記", "2026年11月01日.txt", NEW_POST)
        self.assertOK(run(box.blog, "import_txt.py"))
        after_first = box.posts()
        box.put("日記", "2026年11月02日.txt", NEW_POST + "\n\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertIn("すでにある記事と本文が同じです", result.stdout)
        self.assertEqual(after_first, box.posts())

    def test_27_done_folder_is_not_reimported(self):
        """取り込み済みの控えフォルダからは取り込まない。"""
        box = self.sandbox()
        before = box.posts()
        path = box.put(os.path.join("_取り込み済み", "日記"), "2026-11-02.txt",
                       "2026年11月02日\n\n控えから入れようとしてみる。\n")
        result = run(box.blog, "import_txt.py", path)
        self.assertOK(result)
        self.assertIn("「_」で始まるフォルダ", result.stdout)
        self.assertEqual(before, box.posts())
        self.assertOK(run(box.blog, "import_txt.py"))
        self.assertEqual(before, box.posts())

    def test_28_false_date_in_filename(self):
        """ファイル名の中のまぎらわしい数字を、日付として使わない。"""
        box = self.sandbox()
        before = box.posts()
        for name in ("v20260101_final.txt", "製品20260715番.txt", "report-12345678.txt"):
            box.put("日記", name, "本文です。\n\nもう少し本文。\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertEqual(before, box.posts())
        self.assertIn("ファイル名に日付が入っていません", result.stdout)

    def test_29_dates_outside_the_blog(self):
        """ブログの開始前・未来の日付は取り込まない。"""
        box = self.sandbox()
        before = box.posts()
        box.put("日記", "2020-01-01.txt", "むかしの日記。\n\n本文です。\n")
        box.put("日記", "2099-01-01.txt", "未来の日記。\n\n本文です。\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertIn("より前の日付です", result.stdout)
        self.assertIn("未来の日付です", result.stdout)
        self.assertEqual(before, box.posts())

    def test_30_unsafe_category_name(self):
        """URL に使えないカテゴリー名は登録も取り込みもしない。"""
        box = self.sandbox()
        before = box.posts()
        box.put("posts", "2026-11-02.txt", "2026年11月02日\n\n本文です。\n")
        result = run(box.blog, "import_txt.py", "--新カテゴリー許可")
        self.assertOK(result)
        self.assertIn("登録できません", result.stdout)
        self.assertEqual(before, box.posts())
        outside = os.path.join(box.root, "2026-11-02.html")
        self.assertFalse(os.path.exists(outside), "フォルダの外に記事ができました")

    def test_31_force_cannot_touch_a_frozen_article(self):
        """公開済みとして記録された記事は --force でも上書きされない。"""
        box = self.sandbox()
        target = box.blog_path("content", "posts", "2026-10-30.html")

        def digest():
            with open(target, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()

        before = digest()
        box.put("日記", "2026年10月30日.txt", "2026年10月30日\n\nまったく別の本文に差し替える。\n")
        result = run(box.blog, "import_txt.py", "--force")
        self.assertOK(result)
        self.assertIn("公開済みとして記録されている記事です", result.stdout)
        self.assertIn("--修正許可", result.stdout)
        self.assertEqual(before, digest(), "公開済みの記事が書き換えられました")


# ----------------------------------------------------------- 異常系（検査）

class CheckCatches(Base):
    """公開物のおかしなところを、検査が見つけること。"""

    def test_40_changed_article_body(self):
        box = self.sandbox()
        text = box.read("content", "posts", "2026-08-06.html")
        box.write(text.replace("今日は疲れた。", "書き換えた。"),
                  "content", "posts", "2026-08-06.html")
        self.assertOK(run(box.blog, "build.py"))
        self.assertNG(run(box.blog, "check.py"),
                      "公開済みの記事が変わっています", "/posts/2026-08-06/")

    def test_41_deleted_article_and_redirect(self):
        box = self.sandbox()
        os.remove(box.blog_path("content", "posts", "2026-08-08.html"))
        self.assertOK(run(box.blog, "build.py"))
        result = run(box.blog, "check.py")
        self.assertNG(result, "公開済みの記事が無くなっています")
        self.assertIn("旧URLからの転送が無くなっています", result.stdout)

    def test_42_orphan_file_in_docs(self):
        box = self.sandbox()
        os.makedirs(box.blog_path("docs", "古いページ"), exist_ok=True)
        box.write("<html>のこりもの</html>", "docs", "古いページ", "index.html")
        self.assertNG(run(box.blog, "check.py"),
                      "もう作られないファイルが公開されたままです")

    def test_43_hand_edited_docs(self):
        box = self.sandbox()
        text = box.read("docs", "index.html")
        box.write(text.replace("</main>", "<p>手で足した</p></main>"), "docs", "index.html")
        self.assertNG(run(box.blog, "check.py"),
                      "公開物が、記事から作り直したものと違います")

    def test_44_changed_stylesheet(self):
        box = self.sandbox()
        with open(box.blog_path("docs", "assets", "style.css"), "a", encoding="utf-8") as f:
            f.write("\n/* 手で足した */\n")
        self.assertNG(run(box.blog, "check.py"),
                      "手で用意した公開ファイルの中身が変わっています")

    def test_45_secret_files_are_found(self):
        box = self.sandbox()
        with open(box.blog_path("export-2026.xml"), "w", encoding="utf-8") as f:
            f.write("<xml/>\n")
        with open(box.blog_path("token.json"), "w", encoding="utf-8") as f:
            f.write("{}\n")
        box.make_git()
        result = run(box.blog, "check.py", "--衛生のみ")
        self.assertNG(result, "export-2026.xml", "token.json")

    def test_46_broken_front_matter_is_explained(self):
        """必要な項目が欠けていても、日本語で知らせて落ちないこと。"""
        box = self.sandbox()
        box.write("---\ntitle: こわれた記事\ndate: 2026-11-02 21:00:00\n"
                  "categories: [日記]\ntags: []\n---\n\n<p>本文</p>\n",
                  "content", "posts", "broken.html")
        result = run(box.blog, "check.py", "--記事のみ")
        self.assertNG(result, "slug がありません")
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_47_dropped_front_matter_line(self):
        """「title:A」のように書き間違えた行を、黙って捨てないこと。"""
        box = self.sandbox()
        box.write("---\ntitle:こわれた\ndate: 2026-11-02 21:00:00\nslug: broken2\n"
                  "categories: [日記]\ntags: []\n---\n\n<p>本文</p>\n",
                  "content", "posts", "broken2.html")
        self.assertNG(run(box.blog, "check.py", "--記事のみ"),
                      "読み取れなかった行があります")

    def test_48_empty_body_article(self):
        box = self.sandbox()
        box.write("---\ntitle: 空の記事\ndate: 2026-11-02 21:00:00\nslug: empty-one\n"
                  "categories: [日記]\ntags: []\n---\n\n",
                  "content", "posts", "empty-one.html")
        self.assertNG(run(box.blog, "check.py", "--記事のみ"), "本文が空の記事があります")

    def test_49_unknown_category(self):
        box = self.sandbox()
        text = box.read("content", "posts", "2026-08-06.html")
        box.write(text.replace("categories: [日記]", "categories: [デスクトップ]"),
                  "content", "posts", "2026-08-06.html")
        self.assertNG(run(box.blog, "check.py", "--記事のみ"),
                      "登録されていないカテゴリー「デスクトップ」")

    def test_50_wrong_encoding_is_explained(self):
        """UTF-8 でない記事ファイルを、トレースバックではなく案内で知らせること。"""
        box = self.sandbox()
        path = box.blog_path("content", "posts", "cp932.html")
        with open(path, "wb") as f:
            f.write(("---\ntitle: 文字コードの試し\ndate: 2026-11-02 21:00:00\n"
                     "slug: cp932-test\ncategories: [日記]\ntags: []\n---\n\n"
                     "<p>日本語の本文</p>\n").encode("cp932"))
        result = run(box.blog, "check.py", "--記事のみ")
        self.assertNG(result, "文字コードが UTF-8 ではありません",
                      "content/posts/cp932.html")
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_51_slug_date_must_match_diary_date(self):
        """URL の日付と日記の日付のずれを見つけること。"""
        box = self.sandbox()
        text = box.read("content", "posts", "2026-08-06.html")
        box.write(text.replace("diary_date: 2026-08-06", "diary_date: 2026-08-05"),
                  "content", "posts", "2026-08-06.html")
        self.assertNG(run(box.blog, "check.py", "--記事のみ"),
                      "の URL の日付（2026-08-06）と日記の日付（2026-08-05）が合っていません")

    def test_52_duplicate_slug(self):
        box = self.sandbox()
        text = box.read("content", "posts", "2026-08-06.html")
        box.write(text, "content", "posts", "copy-of-2026-08-06.html")
        self.assertNG(run(box.blog, "check.py", "--記事のみ"), "URL が重複しています")


# ----------------------------------------------------------- 異常系（組み立て）

class BuildGuards(Base):
    """公開物を作るときに、危ない書き出しを止めること。"""

    def test_60_slug_escaping_docs_is_refused(self):
        """公開フォルダの外へ出る URL は、1ファイルも書かずに中断すること。"""
        box = self.sandbox()
        before = box.docs()
        text = box.read("content", "posts", "2026-08-06.html")
        box.write(text.replace("slug: 2026-08-06", "slug: ../../逃げ出す"),
                  "content", "posts", "2026-08-06.html")
        result = run(box.blog, "build.py")
        self.assertNG(result, "中断します", "URL が使えません")
        self.assertEqual(before, box.docs(), "中断したのに公開物が変わりました")
        self.assertFalse(os.path.exists(os.path.join(box.root, "逃げ出す")))

    def test_61_backslash_in_slug_is_refused(self):
        """円記号を含む URL は、動かす場所によらず止めること。"""
        box = self.sandbox()
        text = box.read("content", "posts", "2026-08-06.html")
        box.write(text.replace("slug: 2026-08-06", "slug: ..\\\\逃げ出す"),
                  "content", "posts", "2026-08-06.html")
        self.assertNG(run(box.blog, "build.py"), "中断します")

    def test_62_broken_date_is_refused_before_writing(self):
        """投稿日が壊れていたら、書き始める前に中断すること。"""
        box = self.sandbox()
        before = box.docs()
        text = box.read("content", "posts", "2026-08-06.html")
        box.write(text.replace("date: 2026-08-07 00:13:34", "date: きのう"),
                  "content", "posts", "2026-08-06.html")
        result = run(box.blog, "build.py")
        self.assertNG(result, "中断します", "投稿日の形が違います")
        self.assertEqual(before, box.docs(), "中断したのに公開物が変わりました")
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_63_two_redirects_to_the_same_folder(self):
        """同じ場所へ2回書こうとしたら、黙って上書きせずに中断すること。"""
        box = self.sandbox()
        text = box.read("content", "posts", "2026-08-08.html")
        other = box.read("content", "posts", "2026-08-06.html")
        legacy = [l for l in other.split("\n") if l.startswith("legacy_slug:")][0]
        box.write("\n".join(l if not l.startswith("legacy_slug:") else legacy
                            for l in text.split("\n")),
                  "content", "posts", "2026-08-08.html")
        result = run(box.blog, "build.py")
        self.assertNG(result, "同じ場所へ2回書き出そうとしました")
        self.assertIn("旧URL", result.stdout)

    def test_64_reserved_legacy_name_is_skipped(self):
        """旧URLがサイトの使う名前と同じなら、転送ページを作らず知らせること。"""
        box = self.sandbox()
        text = box.read("content", "posts", "2026-08-08.html")
        box.write("\n".join(("legacy_slug: assets" if l.startswith("legacy_slug:") else l)
                            for l in text.split("\n")),
                  "content", "posts", "2026-08-08.html")
        result = run(box.blog, "build.py")
        self.assertOK(result)
        self.assertIn("転送ページのフォルダ名に使えません", result.stdout)
        self.assertTrue(os.path.isfile(box.blog_path("docs", "assets", "style.css")),
                        "スタイルが転送ページに上書きされました")

    def test_65_braces_in_body_are_kept(self):
        """本文に書いた {{...}} が消えないこと。"""
        box = self.sandbox()
        text = box.read("content", "posts", "2026-08-06.html")
        box.write(text.replace("<p>今日は疲れた。", "<p>型は {{title}} と書く。今日は疲れた。"),
                  "content", "posts", "2026-08-06.html")
        self.assertOK(run(box.blog, "build.py"))
        page = box.read("docs", "posts", "2026-08-06", "index.html")
        self.assertIn("{{title}}", page, "本文の {{title}} が消えました")


# ----------------------------------------------------------- 公開の手前

class PublishGuards(Base):
    """公開の前に止めること。"""

    def test_70_publish_stops_on_a_changed_article(self):
        box = self.sandbox()
        box.make_git()
        text = box.read("content", "posts", "2026-08-06.html")
        box.write(text.replace("今日は疲れた。", "書き換えた。"),
                  "content", "posts", "2026-08-06.html")
        self.assertOK(run(box.blog, "build.py"))
        result = run(box.blog, "publish.py", stdin="\n")
        self.assertNG(result, "自動検査で問題が見つかりました",
                      "公開済みの記事が変わっています")
        self.assertIn("python tools/check.py", result.stdout)

    def test_71_publish_passes_the_check_on_a_normal_post(self):
        box = self.sandbox()
        box.make_git()
        box.put("日記", "2026年11月02日.txt", NEW_POST)
        self.assertOK(run(box.blog, "import_txt.py"))
        self.assertOK(run(box.blog, "build.py"))
        self.assertOK(run(box.blog, "baseline.py", "--update", "--はい"))
        result = run(box.blog, "publish.py", stdin="\n")
        self.assertIn("自動検査を通りました", result.stdout)
        self.assertIn("送信されるファイル", result.stdout)
        self.assertNotIn("公開してはいけないファイル", result.stdout)
        self.assertIn("中断しました", result.stdout)

    def test_72_publish_shows_japanese_paths(self):
        """日本語のファイル名が、読める形で画面に出ること。"""
        box = self.sandbox()
        box.make_git()
        box.write("<html>てすと</html>", "docs", "10月3日", "index.html")
        result = run(box.blog, "publish.py", "--検査を省く", stdin="\n")
        self.assertIn("docs/10月3日/index.html", result.stdout)
        self.assertNotIn("\\346", result.stdout)


# ----------------------------------------------- クラウドからの投稿（V02）

CLOUD_POST = (
    "# クラウドからの投稿\n"
    "タグ: 仕組み\n"
    "\n"
    "スマホから送ったつもりの文章です。\n"
    "空行で段落が分かれることも確かめます。\n"
    "\n"
    "二段落目。**太字**も使えます。\n"
)


def issue_form(body="スマホの投稿フォームから送ったつもりの文章です。\n"
                    "二行目も書いておきます。",
               day="", title="", category="", tags="", confirm=True):
    """GitHub の投稿フォームが作る本文と同じ形を組み立てる。

    空欄のときに GitHub が入れる「_No response_」も、そのまま再現する。
    """
    def value(v):
        return v if v else "_No response_"
    mark = "- [X]" if confirm else "- [ ]"
    return "\n".join([
        "### 本文", "", body, "",
        "### 日付", "", value(day), "",
        "### タイトル", "", value(title), "",
        "### カテゴリー", "", value(category), "",
        "### タグ", "", value(tags), "",
        "### 公開の確認", "",
        "%s この内容を公開してよい（送った時点で第三者に読まれ、取り消せないことを分かっています）"
        % mark,
        "",
    ])


class CloudPosting(Base):
    """スマホ・クラウドからの投稿が、正しく記事になること。"""

    # ---- inbox に置いた TXT ----

    def test_80_inbox_txt_becomes_an_article(self):
        """inbox の TXT が、既定のカテゴリーの記事になること。"""
        box = self.sandbox()
        box.put_inbox("2026-11-03.txt", CLOUD_POST)
        result = run(box.blog, "import_inbox.py")
        self.assertOK(result)
        self.assertIn("/posts/2026-11-03/", result.stdout)
        meta, body = common.parse_front_matter(
            box.read("content", "posts", "2026-11-03.html"))
        entry = meta
        self.assertEqual(entry["title"], "クラウドからの投稿")
        self.assertEqual(entry["date"], "2026-11-03 21:00:00")
        self.assertEqual(entry["slug"], "2026-11-03")
        self.assertEqual(entry["diary_date"], "2026-11-03")
        self.assertEqual(entry["categories"], ["日記"])
        self.assertEqual(entry["tags"], ["仕組み"])
        self.assertEqual(entry["source"], "inbox")
        self.assertEqual(entry["source_file"], "2026-11-03.txt")
        self.assertIn("<strong>太字</strong>", body)
        self.assertFalse(box.inbox_has("2026-11-03.txt"),
                         "取り込んだあとも inbox に TXT が残っています")

    def test_81_inbox_category_folder_decides_the_category(self):
        """inbox のフォルダ名が、そのままカテゴリーになること。"""
        box = self.sandbox()
        box.put_inbox("2026-11-03.txt", CLOUD_POST, category="感想")
        result = run(box.blog, "import_inbox.py")
        self.assertOK(result)
        self.assertIn("/posts/review/2026-11-03/", result.stdout)
        entry = common.parse_front_matter(
            box.read("content", "posts", "review", "2026-11-03.html"))[0]
        self.assertEqual(entry["categories"], ["感想"])
        self.assertEqual(entry["slug"], "review/2026-11-03")

    def test_82_inbox_can_keep_the_txt(self):
        """--残す を付けると、取り込んだ TXT が inbox に残ること。"""
        box = self.sandbox()
        box.put_inbox("2026-11-03.txt", CLOUD_POST)
        self.assertOK(run(box.blog, "import_inbox.py", "--残す"))
        self.assertTrue(box.inbox_has("2026-11-03.txt"))

    def test_83_inbox_dry_run_writes_nothing(self):
        """--dry-run では、記事も作らず TXT も消さないこと。"""
        box = self.sandbox()
        before = box.posts()
        box.put_inbox("2026-11-03.txt", CLOUD_POST)
        result = run(box.blog, "import_inbox.py", "--dry-run")
        self.assertOK(result)
        self.assertIn("確認のみ", result.stdout)
        self.assertEqual(before, box.posts())
        self.assertTrue(box.inbox_has("2026-11-03.txt"))

    def test_84_inbox_refuses_an_unknown_category(self):
        """登録されていないカテゴリーのフォルダは取り込まないこと。"""
        box = self.sandbox()
        before = box.posts()
        box.put_inbox("2026-11-03.txt", CLOUD_POST, category="知らないぶんるい")
        result = run(box.blog, "import_inbox.py")
        self.assertNG(result, "登録されていないカテゴリー")
        self.assertEqual(before, box.posts())
        self.assertTrue(box.inbox_has("2026-11-03.txt", "知らないぶんるい"),
                        "取り込まなかったのに TXT が消えました")

    def test_85_inbox_refuses_a_name_without_a_date(self):
        """ファイル名に日付が無いものは取り込まないこと。"""
        box = self.sandbox()
        before = box.posts()
        box.put_inbox("きょうのにっき.txt", CLOUD_POST)
        result = run(box.blog, "import_inbox.py")
        self.assertNG(result, "日付")
        self.assertEqual(before, box.posts())

    def test_86_inbox_refuses_a_duplicate_body(self):
        """すでにある記事と本文が同じなら取り込まないこと。"""
        box = self.sandbox()
        box.put_inbox("2026-11-03.txt", CLOUD_POST)
        self.assertOK(run(box.blog, "import_inbox.py"))
        after_first = box.posts()
        box.put_inbox("2026-11-04.txt", CLOUD_POST + "\n\n")
        result = run(box.blog, "import_inbox.py")
        self.assertNG(result, "すでにある記事と本文が同じです")
        self.assertEqual(after_first, box.posts(), "同じ本文の記事が増えました")

    def test_87_inbox_refuses_the_same_name_twice(self):
        """同じ名前で中身が違うものは、確かめずに増やさないこと。"""
        box = self.sandbox()
        box.put_inbox("2026-11-03.txt", CLOUD_POST)
        self.assertOK(run(box.blog, "import_inbox.py"))
        after_first = box.posts()
        box.put_inbox("2026-11-03.txt",
                      "# 書き直し\n\nまったく別の本文にしてみます。\n")
        result = run(box.blog, "import_inbox.py")
        self.assertNG(result, "同じ名前で取り込み済みです")
        self.assertEqual(after_first, box.posts())

    def test_88_inbox_refuses_a_manual_name(self):
        """説明書・メモらしい名前は取り込まないこと。"""
        box = self.sandbox()
        before = box.posts()
        box.put_inbox("2026-11-03-使い方メモ.txt", CLOUD_POST)
        result = run(box.blog, "import_inbox.py")
        self.assertNG(result, "説明や覚え書きの可能性")
        self.assertEqual(before, box.posts())

    def test_89_inbox_refuses_dates_outside_the_blog(self):
        """ブログの開始前・未来の日付は取り込まないこと。"""
        box = self.sandbox()
        before = box.posts()
        box.put_inbox("2020-01-01.txt", CLOUD_POST)
        self.assertNG(run(box.blog, "import_inbox.py"), "より前の日付です")
        box.put_inbox("2099-01-01.txt", CLOUD_POST)
        self.assertNG(run(box.blog, "import_inbox.py"), "未来の日付です")
        self.assertEqual(before, box.posts())

    def test_90_inbox_never_overwrites_a_published_article(self):
        """公開済みの日付と同じ日に置いても、その記事を書き換えないこと。"""
        box = self.sandbox()
        target = box.blog_path("content", "posts", "2026-10-30.html")
        with open(target, "rb") as f:
            before = hashlib.sha256(f.read()).hexdigest()
        box.put_inbox("2026-10-30.txt",
                      "# 同じ日にもう1本\n\n同じ日に書いた、別の日記のつもりです。\n")
        result = run(box.blog, "import_inbox.py")
        self.assertOK(result)
        self.assertIn("/posts/2026-10-30-2/", result.stdout)
        with open(target, "rb") as f:
            self.assertEqual(before, hashlib.sha256(f.read()).hexdigest(),
                             "公開済みの記事が書き換えられました")

    def test_91_inbox_leaves_existing_articles_untouched(self):
        """inbox から取り込んでも、既存の記事と公開物が変わらないこと。"""
        box = self.sandbox()
        before_posts, before_docs = box.posts(), box.docs()
        box.put_inbox("2026-11-03.txt", CLOUD_POST)
        self.assertOK(run(box.blog, "import_inbox.py"))
        self.assertOK(run(box.blog, "build.py"))
        after_posts, after_docs = box.posts(), box.docs()
        for rel, digest in before_posts.items():
            self.assertIn(rel, after_posts, "既存の記事が消えました: %s" % rel)
            self.assertEqual(digest, after_posts[rel],
                             "既存の記事が変わりました: %s" % rel)
        for rel in before_docs:
            self.assertIn(rel, after_docs, "公開物が消えました: %s" % rel)
        self.assertEqual(len(after_posts), len(before_posts) + 1)

    def test_92_cloud_post_passes_the_whole_check(self):
        """inbox から取り込んだあと、組み立てと総合検査が通ること。"""
        box = self.sandbox()
        box.make_git()
        box.put_inbox("2026-11-03.txt", CLOUD_POST)
        self.assertOK(run(box.blog, "import_inbox.py"))
        self.assertOK(run(box.blog, "build.py"))
        checked = run(box.blog, "check.py")
        self.assertOK(checked)
        self.assertIn("新しい記事", checked.stdout)
        self.assertOK(run(box.blog, "verify.py"))

    def test_93_inbox_and_txt_share_the_duplicate_guard(self):
        """inbox で入れた本文を、10_記事管理 から入れ直せないこと。"""
        box = self.sandbox()
        box.put_inbox("2026-11-03.txt", CLOUD_POST)
        self.assertOK(run(box.blog, "import_inbox.py"))
        after_first = box.posts()
        box.put("日記", "2026年11月04日.txt", CLOUD_POST + "\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        self.assertIn("すでにある記事と本文が同じです", result.stdout)
        self.assertEqual(after_first, box.posts())

    # ---- 投稿フォーム（Issue） ----

    def test_94_issue_form_becomes_an_article(self):
        """投稿フォームの本文が、指定どおりの記事になること。"""
        box = self.sandbox()
        body = issue_form(body="フォームから送ったつもりの文章です。\n二行目です。",
                          day="2026-11-03", title="フォームからの投稿",
                          category="感想", tags="仕組み, 覚え")
        result = run(box.blog, "post_from_issue.py",
                     env={"ISSUE_BODY": body, "ISSUE_NUMBER": "12"})
        self.assertOK(result)
        self.assertIn("/posts/review/2026-11-03/", result.stdout)
        entry = common.parse_front_matter(
            box.read("content", "posts", "review", "2026-11-03.html"))[0]
        self.assertEqual(entry["title"], "フォームからの投稿")
        self.assertEqual(entry["diary_date"], "2026-11-03")
        self.assertEqual(entry["categories"], ["感想"])
        self.assertEqual(entry["tags"], ["仕組み", "覚え"])
        self.assertEqual(entry["source"], "issue")
        self.assertEqual(entry["source_ref"], "issue-12")
        self.assertIn("2026-11-03", entry["source_file"])

    def test_95_issue_form_fills_in_what_is_left_blank(self):
        """日付・題・カテゴリーを空にしても、きちんと決まること。"""
        box = self.sandbox()
        result = run(box.blog, "post_from_issue.py",
                     env={"ISSUE_BODY": issue_form(), "ISSUE_NUMBER": "13"})
        self.assertOK(result)
        posts = [p for p in box.posts() if p not in Sandbox.__dict__]
        new = sorted(set(box.posts()) - set(tree_digest(
            os.path.join(TEMPLATE, "blog", "content", "posts"), only_ext=".html")))
        self.assertEqual(len(new), 1, "新しい記事が1件ではありません: %r" % new)
        entry = common.parse_front_matter(
            box.read("content", "posts", new[0]))[0]
        self.assertEqual(entry["categories"], ["日記"])
        self.assertEqual(entry["diary_date"], entry["slug"].split("/")[-1][:10])
        self.assertTrue(entry["title"].endswith("日"),
                        "題が日付の形になっていません: %r" % entry["title"])

    def test_96_issue_form_needs_the_confirmation(self):
        """「公開の確認」に印が無いものは取り込まないこと。"""
        box = self.sandbox()
        before = box.posts()
        result = run(box.blog, "post_from_issue.py",
                     env={"ISSUE_BODY": issue_form(confirm=False),
                          "ISSUE_NUMBER": "14"})
        self.assertNG(result, "公開の確認")
        self.assertEqual(before, box.posts())

    def test_97_issue_form_refuses_a_free_form_issue(self):
        """フォームを使わずに書かれた Issue は取り込まないこと。"""
        box = self.sandbox()
        before = box.posts()
        result = run(box.blog, "post_from_issue.py",
                     env={"ISSUE_BODY": "ちょっと相談があります。",
                          "ISSUE_NUMBER": "15"})
        self.assertNG(result, "投稿フォームの形")
        self.assertEqual(before, box.posts())

    def test_98_issue_form_keeps_dangerous_text_as_text(self):
        """命令や HTML の形をした文字が、そのまま効いてしまわないこと。"""
        box = self.sandbox()
        danger = ("$(echo abc) `echo def` <script>alert(1)</script> "
                  "& < > ${IFS} | ; 改行のあとも続きます。")
        result = run(box.blog, "post_from_issue.py",
                     env={"ISSUE_BODY": issue_form(body=danger, day="2026-11-03"),
                          "ISSUE_NUMBER": "16"})
        self.assertOK(result)
        body = common.parse_front_matter(
            box.read("content", "posts", "2026-11-03.html"))[1]
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertIn("$(echo abc)", body)
        self.assertIn("&amp;", body)

    def test_99_issue_form_is_not_imported_twice(self):
        """同じ Issue からは、本文を直しても二度取り込まないこと。"""
        box = self.sandbox()
        self.assertOK(run(box.blog, "post_from_issue.py",
                          env={"ISSUE_BODY": issue_form(day="2026-11-03"),
                               "ISSUE_NUMBER": "17"}))
        after_first = box.posts()
        result = run(box.blog, "post_from_issue.py",
                     env={"ISSUE_BODY": issue_form(body="書き直した本文です。",
                                                   day="2026-11-04"),
                          "ISSUE_NUMBER": "17"})
        self.assertNG(result, "取り込み済み")
        self.assertEqual(after_first, box.posts())

    def test_100_issue_form_refuses_an_unknown_category(self):
        """登録されていないカテゴリーは、勝手に作らないこと。"""
        box = self.sandbox()
        before = box.posts()
        result = run(box.blog, "post_from_issue.py",
                     env={"ISSUE_BODY": issue_form(category="知らないぶんるい"),
                          "ISSUE_NUMBER": "18"})
        self.assertNG(result, "登録されていないカテゴリー")
        self.assertEqual(before, box.posts())
        config = json.loads(box.read("config.json"))
        self.assertNotIn("知らないぶんるい", config["categories"],
                         "カテゴリーが勝手に登録されました")

    def test_101_issue_form_refuses_a_broken_date(self):
        """読めない日付・ありえない日付は取り込まないこと。"""
        box = self.sandbox()
        before = box.posts()
        self.assertNG(run(box.blog, "post_from_issue.py",
                          env={"ISSUE_BODY": issue_form(day="きのう"),
                               "ISSUE_NUMBER": "19"}), "日付")
        self.assertNG(run(box.blog, "post_from_issue.py",
                          env={"ISSUE_BODY": issue_form(day="2026-13-40"),
                               "ISSUE_NUMBER": "20"}), "日付")
        self.assertEqual(before, box.posts())

    def test_102_issue_form_keeps_unknown_headings_in_the_body(self):
        """本文の中の「### なにか」が、欄の区切りと取り違えられないこと。"""
        box = self.sandbox()
        body = "はじめの行です。\n\n### これは見出しのつもり\n\nそのあとの行です。"
        result = run(box.blog, "post_from_issue.py",
                     env={"ISSUE_BODY": issue_form(body=body, day="2026-11-03"),
                          "ISSUE_NUMBER": "21"})
        self.assertOK(result)
        body = common.parse_front_matter(
            box.read("content", "posts", "2026-11-03.html"))[1]
        self.assertIn("これは見出しのつもり", body)
        self.assertIn("そのあとの行です", body)

    def test_103_issue_form_passes_the_whole_check(self):
        """投稿フォームから入れたあと、組み立てと総合検査が通ること。"""
        box = self.sandbox()
        box.make_git()
        self.assertOK(run(box.blog, "post_from_issue.py",
                          env={"ISSUE_BODY": issue_form(day="2026-11-03"),
                               "ISSUE_NUMBER": "22"}))
        self.assertOK(run(box.blog, "build.py"))
        self.assertOK(run(box.blog, "check.py"))
        self.assertOK(run(box.blog, "verify.py"))

    def test_104_issue_form_does_not_touch_a_published_article(self):
        """公開済みの日付を指定しても、その記事を書き換えないこと。"""
        box = self.sandbox()
        target = box.blog_path("content", "posts", "2026-10-30.html")
        with open(target, "rb") as f:
            before = hashlib.sha256(f.read()).hexdigest()
        self.assertOK(run(box.blog, "post_from_issue.py",
                          env={"ISSUE_BODY": issue_form(day="2026-10-30"),
                               "ISSUE_NUMBER": "23"}))
        with open(target, "rb") as f:
            self.assertEqual(before, hashlib.sha256(f.read()).hexdigest(),
                             "公開済みの記事が書き換えられました")

    def test_105_a_category_with_shell_characters_stays_text(self):
        """シェルで意味を持つ文字を含むカテゴリーでも、文字のまま扱うこと。

        カテゴリー名はフォルダ名になり、そのまま記事の URL にもなる。
        rules.safe_url_name が止めるのは改行と \\ / : * ? " < > | だけなので、
        $ ( ) ` ; & ' を含む名前は登録できてしまう。
        登録できてしまう以上、そのあとの書き出し・組み立て・検査で
        「ただの文字」として扱われ、コマンドとして動かないことを確かめる。

        ワークフロー側で slug を env: 経由にしているのは、このため。
        その決まりは test_119 と test_120 が見張っている。
        """
        box = self.sandbox()
        box.make_git()
        # 前後を普通の文字にしてあるのは、名前の端の扱い（末尾の「.」や
        # 空白など）が OS で違うのを、この試験に持ち込まないため。
        name = "試し$(echo A)`echo B`;&\'終"
        config = json.loads(box.read("config.json"))
        config["categories"].append(name)
        box.write(json.dumps(config, ensure_ascii=False, indent=1),
                  "config.json")
        box.put(name, "2026-11-03.txt", "特殊文字のカテゴリーの試しです。\n")

        self.assertOK(run(box.blog, "import_txt.py"))

        slug = "%s/2026-11-03" % name
        self.assertTrue(
            os.path.exists(box.blog_path("content", "posts", slug + ".html")),
            "記事が /posts/%s/ に書き出されていません" % slug)
        meta = common.parse_front_matter(
            box.read("content", "posts", slug + ".html"))[0]
        self.assertEqual(slug, meta["slug"],
                         "slug が文字のまま保たれていません")
        self.assertEqual([name], meta["categories"],
                         "カテゴリー名が文字のまま保たれていません")

        # 名前が「実行された跡」になっていないこと。$(echo A) が動いていれば
        # A になり、`echo B` が動いていれば B になる。
        self.assertIn("$(echo A)", meta["slug"])
        self.assertIn("`echo B`", meta["slug"])

        self.assertOK(run(box.blog, "build.py"))
        self.assertOK(run(box.blog, "check.py"))
        self.assertOK(run(box.blog, "verify.py"))


# ------------------------------------- 投稿の仕組みそのものの決まり（V02）

def read_root(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


POSTING_WORKFLOWS = ("post-from-issue.yml", "post-from-inbox.yml")
WORKFLOW_DIR = os.path.join(ROOT, ".github", "workflows")


def run_lines(text):
    """run: のブロックに入っている行を (行番号, 行) で返す。

    行頭の字下げで、ブロックの終わりを見分ける。
    コメントの行（# で始まる行）は返さない。
    """
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if re.match(r"^(?:-\s+)?run:\s+(?![|>])(.+)$", stripped):
            out.append((i + 1, line))
            i += 1
            continue
        if re.match(r"^(?:-\s+)?run:\s*(?:\|-?|>-?)?\s*$", stripped):
            indent = len(line) - len(line.lstrip())
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                if not nxt.lstrip().startswith("#"):
                    out.append((i + 1, nxt))
                i += 1
            continue
        i += 1
    return out


class CloudSafety(unittest.TestCase):
    """投稿のワークフローとフォームが、危ない形になっていないこと。

    ここは文字として読むだけの点検。GitHub Actions の実行環境に
    PyYAML が無くても動くよう、正規表現で見ている。
    """

    def workflow(self, name):
        return read_root(".github", "workflows", name)

    def test_110_posting_workflows_never_push_to_main(self):
        """投稿のワークフローが main へ push しないこと。"""
        for name in POSTING_WORKFLOWS:
            text = self.workflow(name)
            with self.subTest(name=name):
                self.assertNotRegex(
                    text, r"git push[^\n]*\bmain\b",
                    "%s が main へ push しようとしています" % name)
                self.assertNotRegex(
                    text, r"gh pr merge",
                    "%s がプルリクエストを自動で取り込もうとしています" % name)
                self.assertIn("DEFAULT_BRANCH", text,
                              "%s に既定ブランチの確認がありません" % name)

    def test_111_issue_body_is_never_run_as_a_command(self):
        """Issue の本文を run: に直接書いていないこと（コマンド差し込みの防止）。"""
        for name in POSTING_WORKFLOWS + ("post-rehearsal.yml", "check.yml"):
            text = self.workflow(name)
            in_run = False
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("run:") or stripped in ("run: |", "run: >"):
                    in_run = True
                    continue
                if stripped.startswith("- name:") or stripped.startswith("- uses:"):
                    in_run = False
                if in_run and "${{" in line:
                    self.assertNotIn(
                        "github.event.issue.body", line,
                        "%s: 投稿の本文を run: の中に書いています: %s" % (name, stripped))
                    self.assertNotIn(
                        "github.event.issue.title", line,
                        "%s: 投稿の題を run: の中に書いています: %s" % (name, stripped))
            # 実行の名前（run-name）に本文を入れると、ログインしていない
            # 第三者にも読める一覧に出てしまう。使っていないことを確かめる。
            self.assertNotRegex(text, r"(?m)^run-name:",
                                "%s が run-name を使っています" % name)
            # 画面の記録（ステップサマリー）にも本文を書かない。
            self.assertNotIn("GITHUB_STEP_SUMMARY\" <<", text)

    def test_112_posting_workflows_only_run_for_the_owner(self):
        """ほかの人の投稿では動かないこと。"""
        issue = self.workflow("post-from-issue.yml")
        self.assertIn("github.event.issue.user.login == github.repository_owner",
                      issue, "投稿フォームが、持ち主以外にも開いています")
        inbox = self.workflow("post-from-inbox.yml")
        self.assertIn("github.actor == github.repository_owner",
                      inbox, "inbox が、持ち主以外にも開いています")

    def test_113_posting_workflows_ask_for_least_permissions(self):
        """既定の権限が読み取りだけで、必要な分だけを job に足していること。"""
        for name in POSTING_WORKFLOWS + ("post-rehearsal.yml",):
            text = self.workflow(name)
            with self.subTest(name=name):
                self.assertRegex(text, r"(?m)^permissions:\n  contents: read\n",
                                 "%s の既定の権限が読み取りだけではありません" % name)
        rehearsal = self.workflow("post-rehearsal.yml")
        self.assertNotIn("contents: write", rehearsal,
                         "予行演習に書き込みの権限が付いています")

    def test_114_posting_workflows_do_not_update_the_baseline(self):
        """公開済みの記録を、自動で作り直してしまわないこと。

        案内の文（echo）の中に書いてあるのはよい。実際に実行する行、
        つまり行の先頭が python で始まるものだけを見る。
        """
        for name in POSTING_WORKFLOWS + ("post-rehearsal.yml", "check.yml"):
            text = self.workflow(name)
            with self.subTest(name=name):
                self.assertNotRegex(
                    text, r"(?m)^\s*python[^\n]*baseline\.py[^\n]*--update",
                    "%s が baseline を自動で作り直そうとしています" % name)

    def test_115_issue_form_categories_are_registered(self):
        """投稿フォームの選択肢が、すべて登録済みのカテゴリーであること。"""
        text = read_root(".github", "ISSUE_TEMPLATE", "post.yml")
        block = re.search(r"(?m)^      options:\n((?:^        - [^\n]+\n)+)", text)
        self.assertIsNotNone(block, "カテゴリーの選択肢が見つかりません")
        options = [line.strip()[2:].strip()
                   for line in block.group(1).split("\n") if line.strip()]
        allowed = rules.allowed_categories(common.load_config())
        for name in options:
            self.assertIn(name, allowed,
                          "投稿フォームの「%s」は config.json に登録されていません" % name)

    def test_116_issue_form_labels_match_the_reader(self):
        """フォームの見出しと、読み取り側の目印がそろっていること。"""
        text = read_root(".github", "ISSUE_TEMPLATE", "post.yml")
        labels = set(re.findall(r"(?m)^      label: (.+)$", text))
        labels |= set(re.findall(r"(?m)^      label: (.+)$", text))
        reader = read_root("tools", "post_from_issue.py")
        fields = set(re.findall(r'(?m)^FIELD_\w+ = "(.+)"$', reader))
        for field in fields:
            self.assertIn(field, labels,
                          "読み取り側の「%s」が、フォームの見出しにありません" % field)

    def test_117_the_issue_form_warns_about_the_public_repository(self):
        """公開リポジトリであることの注意が、フォームに書いてあること。"""
        text = read_root(".github", "ISSUE_TEMPLATE", "post.yml")
        self.assertIn("公開リポジトリ", text)
        self.assertIn("required: true", text,
                      "公開の確認が必須になっていません")

    def test_118_inbox_is_explained(self):
        """inbox に、置き方と公開の注意が書いてあること。"""
        text = read_root("inbox", "README.md")
        self.assertIn("公開リポジトリ", text)
        self.assertIn("inbox/2026-11-02.txt", text)

    def test_119_no_workflow_expression_inside_run(self):
        """run: の中に ${{ }} を書いていないこと。

        ${{ }} は、シェルが動く前に「文字」として差し替えられる。
        中身に $( ) やバッククォートが入っていれば、それがそのまま
        コマンドとして動いてしまう。
        だから run: の中では一度も使わず、env: で環境変数に受けてから
        使う、という決まりにしている。例外を作らないほうが、ここで
        機械的に見張れる。
        """
        names = sorted(n for n in os.listdir(WORKFLOW_DIR)
                       if n.endswith((".yml", ".yaml")))
        self.assertTrue(names, "ワークフローが見つかりません")
        for name in names:
            for num, line in run_lines(self.workflow(name)):
                with self.subTest(name=name, line=num):
                    self.assertNotIn(
                        "${{", line,
                        "%s の %d 行目: run: の中に ${{ }} があります。"
                        "env: で環境変数に受けてから使ってください → %s"
                        % (name, num, line.strip()))

    def test_120_step_results_are_passed_through_env(self):
        """前の段の結果は、env: で環境変数に受けていること。

        test_119 は「run: の中に無いこと」を見るだけなので、
        受け渡しそのものが消えていないかを、ここで見る。
        """
        issue = self.workflow("post-from-issue.yml")
        for want in ("POST_BRANCH: ${{ steps.push.outputs.branch }}",
                     "POST_SLUG: ${{ steps.push.outputs.slug }}",
                     "PR_URL: ${{ steps.pr.outputs.url }}"):
            self.assertIn(want, issue,
                          "post-from-issue.yml に「%s」がありません" % want)
            self.assertIn(want.split(":")[0].strip(), issue)
        inbox = self.workflow("post-from-inbox.yml")
        self.assertIn("POST_SLUGS: ${{ steps.push.outputs.slugs }}", inbox,
                      "post-from-inbox.yml に POST_SLUGS がありません")

    def test_121_a_dangerous_slug_does_not_run_as_a_command(self):
        """いまの渡し方では、危ない slug がコマンドとして動かないこと。

        いまの渡し方（env: で環境変数にする）と、むかしの渡し方
        （run: の中に ${{ }} で文字として埋め込む）を、実際のシェルで
        動かして比べる。前者では動かず、後者では動いてしまうことを示す。
        後者まで確かめるのは、この試験に効き目があることの裏取り。
        （動かないことだけを見ていると、試験の書き方を間違えていても
        気づけない。）
        """
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash が見つかりません（Windows なら Git 付属の bash）")
        box = tempfile.mkdtemp(prefix="blog-shell-")
        try:
            slug = "$(touch pwned-by-slug)/2026-11-03"
            mark = os.path.join(box, "pwned-by-slug")

            # いまの渡し方: 環境変数で渡す
            subprocess.run(
                [bash, "-c", 'printf "%s\\n" "記事: ${POST_SLUG}" >/dev/null'],
                cwd=box, env=dict(os.environ, POST_SLUG=slug),
                capture_output=True)
            self.assertFalse(
                os.path.exists(mark),
                "環境変数で渡したのに、slug の中身がコマンドとして動きました")

            # むかしの渡し方: 文字として差し替える（${{ }} と同じこと）
            subprocess.run(
                [bash, "-c",
                 'printf "%%s\\n" "記事: %s" >/dev/null' % slug],
                cwd=box, capture_output=True)
            self.assertTrue(
                os.path.exists(mark),
                "この試験に効き目がありません（差し込みが起きていません）")
        finally:
            shutil.rmtree(box, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
