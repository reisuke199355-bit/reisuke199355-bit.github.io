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
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import ingest
import rules

common.use_utf8_stdout()

ROOT = common.ROOT
TEMPLATE = None             # 試すたびに複製する、組み立て済みのひな型
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")

NEW_POST = "2026年11月02日\n\n今日は自動検査の仕組みを作った。\nうまく動いてくれるとよい。\n"

# ---- 試験が使う日付 ----
#
# この試験は、実物のリポジトリを丸ごと複製したひな型の上で動く。
# だから「実物に何が入っているか」で結果が変わってしまう余地がある。
# 実際に 2026-11-03 の日記を inbox に置いた日、10 件の試験が落ちた。
# 理由は2つあり、どちらも日付の決め打ちが元だった。
#
#   ・同じ名前（2026-11-03.txt）から取り込んだ記事がもうあると、
#     二重投稿の防止が働いて、試験の取り込みそのものが拒否される
#   ・同じ日付の記事がもうあると、取り込みは別名（-2）に逃げるので、
#     決め打ちのパスを読む試験が、自分の記事ではなく既存の記事を読む
#
# そこで日付を2つに分けて宣言し、ひな型を作るときに下ごしらえする。
# 試験で新しい日付を使うときは、必ずどちらかに足すこと。
# 足し忘れは test_132 が見つける。

# 「その日の記事はまだ無い」ことを前提にしている日付。
# ひな型からこの日付の記事を外してから、試験を始める。
RESERVED_TEST_DATES = ("2026-11-02", "2026-11-03", "2026-11-04")

# 「その日の記事はもうある」ことを前提にしている、または記事があっても
# 困らない日付。ひな型にはそのまま残す。
KNOWN_OCCUPIED_DATES = ("2026-10-30", "2026-11-01")

# 試験が名指しで読む記事。ひな型に無ければ、そこで止めて知らせる。
REQUIRED_TEMPLATE_POSTS = ("2026-10-30.html",)


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


