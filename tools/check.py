# -*- coding: utf-8 -*-
"""ブログ全体を点検して、結果を日本語で知らせる。

使い方:
    python check.py              すべて点検する
    python check.py --衛生のみ    公開してはいけないファイルの点検だけ（速い）
    python check.py --記事のみ    記事のもとの点検だけ（速い）
    python check.py --ci         GitHub Actions の画面にも結果を書く
    python check.py --json       結果を JSON で出す

点検するもの:
    A. 記事のもと   … フロントマター、カテゴリー、URL、重複、誤取り込み、日付、本文、画像
    B. 公開済みの記録 … baseline.json と見比べて、過去記事が変わっていないか
    C. 公開物       … 作り直して一致するか、余分なファイル、リンク、旧URL転送、RSS
    D. リポジトリ衛生 … 公開してはいけないファイルが入っていないか

このコマンドは何も書き換えない。
作り直しての照合は一時フォルダで行うので、content/ と docs/ は変わらない。
画面に出す記号は [OK] [NG] [注意] と -> だけにしている（Windows の画面で化けないため）。
"""
import collections
import difflib
import html.parser
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baseline
import common
import rules
import verify

common.use_utf8_stdout()

NEAR_DUPLICATE_RATIO = 0.95
SHORT_BODY_SUSPECT = 60


# ---------------------------------------------------------------- 結果の受け皿

class Report:
    def __init__(self):
        self.groups = []

    def group(self, title):
        g = Group(title)
        self.groups.append(g)
        return g

    @property
    def errors(self):
        return [(g.title, line) for g in self.groups for line in g.errors]

    @property
    def warnings(self):
        return [(g.title, line) for g in self.groups for line in g.warnings]

    def print_text(self):
        for g in self.groups:
            print("\n### %s" % g.title)
            for kind, line in g.lines:
                print("  %s %s" % (kind, line))

    def as_dict(self):
        return {
            "errors": [{"group": t, "message": m} for t, m in self.errors],
            "warnings": [{"group": t, "message": m} for t, m in self.warnings],
            "ok": not self.errors,
        }

    def markdown(self):
        out = []
        if self.errors:
            out.append("## 直す必要があります（%d 件）\n" % len(self.errors))
            for title, line in self.errors:
                out.append("- **%s** %s" % (title.split(".")[0], line))
            out.append("")
        else:
            out.append("## すべての点検を通りました\n")
        if self.warnings:
            out.append("## 気をつけたい点（%d 件・止めません）\n" % len(self.warnings))
            for title, line in self.warnings:
                out.append("- %s %s" % (title.split(".")[0], line))
            out.append("")
        out.append("<details><summary>点検の内訳</summary>\n")
        for g in self.groups:
            out.append("### %s" % g.title)
            for kind, line in g.lines:
                out.append("- `%s` %s" % (kind, line))
            out.append("")
        out.append("</details>")
        return "\n".join(out)


class Group:
    def __init__(self, title):
        self.title = title
        self.lines = []
        self.errors = []
        self.warnings = []

    def ng(self, message):
        self.lines.append(("[NG]", message))
        self.errors.append(message)

    def warn(self, message):
        self.lines.append(("[注意]", message))
        self.warnings.append(message)

    def ok(self, message):
        self.lines.append(("[OK]", message))

    def note(self, message):
        self.lines.append(("[知らせ]", message))


# ---------------------------------------------------------------- 本文の下ごしらえ

VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
# 閉じ忘れても HTML として通る（ブラウザが補う）ので、閉じ漏れを責めないタグ
OPTIONAL_CLOSE = frozenset({"li", "p", "dt", "dd", "option", "thead", "tbody", "tr", "td", "th"})


class TagBalance(html.parser.HTMLParser):
    """本文のタグの閉じ忘れ・閉じ違いを調べる。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.problems = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if tag in self.stack:
            while self.stack:
                top = self.stack.pop()
                if top == tag:
                    break
                if top not in OPTIONAL_CLOSE:
                    self.problems.append("<%s> が閉じられていません" % top)
        else:
            self.problems.append("</%s> に対応する開始タグがありません" % tag)

    def close(self):
        super().close()
        for tag in self.stack:
            if tag not in OPTIONAL_CLOSE:
                self.problems.append("<%s> が閉じられていません" % tag)


def flat_text(body):
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", body or ""))


def raw_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------- A. 記事のもと

def check_sources(report, posts, pages):
    g = report.group("A1. フロントマター（記事の先頭の情報）")
    bad = 0
    for entry, required, kind in (
            [(p, rules.REQUIRED_POST_KEYS, "記事") for p in posts]
            + [(p, rules.REQUIRED_PAGE_KEYS, "固定ページ") for p in pages]):
        where = os.path.relpath(entry["_path"], common.ROOT).replace(os.sep, "/")
        missing = [k for k in required if not entry.get(k)]
        if missing:
            g.ng("%s %s: %s がありません" % (kind, where, "・".join(missing)))
            bad += 1
        if entry.get("_dropped"):
            g.ng("%s %s: 読み取れなかった行があります（「項目: 値」の形にしてください）: %s"
                 % (kind, where, " / ".join(entry["_dropped"])))
            bad += 1
        date = entry.get("date")
        if kind == "記事" and date and not rules.DATE_VALUE.match(date):
            g.ng("記事 %s: 投稿日の形が違います: %r（例 2026-11-02 21:00:00）" % (where, date))
            bad += 1
        diary = entry.get("diary_date")
        if diary and not re.match(r"^\d{4}-\d{2}-\d{2}$", diary):
            g.ng("記事 %s: 日記の日付の形が違います: %r（例 2026-11-02）" % (where, diary))
            bad += 1
        if kind == "記事" and not entry.get("categories"):
            g.ng("記事 %s: カテゴリーが空です" % where)
            bad += 1
    if not bad:
        g.ok("記事 %d 件・固定ページ %d 件、どれも必要な項目がそろっています"
             % (len(posts), len(pages)))

    g = report.group("A2. カテゴリー")
    cfg = common.load_config()
    allowed = rules.allowed_categories(cfg)
    unknown = collections.Counter()
    for post in posts:
        for name in post.get("categories", []):
            if name not in allowed:
                unknown[name] += 1
    if unknown:
        for name, count in unknown.most_common():
            g.ng("登録されていないカテゴリー「%s」の記事が %d 件あります。"
                 "使えるのは %s です。増やすなら config.json の categories に追記してください"
                 % (name, count, " / ".join(allowed)))
    else:
        used = sorted({c for p in posts for c in p.get("categories", [])})
        g.ok("使われているカテゴリーはすべて登録済みです: %s" % " / ".join(used))
    top_level = rules.reserved_top_level(p.get("slug") for p in pages)
    for name in allowed:
        ok, why = rules.safe_url_name(name, reserved=top_level)
        if not ok:
            g.ng("登録されているカテゴリー「%s」は URL に使えません: %s" % (name, why))
        else:
            for w in rules.url_name_warnings(name):
                g.warn("カテゴリー「%s」: %s" % (name, w))

    g = report.group("A3. URL（slug）")
    slugs = collections.defaultdict(list)
    bad = 0
    for post in posts:
        slug = post.get("slug") or ""
        slugs[slug].append(post.get("title") or "(題なし)")
        # 記事は posts/ の下に入るので、サイトの骨組みの名前とはぶつからない。
        # 文字の安全さと、記事同士の重複だけを見る。
        ok, why = rules.safe_slug(slug, reserved=())
        if not ok:
            g.ng("記事 %s の URL が使えません: %s"
                 % (os.path.relpath(post["_path"], common.ROOT).replace(os.sep, "/"), why))
            bad += 1
    # 固定ページは docs/ の直下に入るので、骨組みの名前とページ同士の衝突を見る。
    for page in pages:
        slug = page.get("slug") or ""
        others = {p.get("slug") for p in pages if p is not page and p.get("slug")}
        ok, why = rules.safe_slug(slug, reserved=rules.RESERVED_STRUCTURE | others)
        if not ok:
            g.ng("固定ページ %s の URL が使えません: %s" % (slug, why))
            bad += 1
    duplicated = {s: t for s, t in slugs.items() if len(t) > 1}
    if duplicated:
        for slug, titles in sorted(duplicated.items()):
            g.ng("URL が重複しています: /posts/%s/ … %s" % (slug, "、".join(titles)))
        bad += len(duplicated)
    page_slugs = {p.get("slug") for p in pages}
    clash = page_slugs & set(slugs)
    if clash:
        g.ng("固定ページと記事の URL がぶつかっています: %s" % "、".join(sorted(clash)))
        bad += 1
    if not bad:
        g.ok("記事 %d 件の URL はすべて一意で、安全な文字だけです" % len(slugs))

    g = report.group("A4. 重複と本文の中身")
    texts = {}
    empty = []
    for post in posts:
        slug = post.get("slug") or post["_path"]
        text = flat_text(post.get("_body", ""))
        texts[slug] = text
        if not text:
            empty.append(slug)
    for slug in empty:
        g.ng("本文が空の記事があります: /posts/%s/" % slug)
    same = collections.defaultdict(list)
    for slug, text in texts.items():
        if text:
            same[text].append(slug)
    exact = [v for v in same.values() if len(v) > 1]
    for group_ in exact:
        g.ng("本文がまったく同じ記事があります: %s" % "、".join("/posts/%s/" % s for s in sorted(group_)))
    items = sorted((s, t) for s, t in texts.items() if t)
    near = 0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i][1], items[j][1]
            longest = max(len(a), len(b))
            if longest == 0 or abs(len(a) - len(b)) / longest > 0.2:
                continue
            m = difflib.SequenceMatcher(None, a, b)
            if m.quick_ratio() <= NEAR_DUPLICATE_RATIO:
                continue
            if m.ratio() > NEAR_DUPLICATE_RATIO:
                g.ng("本文がほとんど同じ記事があります（%.0f%% 一致）: /posts/%s/ と /posts/%s/"
                     % (m.ratio() * 100, items[i][0], items[j][0]))
                near += 1
    by_diary = collections.Counter(p.get("diary_date") for p in posts if p.get("diary_date"))
    multi = {d: n for d, n in by_diary.items() if n > 1}
    if multi:
        g.warn("同じ日の記事が複数あります（意図した使い方なら問題ありません）: %s"
               % "、".join("%s が %d 件" % (d, n) for d, n in sorted(multi.items())))
    if not empty and not exact and not near:
        g.ok("本文の重複なし・空の記事なし（%d 件）" % len(posts))

    g = report.group("A5. 誤って取り込まれた疑い")
    suspects = 0
    for post in posts:
        slug = post.get("slug") or ""
        text = flat_text(post.get("_body", ""))
        reasons = []
        source_file = post.get("source_file")
        if source_file:
            why = rules.non_article_name(source_file)
            if why:
                reasons.append("もとのファイル名「%s」: %s" % (source_file, why))
            if rules.date_from_name(source_file) is None:
                reasons.append("もとのファイル名に日付が入っていません")
        doc_words = rules.doc_body_words = rules.doc_like_body(post.get("_body", ""))
        if len(text) < SHORT_BODY_SUSPECT and doc_words:
            reasons.append("本文が %d 字しかなく、説明書らしい語（%s）が入っています"
                           % (len(text), "・".join(doc_words[:3])))
        if post.get("title") and rules.non_article_name(post["title"]):
            reasons.append("題が説明や覚え書きのようです: 「%s」" % post["title"])
        if reasons:
            suspects += 1
            g.warn("/posts/%s/ … %s" % (slug, " / ".join(reasons)))
    if not suspects:
        g.ok("説明書や覚え書きが記事になっている形跡はありません")

    g = report.group("A6. 日付")
    import datetime
    today = datetime.date.today()
    bad = 0
    for post in posts:
        slug = post.get("slug") or ""
        date = post.get("date") or ""
        diary = post.get("diary_date")
        try:
            posted = datetime.date.fromisoformat(date[:10])
        except ValueError:
            g.ng("/posts/%s/ の投稿日が読み取れません: %r" % (slug, date))
            bad += 1
            continue
        if posted > today + datetime.timedelta(days=1):
            g.ng("/posts/%s/ の投稿日が未来です: %s" % (slug, posted))
            bad += 1
        if posted < rules.BLOG_START:
            g.ng("/posts/%s/ の投稿日がブログ開始（%s）より前です: %s"
                 % (slug, rules.BLOG_START, posted))
            bad += 1
        if diary:
            try:
                day = datetime.date.fromisoformat(diary)
            except ValueError:
                g.ng("/posts/%s/ の日記の日付が読み取れません: %r" % (slug, diary))
                bad += 1
                continue
            if posted < day:
                g.ng("/posts/%s/ は投稿日(%s)が日記の日付(%s)より前です" % (slug, posted, day))
                bad += 1
            elif (posted - day).days >= rules.POST_DATE_GAP_WARN:
                g.warn("/posts/%s/ は日記の日付(%s)から %d 日後に投稿されています"
                       % (slug, day, (posted - day).days))
    # URL が日付の形をしているなら、日記の日付と一致していなければならない。
    # ずれていると、/posts/2026-08-06/ に別の日の日記が入ってしまう。
    for post in posts:
        slug = post.get("slug") or ""
        diary = post.get("diary_date")
        tail = slug.split("/")[-1]
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(-\d+)?", tail)
        if m and diary and m.group(1) != diary:
            g.ng("/posts/%s/ の URL の日付（%s）と日記の日付（%s）が合っていません"
                 % (slug, m.group(1), diary))
            bad += 1

    dates = collections.Counter(p.get("date") for p in posts)
    tied = {d: n for d, n in dates.items() if n > 1}
    if tied:
        g.warn("投稿日時がまったく同じ記事があります（並び順は URL で決めています）: %s"
               % "、".join("%s が %d 件" % (d, n) for d, n in sorted(tied.items())))
    if not bad:
        g.ok("投稿日・日記の日付に矛盾はありません（%d 件）" % len(posts))

    g = report.group("A7. 本文の書式と文字")
    bad = 0
    for entry, kind in ([(p, "記事") for p in posts] + [(p, "固定ページ") for p in pages]):
        where = entry.get("slug") or os.path.basename(entry["_path"])
        body = entry.get("_body", "")
        raw = raw_bytes(entry["_path"])
        if raw.startswith(b"\xef\xbb\xbf"):
            g.ng("%s %s: ファイルの先頭に BOM が入っています" % (kind, where))
            bad += 1
        if b"\r\n" in raw:
            g.ng("%s %s: 改行が CRLF になっています（LF にしてください）" % (kind, where))
            bad += 1
        if not raw.endswith(b"\n"):
            g.warn("%s %s: ファイルが改行で終わっていません" % (kind, where))
        if "�" in body:
            g.ng("%s %s: 文字化け（置換文字）が入っています" % (kind, where))
            bad += 1
        if " " in body:
            g.warn("%s %s: 改行しない空白（NBSP）が入っています" % (kind, where))
        if re.search(r"^---\s*$", body, re.M):
            g.ng("%s %s: 本文の中に --- だけの行があります"
                 "（フロントマターの区切りと取り違えられます）" % (kind, where))
            bad += 1
        braces = re.findall(r"\{\{\w+\}\}", body)
        if braces:
            g.warn("%s %s: 本文に %s があります（型の差し込み記号と同じ形です）"
                   % (kind, where, "・".join(sorted(set(braces)))))
        parser = TagBalance()
        try:
            parser.feed(body)
            parser.close()
        except Exception as exc:                       # noqa: BLE001
            g.ng("%s %s: 本文を読み取れません: %s" % (kind, where, exc))
            bad += 1
            continue
        for problem in parser.problems[:3]:
            g.ng("%s %s: %s" % (kind, where, problem))
            bad += 1
    if not bad:
        g.ok("本文の書式・文字づかいに問題はありません（%d 件）" % (len(posts) + len(pages)))

    g = report.group("A8. 本文が指している画像")
    bad = 0
    checked = 0
    for post in posts:
        slug = post.get("slug") or ""
        depth = ("posts/%s/" % slug).count("/")
        here = os.path.join(common.DOCS, "posts", slug.replace("/", os.sep))
        for m in re.finditer(r'(?:src|href)="([^"]+)"', post.get("_body", "")):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#", "data:")):
                continue
            checked += 1
            if target.startswith("/"):
                g.ng("/posts/%s/ の本文が絶対パスを指しています: %s" % (slug, target))
                bad += 1
                continue
            resolved = os.path.normpath(os.path.join(
                here, urllib.parse.unquote(target).replace("/", os.sep)))
            if not os.path.isfile(resolved):
                g.ng("/posts/%s/ の本文が指すファイルがありません: %s"
                     "（記事の深さ %d 段から数えています）" % (slug, target, depth))
                bad += 1
    if not bad:
        g.ok("本文が指している %d 件のファイルは、すべて実在します" % checked)


# ---------------------------------------------------------------- B. 公開済みの記録

def check_baseline(report):
    g = report.group("B. 公開済みの記事の記録（baseline.json）")
    base = baseline.load_baseline()
    if base is None:
        g.ng("baseline.json がありません。python tools/baseline.py --update で作ってください")
        return
    unlocked = baseline.load_unlocked()
    if unlocked:
        g.note("修正許可中の記事: %s" % "、".join(sorted(unlocked)))
    errors, warnings, notes = baseline.compare(base, baseline.snapshot(), unlocked)
    for line in notes:
        g.note(line)
    for line in warnings:
        g.warn(line)
    for line in errors:
        g.ng(line)
    if not errors and not warnings:
        g.ok("記事 %d 件・旧URL %d 件・手で用意した公開ファイル %d 件、すべて記録どおりです"
             % (len(base.get("posts", {})), len(base.get("legacy_urls", {})),
                len(base.get("static_files", {}))))
    if errors:
        g.note("意図した修正なら、python tools/baseline.py --update で記録し直してください")


# ---------------------------------------------------------------- C. 公開物

def build_in_temp():
    """一時フォルダで docs を作り直す。リポジトリには一切書き込まない。

    docs/images と docs/assets も渡す。build.py はプロフィール画像が
    あるかどうかで出力を変えるため、渡さないと差分が出てしまう。
    """
    tmp = tempfile.mkdtemp(prefix="blog-check-")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("content", "templates", "tools"):
        src = os.path.join(common.ROOT, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(tmp, name), ignore=ignore)
    for name in ("config.json", "baseline.json"):
        src = os.path.join(common.ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(tmp, name))
    docs = os.path.join(tmp, "docs")
    os.makedirs(docs, exist_ok=True)
    for prefix in rules.NON_GENERATED_DOCS_PREFIX:
        src = os.path.join(common.DOCS, prefix.rstrip("/"))
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(docs, prefix.rstrip("/")))
    result = subprocess.run(
        [sys.executable, os.path.join(tmp, "tools", "build.py")],
        cwd=tmp, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return tmp, result


def docs_files(root):
    out = {}
    for folder, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            if name == ".build-manifest.json":
                continue
            path = os.path.join(folder, name)
            out[os.path.relpath(path, root).replace(os.sep, "/")] = path
    return out


def check_generated(report):
    if not os.path.isdir(common.DOCS):
        report.group("C. 公開物").ng("docs/ がありません。python tools/build.py を実行してください")
        return

    g = report.group("C1. 作り直しても同じになるか（再現性）")
    tmp, result = build_in_temp()
    try:
        if result.returncode != 0:
            g.ng("一時フォルダでの作り直しに失敗しました（build.py が %d で終了）"
                 % result.returncode)
            for line in (result.stderr or result.stdout or "").strip().split("\n")[-6:]:
                if line.strip():
                    g.ng("  build.py: %s" % line.strip())
            return
        manifest_path = os.path.join(tmp, ".build-manifest.json")
        if not os.path.isfile(manifest_path):
            g.ng("作り直しの記録（.build-manifest.json）ができていません")
            return
        with open(manifest_path, encoding="utf-8") as f:
            generated = set(json.load(f).get("files", []))

        here = docs_files(common.DOCS)
        there = docs_files(os.path.join(tmp, "docs"))
        differ, missing = [], []
        for rel in sorted(generated):
            a, b = here.get(rel), there.get(rel)
            if a is None:
                missing.append(rel)
                continue
            if b is None:
                continue
            if raw_bytes(a) != raw_bytes(b):
                differ.append(rel)
        for rel in missing[:10]:
            g.ng("公開物が足りません: docs/%s" % rel)
        if len(missing) > 10:
            g.ng("… ほか %d 件、公開物が足りません" % (len(missing) - 10))
        for rel in differ[:10]:
            g.ng("公開物が、記事から作り直したものと違います: docs/%s" % rel)
        if len(differ) > 10:
            g.ng("… ほか %d 件、作り直したものと違います" % (len(differ) - 10))
        if differ or missing:
            g.note("docs/ を手で直していないか確かめて、python tools/build.py で作り直してください")
        else:
            g.ok("生成される %d ファイルは、すべて記事から作り直したものと一致します" % len(generated))

        g2 = report.group("C2. 余分なファイル（孤児）")
        keep = tuple(rules.NON_GENERATED_DOCS_PREFIX)
        orphans = sorted(rel for rel in here
                         if rel not in generated and not rel.startswith(keep))
        if orphans:
            for rel in orphans[:10]:
                g2.ng("もう作られないファイルが公開されたままです: docs/%s" % rel)
            if len(orphans) > 10:
                g2.ng("… ほか %d 件" % (len(orphans) - 10))
            g2.note("消してよいか確かめてから、git rm で取り除いてください")
        else:
            g2.ok("公開フォルダに余分なファイルはありません"
                  "（生成物 %d + 手で用意したもの %d = %d ファイル）"
                  % (len(generated), len(here) - len(generated), len(here)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    g = report.group("C3. リンクと画像")
    verify.QUIET = True
    del verify.problems[:]
    try:
        verify.check_links()
    except Exception as exc:                            # noqa: BLE001
        g.ng("リンクの点検中に問題が起きました: %s" % exc)
    found = list(verify.problems)
    for line in found[:10]:
        g.ng(line)
    if len(found) > 10:
        g.ng("… ほか %d 件" % (len(found) - 10))
    if not found:
        g.ok("公開ページの中のリンクと画像は、すべて実在するファイルを指しています")

    g = report.group("C4. 旧URLからの転送")
    del verify.problems[:]
    try:
        verify.check_redirects()
    except Exception as exc:                            # noqa: BLE001
        g.ng("転送ページの点検中に問題が起きました: %s" % exc)
    found = list(verify.problems)
    for line in found[:10]:
        g.ng(line)
    if len(found) > 10:
        g.ng("… ほか %d 件" % (len(found) - 10))
    if not found:
        posts = common.load_posts()
        pages = common.load_pages()
        total = len(common.legacy_pairs(list(reversed(posts)), pages))
        g.ok("旧URL %d 件すべてに転送ページがあり、転送先も実在します（固定ページを含む）" % total)
    verify.QUIET = False

    g = report.group("C5. RSS と sitemap")
    for name in ("feed.xml", "sitemap.xml"):
        path = os.path.join(common.DOCS, name)
        if not os.path.isfile(path):
            g.warn("%s がありません（config.json の site_url が空のときは作られません）" % name)
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            g.ng("%s が XML として壊れています: %s" % (name, exc))
            continue
        if name == "sitemap.xml":
            ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
            locs = [el.text or "" for el in root.iter(ns + "loc")]
            site = (common.load_config().get("site_url") or "").rstrip("/")
            bad = 0
            for loc in locs:
                if site and not loc.startswith(site):
                    g.ng("sitemap.xml に別のサイトの URL があります: %s" % loc)
                    bad += 1
                    continue
                rel = urllib.parse.unquote(loc[len(site):]).lstrip("/")
                target = os.path.join(common.DOCS, rel.replace("/", os.sep))
                if rel == "" or rel.endswith("/"):
                    target = os.path.join(target, "index.html")
                if not os.path.isfile(target):
                    g.ng("sitemap.xml の URL に中身がありません: %s" % loc)
                    bad += 1
            if not bad:
                g.ok("sitemap.xml の %d 件の URL は、すべて実在するページです" % len(locs))
        else:
            items = list(root.iter("item"))
            if not items:
                g.warn("feed.xml に記事が入っていません")
            else:
                g.ok("feed.xml は正しい XML で、記事 %d 件が入っています" % len(items))

    g = report.group("C6. 欠かせないファイル")
    required = ("index.html", "404.html", ".nojekyll", "assets/style.css",
                "archive/index.html", "categories/index.html")
    missing = [rel for rel in required
               if not os.path.exists(os.path.join(common.DOCS, rel.replace("/", os.sep)))]
    for rel in missing:
        g.ng("docs/%s がありません" % rel)
    if not missing:
        size = sum(os.path.getsize(p) for p in docs_files(common.DOCS).values())
        g.ok("%s がそろっています（公開フォルダ %.1f MB）"
             % (" / ".join(required), size / 1024 / 1024))


# ---------------------------------------------------------------- D. リポジトリ衛生

def tracked_files():
    """git が追跡しているファイルの一覧。日本語の名前もそのまま得る。"""
    result = subprocess.run(["git", "ls-files", "-z"], cwd=common.ROOT,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.split("\0") if p]


# 判定のもとになる文字列そのものを持っているため、漏えいの点検から外す。
LEAK_SCAN_SKIP = ("tools/rules.py", "tools/check.py", "OPERATIONS.md")


def check_hygiene(report):
    g = report.group("D1. 公開してはいけないファイル")
    files = tracked_files()
    if files is None:
        g.warn("git の情報が読めないため、この点検を省きました")
        return
    blocked = [(f, rules.never_publish(f)) for f in files]
    blocked = [(f, why) for f, why in blocked if why]
    for path, why in blocked:
        g.ng("%s … %s" % (path, why))
    if blocked:
        g.note(".gitignore を直し、git rm --cached で取り除いてください")
    else:
        g.ok("追跡されている %d ファイルに、公開してはいけないものはありません"
             "（RSS と sitemap は公開して当然のものとして許可しています）" % len(files))

    g = report.group("D2. 個人情報・認証情報の混入")
    hits = 0
    for path in files:
        if path in LEAK_SCAN_SKIP:
            continue
        full = os.path.join(common.ROOT, path)
        if not os.path.isfile(full):
            continue
        if os.path.getsize(full) > 2 * 1024 * 1024:
            continue
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        for reason in rules.leaks_in(text):
            g.ng("%s に %s が入っています（値はここに出しません）" % (path, reason))
            hits += 1
    if not hits:
        g.ok("認証情報や個人のフォルダのパスらしい記述は見つかりませんでした")

    g = report.group("D3. 置き忘れたファイル")
    stray = []
    for path in files:
        base = os.path.basename(path)
        if re.search(r"(?i)\.(bak|orig|tmp|swp)$", base) \
                or base in ("Thumbs.db", "desktop.ini", ".DS_Store"):
            stray.append(path)
    for path in stray:
        g.ng("控えや不要ファイルが追跡されています: %s" % path)
    content_stray = [p for p in files
                     if p.startswith("content/") and not p.endswith(".html")]
    for path in content_stray:
        g.ng("content/ に記事ではないファイルがあります: %s" % path)
    if not stray and not content_stray:
        g.ok("置き忘れたファイルはありません")


# ---------------------------------------------------------------- 入口

def write_summary(report):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("# ブログの自動検査\n\n")
            f.write(report.markdown())
            f.write("\n")
    except OSError as exc:
        print("  [注意] 結果の書き出しに失敗しました: %s" % exc)


def main():
    args = sys.argv[1:]
    only_hygiene = any(a in ("--衛生のみ", "--hygiene") for a in args)
    only_sources = any(a in ("--記事のみ", "--sources") for a in args)
    as_json = any(a in ("--json",) for a in args)
    for_ci = any(a in ("--ci",) for a in args)

    report = Report()
    if only_hygiene:
        check_hygiene(report)
    elif only_sources:
        check_sources(report, common.load_posts(), common.load_pages())
    else:
        check_sources(report, common.load_posts(), common.load_pages())
        check_baseline(report)
        check_generated(report)
        check_hygiene(report)

    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
        return 1 if report.errors else 0

    print("=" * 56)
    print(" ブログの自動検査")
    print("=" * 56)
    report.print_text()

    if for_ci:
        write_summary(report)

    print("\n" + "=" * 56)
    print("問題 %d 件 / 注意 %d 件" % (len(report.errors), len(report.warnings)))
    if report.errors:
        print("\n直す必要があるもの:")
        for title, line in report.errors[:15]:
            print("  [NG] %s" % line)
        if len(report.errors) > 15:
            print("  … ほか %d 件" % (len(report.errors) - 15))
        print("\n公開はしないでください。上の内容を直してから、もう一度実行してください。")
        return 1
    if report.warnings:
        print("注意はありますが、公開を止めるほどのものではありません。")
    print("すべての点検を通りました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