def posts_by_date(blog):
    """記事を、日記の日付ごとにまとめて返す。

    値は (パス, フロントマター) の一覧。日付が読めないものは "" に入る。
    """
    out = {}
    posts = os.path.join(blog, "content", "posts")
    for folder, dirs, names in os.walk(posts):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith(".html"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as f:
                meta = common.parse_front_matter(f.read())[0]
            day = str(meta.get("diary_date") or meta.get("date") or "")[:10]
            out.setdefault(day, []).append((path, meta))
    return out


def strip_pending_inbox(blog):
    """inbox から、まだ記事になっていない TXT を取り除く。

    inbox の TXT は「これから記事にするもの」なので、ひな型に持ち込むと、
    試験が置いた TXT と一緒に取り込まれてしまう。
    「これは取り込まないこと」を見る試験が、それで壊れる。
    README.md などの案内は残す。取り除いた数を返す。
    """
    removed = 0
    for folder, dirs, names in os.walk(os.path.join(blog, "inbox")):
        dirs.sort()
        for name in sorted(names):
            if name.lower().endswith(".txt"):
                os.remove(os.path.join(folder, name))
                removed += 1
    return removed


def free_reserved_dates(blog, dates=RESERVED_TEST_DATES):
    """試験が「まだ無い」前提で使う日付の記事を取り除く。

    移行してきた記事（legacy_url を持つもの）は取り除かない。
    旧URLの転送先になっているので、消すと verify.py が通らなくなる。
    そういう日付を予約してしまったときは、ここで止めて知らせる。
    取り除いたファイルの、posts からの相対パスの一覧を返す。
    """
    posts = os.path.join(blog, "content", "posts")
    wanted = set(dates)
    removed = []
    for day, items in sorted(posts_by_date(blog).items()):
        if day not in wanted:
            continue
        for path, meta in items:
            rel = os.path.relpath(path, posts).replace(os.sep, "/")
            if meta.get("legacy_url"):
                raise RuntimeError(
                    "試験が使う日付 %s に、移行してきた記事があります（%s）。\n"
                    "この記事は旧URLの転送先なので、外すと verify.py が"
                    "通りません。\n"
                    "selftest.py の RESERVED_TEST_DATES からこの日付を外し、\n"
                    "試験のほうで別の日付を使ってください。" % (day, rel))
            os.remove(path)
            removed.append(rel)
    return removed


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
    # 実物の中身に左右されないように、組み立てる前に下ごしらえする
    # （上の「試験が使う日付」の覚え書きを見てください）。
    strip_pending_inbox(blog)
    free_reserved_dates(blog)
    for name in REQUIRED_TEMPLATE_POSTS:
        if not os.path.exists(os.path.join(blog, "content", "posts", name)):
            raise RuntimeError(
                "試験が名指しで読む記事がありません: content/posts/%s\n"
                "この記事を消したのなら、それを読んでいる試験も直して"
                "ください。" % name)
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
        # git が作るファイルは読み取り専用で、Windows ではそのままだと消せない
        for folder, _dirs, names in os.walk(self.root):
            for name in names:
                os.chmod(os.path.join(folder, name), stat.S_IWRITE)
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

    def imported(self, box, result):
        """いま取り込んだ記事を1件読む。画面に出た URL をたどる。

        記事の置き場所を決め打ちにしない。実物に同じ日の記事があると、
        取り込みは別名（-2）に逃げるので、決め打ちでは読めなくなる。
        戻り値は (URL, フロントマター)。
        """
        slugs = re.findall(r"→ /posts/(\S+)/", result.stdout or "")
        self.assertEqual(1, len(slugs),
                         "取り込んだ記事が1件だけ出るはずです:\n%s" % result.stdout)
        parts = slugs[0].split("/")
        parts[-1] += ".html"
        return slugs[0], common.parse_front_matter(
            box.read("content", "posts", *parts))[0]


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

    def test_15_a_free_name_imports_as_today(self):
        """日付のない自由な名前の TXT を名指しで渡すと、きょうの日記になる。

        「1_日記を取り込む.bat」へのドラッグ＆ドロップが、この形になる。
        日付はファイルの更新日時ではなく、取り込んだ日（日本時間）を使う。
        """
        box = self.sandbox()
        today = ingest.local_today(common.load_config())
        path = box.put("日記", "きょうのできごと.txt",
                       "朝から雨だった。\n\n夕方には晴れた。\n")
        os.utime(path, (0, 0))          # 更新日時は 1970 年。使われないこと
        # パソコンの時計が日本に合っていなくても、きょうは日本時間で数える
        result = run(box.blog, "import_txt.py", path, env={"TZ": "UTC+12"})
        self.assertOK(result)
        self.assertIn("[取り込み] 日記 / きょうのできごと.txt", result.stdout)
        self.assertIn("きょう（%s）" % today.isoformat(), result.stdout)
        self.assertNotIn("1970", result.stdout)
        slug, entry = self.imported(box, result)
        self.assertEqual(entry["diary_date"], today.isoformat())
        self.assertEqual(entry["date"], "%s 21:00:00" % today.isoformat())
        self.assertEqual(entry["title"], "きょうのできごと")
        self.assertEqual(entry["source_file"], "きょうのできごと.txt")

    def test_16_a_heading_wins_over_the_file_name(self):
        """見出しがあれば、ファイル名ではなく見出しが題になる。"""
        box = self.sandbox()
        path = box.put("日記", "なにかの名前.txt",
                       "# 雨の日のこと\n\n朝から雨だった。\n\n夕方には晴れた。\n")
        result = run(box.blog, "import_txt.py", path)
        self.assertOK(result)
        slug, entry = self.imported(box, result)
        self.assertEqual(entry["title"], "雨の日のこと")

    def test_17_a_dated_name_keeps_the_japanese_date_title(self):
        """日付の名前で見出しが無ければ、題は「2026年11月2日」の形のまま。

        ファイル名をそのまま題にする決まりを足したあとも、
        日付を入れた名前のときは、これまでどおりであること。
        """
        box = self.sandbox()
        box.put("日記", "2026-11-02.txt", "見出しのない日記。\n\n本文です。\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        slug, entry = self.imported(box, result)
        self.assertEqual("2026-11-02", slug)
        self.assertEqual(entry["title"], "2026年11月2日")

    def test_18_a_bare_line_in_a_new_category_becomes_an_article(self):
        """見出しも日付もない1行の TXT が、記事になって組み立てまで通ること。

        本人が置いた「テスト／ローカルテスト.txt」と同じ形
        （本文1行だけ・見出しなし・日付なし・カテゴリーのフォルダ名が
        そのまま URL に入る）。名前と本文は実物とずらしてある。
        同じにすると、本人がその TXT をほんとうに取り込んだあと、
        二重投稿の防止が働いてこの試験が落ちてしまう。
        """
        box = self.sandbox()
        today = ingest.local_today(common.load_config())
        path = box.put("テスト", "ローカルテストの下見.txt", "ローカルテストの下見")
        result = run(box.blog, "import_txt.py", path, "--新カテゴリー許可")
        self.assertOK(result)
        self.assertNotIn("名前に「テスト」が入っています", result.stdout)
        slug, entry = self.imported(box, result)
        # URL は決め打ちにしない。同じ日の記事があると -2 に逃げるため
        self.assertTrue(slug.startswith("テスト/"), slug)
        self.assertEqual(entry["title"], "ローカルテストの下見")
        self.assertEqual(entry["diary_date"], today.isoformat())
        self.assertEqual(entry["categories"], ["テスト"])
        self.assertEqual(entry["source"], "txt")
        self.assertOK(run(box.blog, "build.py"))
        page = box.read("docs", "posts", *(slug.split("/") + ["index.html"]))
        self.assertIn("ローカルテストの下見", page)
        # 検査は注意を出すが、公開を止める問題にはしない
        check = run(box.blog, "check.py")
        self.assertOK(check)
        self.assertIn("もとのファイル名に日付が入っていません", check.stdout)

    def test_19_a_date_in_the_heading_still_sets_the_day(self):
        """名前に日付が無くても、見出しの日付が投稿日になる（従来どおり）。

        この道は、まとめて取り込むときにも通る。
        「名指しのときだけ、きょうの日付」の決まりを足したあとも、
        見出しに日付がある TXT は、これまでどおり取り込めること。
        """
        box = self.sandbox()
        box.put("日記", "なまえだけ.txt",
                "# 2026年11月04日\n\n見出しに日付を書いた日記。\n")
        result = run(box.blog, "import_txt.py")
        self.assertOK(result)
        slug, entry = self.imported(box, result)
        self.assertEqual("2026-11-04", slug)
        self.assertEqual(entry["title"], "2026年11月04日")
        self.assertEqual(entry["diary_date"], "2026-11-04")


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

    def test_32_a_free_name_is_not_swept_up_by_a_folder_scan(self):
        """日付のない名前は、まとめて見るときには取り込まない。

        名指しで渡したときだけ、きょうの日付で取り込む。
        カテゴリーのフォルダに置いたままの TXT が、置いた覚えのない
        日付で記事になってしまうのを防ぐため。日付を必須にしていたのは
        もともとこの事故を防ぐためなので、そこは外していない。
        """
        box = self.sandbox()
        before = box.posts()
        path = box.put("日記", "あとで書く.txt", "まだ下書き。\n\n書きかけです。\n")
        scan = run(box.blog, "import_txt.py")
        self.assertOK(scan)
        self.assertIn("ファイル名に日付が入っていません", scan.stdout)
        self.assertEqual(before, box.posts(), "まとめて見たときに取り込まれました")
        named = run(box.blog, "import_txt.py", path)
        self.assertOK(named)
        self.assertIn("[取り込み] 日記 / あとで書く.txt", named.stdout)
        self.assertNotEqual(before, box.posts(), "名指しでも取り込まれません")

    def test_33_a_test_name_passes_only_in_a_matching_category(self):
        """「テスト」という語は、カテゴリー名が同じときだけ疑わない。"""
        box = self.sandbox()
        before = box.posts()
        # 名前は実物（ローカルテスト.txt）とずらす。同じにすると、
        # 本人がそれを取り込んだあと、二重投稿の防止でこの試験が落ちる。
        body = "ためしに書く。\n\n本文です。\n"
        path = box.put("日記", "テストの記録.txt", body)
        result = run(box.blog, "import_txt.py", path)
        self.assertOK(result)
        self.assertIn("名前に「テスト」が入っています", result.stdout)
        self.assertEqual(before, box.posts(), "「日記」で取り込まれました")

        path = box.put("テスト", "テストの記録.txt", body)
        result = run(box.blog, "import_txt.py", path, "--新カテゴリー許可")
        self.assertOK(result)
        self.assertNotIn("名前に「テスト」が入っています", result.stdout)
        self.assertIn("[取り込み] テスト / テストの記録.txt", result.stdout)

    def test_34_a_manual_name_is_refused_in_a_test_category(self):
        """カテゴリー名に無い語が入っていれば、やはり見送る。"""
        box = self.sandbox()
        before = box.posts()
        path = box.put("テスト", "使い方のテスト.txt", "説明です。\n\n本文です。\n")
        result = run(box.blog, "import_txt.py", path, "--新カテゴリー許可")
        self.assertOK(result)
        self.assertIn("名前に「使い方」が入っています", result.stdout)
        self.assertEqual(before, box.posts())

    def test_35_a_copy_name_is_refused_in_a_matching_category(self):
        """編集ソフトの控えの名前は、カテゴリー名と関係なく見送る。"""
        box = self.sandbox()
        before = box.posts()
        path = box.put("テスト", "テスト - コピー.txt", "控えです。\n\n本文です。\n")
        result = run(box.blog, "import_txt.py", path, "--新カテゴリー許可")
        self.assertOK(result)
        self.assertIn("一時ファイル・控えの名前です", result.stdout)
        self.assertEqual(before, box.posts())


    def test_36_a_free_name_is_not_imported_twice(self):
        """同じ TXT をもう一度渡しても、二重にならない。

        日付は取り込んだ日なので、日をまたいで入れ直すと別の日の記事に
        なりかねない。中身を直した TXT でも、勝手に差し替えない。
        """
        box = self.sandbox()
        path = box.put("日記", "二度入れる日記.txt", "一度目の本文。\n\n続きです。\n")
        self.assertOK(run(box.blog, "import_txt.py", path))
        after_first = box.posts()

        again = run(box.blog, "import_txt.py", path)
        self.assertOK(again)
        self.assertIn("取り込み済みの TXT が 1 件", again.stdout)
        self.assertEqual(after_first, box.posts(), "同じ TXT で記事が増えました")

        box.put("日記", "二度入れる日記.txt", "書き直した本文。\n\n続きです。\n")
        edited = run(box.blog, "import_txt.py", path)
        self.assertOK(edited)
        self.assertIn("同じ名前で取り込み済みです", edited.stdout)
        self.assertEqual(after_first, box.posts(), "既存の記事が置き換わりました")

    def test_37_two_free_names_on_the_same_day_do_not_collide(self):
        """同じ日に自由な名前の TXT を2つ入れても、先の記事を上書きしない。"""
        box = self.sandbox()
        morning = box.put("日記", "朝のこと.txt", "朝の本文です。\n\n続きです。\n")
        night = box.put("日記", "夜のこと.txt", "夜の本文です。\n\n続きです。\n")

        first = run(box.blog, "import_txt.py", morning)
        self.assertOK(first)
        first_slug, first_entry = self.imported(box, first)
        first_file = first_slug.split("/")
        first_file[-1] += ".html"
        before = box.read("content", "posts", *first_file)

        second = run(box.blog, "import_txt.py", night)
        self.assertOK(second)
        second_slug, second_entry = self.imported(box, second)
        self.assertNotEqual(first_slug, second_slug)
        self.assertEqual(first_entry["diary_date"], second_entry["diary_date"])
        self.assertIn("別の記事として足します", second.stdout)
        self.assertEqual(before, box.read("content", "posts", *first_file),
                         "先に入れた記事が書き換えられました")

    def test_38_a_free_name_needs_a_body(self):
        """中身のない TXT や見出しだけの TXT から、記事を作らない。"""
        box = self.sandbox()
        before = box.posts()
        empty = box.put("日記", "からっぽ.txt", "  \n\n")
        heading = box.put("日記", "見出しだけ.txt", "# 見出しだけ\n")
        result = run(box.blog, "import_txt.py", empty, heading)
        self.assertOK(result)
        self.assertIn("中身が空です", result.stdout)
        self.assertIn("本文がありません", result.stdout)
        self.assertEqual(before, box.posts())

    def test_39_a_free_name_outside_a_category_is_refused(self):
        """カテゴリーのフォルダの外にある TXT は、名指しでも取り込まない。"""
        box = self.sandbox()
        before = box.posts()
        body = "本文です。\n\n続きです。\n"
        for path in (os.path.join(box.root, "外の日記.txt"),
                     os.path.join(box.articles, "直置きの日記.txt")):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
        cases = (
            (os.path.join(box.root, "外の日記.txt"),
             "記事管理フォルダの外にあります"),
            (os.path.join("..", "10_記事管理", "日記", "..", "..", "外の日記.txt"),
             "記事管理フォルダの外にあります"),
            (os.path.join(box.articles, "直置きの日記.txt"),
             "カテゴリーのフォルダに入っていません"),
            (box.put(os.path.join("_取り込み済み", "日記"), "控えの日記.txt", body),
             "「_」で始まるフォルダ"),
            (box.put(os.path.join("日記", "奥"), "奥の日記.txt", body),
             "さらに下にあります"),
        )
        for path, expected in cases:
            with self.subTest(path=path):
                result = run(box.blog, "import_txt.py", path)
                self.assertOK(result)
                self.assertIn(expected, result.stdout)
                self.assertEqual(before, box.posts())


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

def rev(cwd, name="HEAD"):
    return git(cwd, "rev-parse", name).stdout.strip()


class PublishGuards(Base):
    """公開の前に止めること。"""

    def make_github(self, box):
        """手元の複製に、仮の GitHub（ただのフォルダ）をつなぐ。

        本物の GitHub には一切さわらない。CI の git には名前の設定が
        無いので、ここで入れておく。作業用の控えの扱いや改行の決まりが
        実物と同じになるように、.gitignore と .gitattributes も持ってくる。
        """
        for name in (".gitignore", ".gitattributes"):
            shutil.copy2(os.path.join(ROOT, name), box.blog)
        box.make_git()
        git(box.blog, "branch", "-M", "main")
        git(box.blog, "config", "user.email", "t@example.invalid")
        git(box.blog, "config", "user.name", "t")
        origin = os.path.join(box.root, "origin.git")
        git(box.root, "init", "-q", "--bare", origin)
        git(box.blog, "remote", "add", "origin", origin)
        self.assertEqual(0, git(box.blog, "push", "-q", "-u", "origin", "main").returncode)
        return origin

    def phone_post(self, box, origin, day):
        """スマホから投稿して Merge したつもりで、仮の GitHub を進める。"""
        phone = os.path.join(box.root, "phone")
        self.assertEqual(0, git(box.root, "clone", "-q", "-b", "main", origin, phone).returncode)
        git(phone, "config", "user.email", "t@example.invalid")
        git(phone, "config", "user.name", "t")
        path = os.path.join(phone, "content", "posts", "%s.html" % day)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("---\ntitle: スマホの記事\ndate: %s 21:00:00\nslug: %s\n"
                    "diary_date: %s\ncategories: [日記]\n---\n\n"
                    "<p>スマホから書いた日記です。</p>\n" % (day, day, day))
        self.assertOK(run(phone, "build.py"))
        git(phone, "add", "-A")
        self.assertEqual(0, git(phone, "commit", "-qm", "スマホの記事").returncode)
        self.assertEqual(0, git(phone, "push", "-q", "origin", "main").returncode)

    def local_post(self, box, day):
        """手元で TXT を取り込んで、ページまで作る（1_ と 2_ のつもり）。"""
        box.put("日記", "%s.txt" % day, "手元で書いた日記です。\n\n二段落目です。\n")
        self.assertOK(run(box.blog, "import_txt.py"))
        self.assertOK(run(box.blog, "build.py"))

    def assertArticlesOnGitHub(self, origin, *slugs):
        for slug in slugs:
            for name in ("content/posts/%s.html" % slug, "docs/posts/%s/index.html" % slug):
                self.assertEqual(0, git(origin, "cat-file", "-e", "main:" + name).returncode,
                                 "GitHub にありません: " + name)

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

    def test_73_publish_merges_a_phone_post_when_sync_was_forgotten(self):
        """5_ を忘れて 1_ → 3_ としても、両方の記事が残って公開できる。"""
        box = self.sandbox()
        origin = self.make_github(box)
        self.phone_post(box, origin, "2026-11-02")
        self.local_post(box, "2026-11-03")
        result = run(box.blog, "publish.py", stdin="yes\n")
        self.assertOK(result)
        self.assertIn("GitHub のほうが 1 件新しくなっています", result.stdout)
        self.assertIn("公開しました", result.stdout)
        self.assertEqual(rev(box.blog), rev(origin, "main"))
        self.assertEqual("", git(box.blog, "status", "--porcelain").stdout)
        self.assertArticlesOnGitHub(origin, "2026-11-02", "2026-11-03")
        archive = box.read("docs", "archive", "index.html")
        self.assertIn("posts/2026-11-02/", archive)
        self.assertIn("posts/2026-11-03/", archive)
        self.assertOK(run(box.blog, "check.py"))

    def test_74_publish_sends_what_a_failed_push_left_behind(self):
        """送信に失敗しても、もう一度実行すれば送れる。「変更なし」とは言わない。"""
        box = self.sandbox()
        origin = self.make_github(box)
        self.local_post(box, "2026-11-03")
        git(box.blog, "remote", "set-url", "--push", "origin",
            os.path.join(box.root, "nowhere.git"))
        failed = run(box.blog, "publish.py", stdin="yes\n")
        self.assertNG(failed, "送信に失敗しました", "もう一度実行すれば")
        left = rev(box.blog)
        self.assertNotEqual(left, rev(origin, "main"))

        git(box.blog, "config", "--unset", "remote.origin.pushurl")
        declined = run(box.blog, "publish.py", stdin="\n")
        self.assertIn("まだ送信していないコミット: 1 件", declined.stdout)
        self.assertIn("中断しました", declined.stdout)
        self.assertNotIn("変わったところがありません", declined.stdout)
        self.assertNotEqual(left, rev(origin, "main"))

        again = run(box.blog, "publish.py", stdin="yes\n")
        self.assertOK(again)
        self.assertEqual(left, rev(origin, "main"))
        self.assertArticlesOnGitHub(origin, "2026-11-03")

    def test_75_publish_stops_before_committing_when_github_is_out_of_reach(self):
        """GitHub を見に行けないときは、コミットする前に止める。"""
        box = self.sandbox()
        self.make_github(box)
        self.local_post(box, "2026-11-03")
        before = rev(box.blog)
        git(box.blog, "remote", "set-url", "origin", os.path.join(box.root, "nowhere.git"))
        result = run(box.blog, "publish.py", stdin="yes\n")
        self.assertNG(result, "GitHub の状態を確かめられませんでした")
        self.assertEqual(before, rev(box.blog))
        self.assertEqual("", git(box.blog, "diff", "--cached", "--name-only").stdout)
        self.assertNotEqual("", git(box.blog, "status", "--porcelain").stdout)

    def test_76_publish_keeps_both_when_the_same_article_collides(self):
        """同じ日付の記事を両方で作ったら、何も失わずに止める。

        そのあと OPERATIONS.md の「公開が止まったとき」の手順で公開できること。
        """
        box = self.sandbox()
        origin = self.make_github(box)
        self.phone_post(box, origin, "2026-11-03")
        published = rev(origin, "main")
        self.local_post(box, "2026-11-03")
        result = run(box.blog, "publish.py", stdin="yes\n")
        self.assertNG(result, "自動では合わせられません",
                      "content/posts/2026-11-03.html", "公開が止まったとき")
        self.assertEqual(published, rev(origin, "main"))
        self.assertNotEqual(0, git(box.blog, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode)
        self.assertEqual("", git(box.blog, "status", "--porcelain").stdout)
        text = box.read("content", "posts", "2026-11-03.html")
        self.assertIn("手元で書いた日記です。", text)

        os.remove(box.blog_path("content", "posts", "2026-11-03.html"))
        box.write(text.replace("slug: 2026-11-03", "slug: 2026-11-03-2"),
                  "content", "posts", "2026-11-03-2.html")
        self.assertOK(run(box.blog, "build.py"))
        again = run(box.blog, "publish.py", stdin="yes\n")
        self.assertOK(again)
        self.assertEqual(rev(box.blog), rev(origin, "main"))
        self.assertArticlesOnGitHub(origin, "2026-11-03", "2026-11-03-2")
        self.assertOK(run(box.blog, "check.py"))

    def test_77_publish_says_what_is_true_when_nothing_can_be_sent(self):
        """送るものが無いときの案内。GitHub が新しいときは、そう伝える。"""
        box = self.sandbox()
        origin = self.make_github(box)
        same = run(box.blog, "publish.py")
        self.assertOK(same)
        self.assertIn("変わったところがありません", same.stdout)

        self.phone_post(box, origin, "2026-11-02")
        before = rev(box.blog)
        newer = run(box.blog, "publish.py")
        self.assertOK(newer)
        self.assertIn("手元から送信するものはありません", newer.stdout)
        self.assertIn("5_クラウドの投稿を受け取る.bat", newer.stdout)
        self.assertEqual(before, rev(box.blog))

        self.assertOK(run(box.blog, "sync.py"))
        self.assertEqual(rev(origin, "main"), rev(box.blog))

    def test_78_sync_points_to_publish_when_the_branches_diverged(self):
        """枝分かれしているとき、5_ は何も変えず、3_ を案内する。案内どおり公開できる。"""
        box = self.sandbox()
        origin = self.make_github(box)
        self.phone_post(box, origin, "2026-11-02")
        self.local_post(box, "2026-11-03")
        git(box.blog, "add", "-A")
        self.assertEqual(0, git(box.blog, "commit", "-qm", "手元の記事").returncode)
        before = rev(box.blog)
        result = run(box.blog, "sync.py")
        self.assertNG(result, "枝分かれ", "3_GitHubへ公開.bat")
        self.assertEqual(before, rev(box.blog))

        self.assertOK(run(box.blog, "publish.py", stdin="yes\n"))
        self.assertEqual(rev(box.blog), rev(origin, "main"))
        self.assertArticlesOnGitHub(origin, "2026-11-02", "2026-11-03")


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

    def test_106_the_rehearsal_works_after_a_real_post_today(self):
        """きょうの日記を inbox から入れたあとでも、予行演習が通ること。

        予行演習は「きょうの日付」で TXT を置く。名前をそのまま
        <きょう>.txt にすると、本人がきょう inbox から入れた記事と
        同じ名前になり、二重投稿の防止で止まってしまう。
        実機で1回通したとき、これで予行演習が落ちた。

        きょうの日付は予約できない（本人がその日の日記を書くのだから）。
        だから、ここだけは「ぶつかっても通ること」を確かめる。
        """
        box = self.sandbox()
        today = ingest.local_today(common.load_config()).isoformat()
        box.put_inbox("%s.txt" % today,
                      "# きょうの日記\n\n本人がきょう入れた日記のつもりです。\n")
        self.assertOK(run(box.blog, "import_inbox.py"))
        self.assertOK(run(box.blog, "build.py"))
        self.assertOK(run(box.blog, "rehearse_post.py"))


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


# ------------------------------- この試験そのものの土台の決まり（V02）

class Harness(Base):
    """この試験が、実物のリポジトリの中身に左右されないこと。

    2026-11-03 の日記を inbox に置いた日、ひな型に「取り込み待ちの TXT」と
    「その日の記事」が入り込み、10 件の試験が落ちた。
    記事は正しく作れていたのに、検査が落ちたせいで公開が止まった。
    同じことが二度起きないように、ひな型の下ごしらえを見張る。
    """

    def template(self, *parts):
        return os.path.join(TEMPLATE, "blog", *parts)

    def test_130_the_template_has_no_pending_inbox_txt(self):
        """ひな型の inbox に、取り込み待ちの TXT が残っていないこと。"""
        inbox = self.template("inbox")
        left = []
        for folder, dirs, names in os.walk(inbox):
            dirs.sort()
            for name in sorted(names):
                if name.lower().endswith(".txt"):
                    left.append(os.path.relpath(os.path.join(folder, name),
                                                inbox).replace(os.sep, "/"))
        self.assertEqual(
            [], left,
            "ひな型に取り込み待ちの TXT が入っています。試験が置いた TXT と"
            "一緒に取り込まれ、「取り込まないこと」の試験が壊れます。")

    def test_131_the_template_is_prepared_as_intended(self):
        """ひな型の日付の下ごしらえが、思ったとおりになっていること。"""
        found = posts_by_date(self.template())
        for day in RESERVED_TEST_DATES:
            self.assertNotIn(
                day, found,
                "%s の記事がひな型に残っています"
                "（RESERVED_TEST_DATES なので外すはずです）" % day)
        for name in REQUIRED_TEMPLATE_POSTS:
            self.assertTrue(
                os.path.exists(self.template("content", "posts", name)),
                "試験が名指しで読む記事がありません: content/posts/%s" % name)

    def test_132_every_fixture_date_is_accounted_for(self):
        """試験が使う日付が、すべてどちらかの一覧に入っていること。

        新しい日付の TXT を試験に足したとき、一覧への足し忘れを見つける。
        ブログの範囲外の日付（2099-01-01 など）は、そもそも取り込まれず
        記事にならないので見送る。
        """
        with open(os.path.join(ROOT, "tools", "selftest.py"),
                  encoding="utf-8") as f:
            src = f.read()
        patterns = (
            r'\.put\(\s*"[^"]*"\s*,\s*"([^"]+)"',
            r'\.put_bytes\(\s*"[^"]*"\s*,\s*"([^"]+)"',
            r'\.put_inbox\(\s*"([^"]+)"',
            r'\bday="([^"]*)"',
        )
        known = set(RESERVED_TEST_DATES) | set(KNOWN_OCCUPIED_DATES)
        seen = set()
        for pattern in patterns:
            for m in re.finditer(pattern, src):
                name = m.group(1)
                day = rules.date_from_name(name)
                if not day or rules.date_in_range(day):
                    continue
                seen.add(day.isoformat())
                self.assertIn(
                    day.isoformat(), known,
                    "試験が %r で %s を使っていますが、どちらの一覧にも"
                    "入っていません。RESERVED_TEST_DATES（その日の記事は"
                    "まだ無い前提）か KNOWN_OCCUPIED_DATES（記事があっても"
                    "困らない）に足してください。"
                    % (name, day.isoformat()))
        self.assertTrue(
            seen,
            "日付つきの TXT が1つも見つかりませんでした"
            "（この試験の探し方が古くなっています）")
        for day in RESERVED_TEST_DATES:
            self.assertIn(
                day, seen,
                "%s は予約されていますが、どの試験も使っていません。"
                "要らなくなったら RESERVED_TEST_DATES から外してください。"
                % day)

    def test_133_the_preparation_removes_what_the_repo_carries(self):
        """実物に取り込み待ちの TXT と予約日付の記事があっても、外せること。

        2026-11-03 の日記を置いた日に起きたことを、ここで作って確かめる。
        """
        box = tempfile.mkdtemp(prefix="blog-harness-")
        self.addCleanup(shutil.rmtree, box, True)

        def put(text, *parts):
            path = os.path.join(box, *parts)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)

        def article(day, extra=""):
            return ("---\ntitle: %s\ndate: %s 21:00:00\nslug: %s\n"
                    "diary_date: %s\ncategories: [日記]\ntags: []\n%s"
                    "---\n\n<p>本文</p>\n" % (day, day, day, day, extra))

        put("置き方の案内\n", "inbox", "README.md")
        put("きょうの日記\n", "inbox", "2026-11-03.txt")
        put("きょうの感想\n", "inbox", "感想", "2026-11-03.txt")
        put(article("2026-11-03"), "content", "posts", "2026-11-03.html")
        put(article("2026-11-03"), "content", "posts", "感想",
            "2026-11-03.html")
        put(article("2026-10-30"), "content", "posts", "2026-10-30.html")

        self.assertEqual(2, strip_pending_inbox(box),
                         "取り込み待ちの TXT を外せていません")
        self.assertTrue(
            os.path.exists(os.path.join(box, "inbox", "README.md")),
            "案内の README.md まで消してしまっています")

        self.assertEqual(["2026-11-03.html", "感想/2026-11-03.html"],
                         sorted(free_reserved_dates(box)),
                         "予約日付の記事を外せていません")
        self.assertTrue(
            os.path.exists(os.path.join(box, "content", "posts",
                                        "2026-10-30.html")),
            "予約していない日付の記事まで消してしまっています")

    def test_134_a_reserved_date_on_a_migrated_article_stops_the_run(self):
        """移行してきた記事の日付を予約してしまったら、止めて知らせること。

        旧URLの転送先を黙って消すと verify.py が通らなくなるので、
        消さずに止める。
        """
        box = tempfile.mkdtemp(prefix="blog-harness-")
        self.addCleanup(shutil.rmtree, box, True)
        path = os.path.join(box, "content", "posts", "2026-11-03.html")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("---\ntitle: むかしの記事\ndate: 2026-11-03 21:00:00\n"
                    "slug: 2026-11-03\ndiary_date: 2026-11-03\n"
                    "categories: [日記]\ntags: []\n"
                    "legacy_url: /2026/11/03/old/\n---\n\n<p>本文</p>\n")
        with self.assertRaises(RuntimeError) as caught:
            free_reserved_dates(box)
        self.assertIn("旧URLの転送先", str(caught.exception))
        self.assertTrue(os.path.exists(path),
                        "止めると言いながら消してしまっています")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
