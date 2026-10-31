# -*- coding: utf-8 -*-
"""生成したサイトを点検する。

使い方:
    python verify.py                      リンクと記事の点検
    python verify.py <WordPress.xml>      さらに、移行元 XML と本文を1文字ずつ照合する

確かめること:
  1. 記事の数、タイトル、投稿日、本文が移行元と一致しているか
  2. ページの中のリンクと画像が、すべて実在するファイルを指しているか
  3. 旧URLの転送ページが、すべて正しい記事へ向いているか
"""
import io
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

problems = []


def fail(message):
    problems.append(message)
    print("  [NG] %s" % message)


def squash(text):
    """タグを外し、空白をすべて取り除いた文字列にする（本文の照合用）。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", "", text)


def check_against_xml(xml_path):
    ns = {"wp": "http://wordpress.org/export/1.2/",
          "content": "http://purl.org/rss/1.0/modules/content/"}

    def t(el, path):
        found = el.find(path, ns)
        return (found.text or "") if found is not None else ""

    channel = ET.parse(xml_path).getroot().find("channel")
    original = {}
    for item in channel.findall("item"):
        if t(item, "wp:post_type") == "post" and t(item, "wp:status") == "publish":
            original[t(item, "wp:post_id")] = {
                "title": t(item, "title"),
                "date": t(item, "wp:post_date"),
                "body": squash(t(item, "content:encoded")),
                "cats": sorted(c.text or "" for c in item.findall("category")
                               if c.get("domain") == "category"),
            }

    posts = {p.get("wp_id"): p for p in common.load_posts() if p.get("wp_id")}
    print("\n### 1. 移行元 XML との照合")
    print("  移行元の公開記事: %d 件 / 取り込み済み: %d 件" % (len(original), len(posts)))
    if len(original) != len(posts):
        fail("記事数が一致しません。")

    missing = set(original) - set(posts)
    extra = set(posts) - set(original)
    for wp_id in sorted(missing):
        fail("記事が取り込まれていません: id=%s 「%s」" % (wp_id, original[wp_id]["title"]))
    for wp_id in sorted(extra):
        fail("移行元に無い記事があります: id=%s" % wp_id)

    same_title = same_date = same_body = same_cats = 0
    for wp_id, src in original.items():
        got = posts.get(wp_id)
        if not got:
            continue
        if got["title"] == src["title"]:
            same_title += 1
        else:
            fail("タイトルが違います id=%s: 「%s」 → 「%s」"
                 % (wp_id, src["title"], got["title"]))
        if got["date"] == src["date"]:
            same_date += 1
        else:
            fail("投稿日が違います id=%s: %s → %s" % (wp_id, src["date"], got["date"]))
        if squash(got["_body"]) == src["body"]:
            same_body += 1
        else:
            a, b = src["body"], squash(got["_body"])
            at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
            fail("本文が違います id=%s（%d文字目から / 元%d字 → 今%d字）: ...%s / ...%s"
                 % (wp_id, at + 1, len(a), len(b), a[at:at + 30], b[at:at + 30]))
        if sorted(got.get("categories", [])) == src["cats"]:
            same_cats += 1
        else:
            fail("カテゴリーが違います id=%s: %s → %s"
                 % (wp_id, src["cats"], got.get("categories")))

    print("  タイトル一致: %d / %d" % (same_title, len(original)))
    print("  投稿日一致  : %d / %d" % (same_date, len(original)))
    print("  本文一致    : %d / %d" % (same_body, len(original)))
    print("  カテゴリ一致: %d / %d" % (same_cats, len(original)))


def all_html_files():
    for folder, _, names in os.walk(common.DOCS):
        for name in names:
            if name.endswith(".html"):
                yield os.path.join(folder, name)


def check_links():
    print("\n### 2. リンクと画像の点検")
    files = list(all_html_files())
    total = broken = 0
    pattern = re.compile(r'(?:href|src)="([^"]+)"')
    for path in files:
        with open(path, encoding="utf-8") as f:
            html = f.read()
        here = os.path.dirname(path)
        for raw in pattern.findall(html):
            if raw.startswith(("http://", "https://", "mailto:", "#", "data:")):
                continue
            target = urllib.parse.unquote(raw.split("#")[0].split("?")[0])
            if not target:
                continue
            if target.startswith("/"):
                # 404.html だけは base_path を使った絶対パスなので対象外
                if os.path.basename(path) == "404.html":
                    continue
                fail("絶対パスのリンクがあります: %s （%s）"
                     % (raw, os.path.relpath(path, common.DOCS)))
                broken += 1
                continue
            total += 1
            resolved = os.path.normpath(os.path.join(here, target.replace("/", os.sep)))
            if target.endswith("/") or os.path.isdir(resolved):
                resolved = os.path.join(resolved, "index.html")
            if not os.path.isfile(resolved):
                fail("リンク先がありません: %s → %s （%s）"
                     % (raw, os.path.relpath(resolved, common.DOCS),
                        os.path.relpath(path, common.DOCS)))
                broken += 1
    print("  HTML ファイル: %d 枚 / 内部リンク・画像: %d 件 / 切れているもの: %d 件"
          % (len(files), total, broken))


def check_redirects():
    print("\n### 3. 旧URLからの転送ページの点検")
    posts = common.load_posts()
    expected = {}
    for post in posts:
        legacy = urllib.parse.unquote(post.get("legacy_slug", "") or "")
        if legacy:
            expected[legacy] = "posts/%s/" % post["slug"]

    found = checked = 0
    for legacy, target in sorted(expected.items()):
        path = os.path.join(common.DOCS, legacy, "index.html")
        if not os.path.isfile(path):
            fail("転送ページがありません: /%s/" % legacy)
            continue
        found += 1
        with open(path, encoding="utf-8") as f:
            html = f.read()
        m = re.search(r'http-equiv="refresh" content="0; url=([^"]+)"', html)
        if not m:
            fail("転送の指定が見当たりません: /%s/" % legacy)
            continue
        got = m.group(1)
        if got != "../" + target:
            fail("転送先が違います /%s/: %s （期待 ../%s）" % (legacy, got, target))
            continue
        resolved = os.path.normpath(os.path.join(common.DOCS, legacy,
                                                 got.replace("/", os.sep), "index.html"))
        if not os.path.isfile(resolved):
            fail("転送先のファイルがありません: /%s/ → %s" % (legacy, got))
            continue
        checked += 1
    print("  旧URL: %d 件 / 転送ページ: %d 枚 / 転送先まで確認できたもの: %d 枚"
          % (len(expected), found, checked))


def check_misc():
    print("\n### 4. そのほか")
    for rel in ("index.html", "404.html", ".nojekyll", "assets/style.css", "archive/index.html"):
        path = os.path.join(common.DOCS, rel.replace("/", os.sep))
        print("  %s %s" % ("[OK]" if os.path.exists(path) else "[NG]", rel))
        if not os.path.exists(path):
            fail("%s がありません。" % rel)
    slugs = {}
    for post in common.load_posts():
        slugs.setdefault(post["slug"], []).append(post.get("wp_id") or post["title"])
    duplicated = {k: v for k, v in slugs.items() if len(v) > 1}
    if duplicated:
        fail("URL が重複しています: %s" % duplicated)
    else:
        print("  [OK] URL の重複なし（%d 件すべて一意）" % len(slugs))
    size = sum(os.path.getsize(os.path.join(f, n))
               for f, _, ns in os.walk(common.DOCS) for n in ns)
    print("  公開フォルダの合計サイズ: %.1f MB" % (size / 1024 / 1024))


def main():
    if not os.path.isdir(common.DOCS):
        print("docs/ がありません。先に build.py を実行してください。")
        return 1
    xml_path = next((a for a in sys.argv[1:] if a.lower().endswith(".xml")), None)
    if xml_path:
        check_against_xml(xml_path)
    else:
        print("\n### 1. 移行元 XML との照合 … XML が指定されていないので省略します。")
    check_links()
    check_redirects()
    check_misc()

    print("\n" + "=" * 56)
    if problems:
        print("問題が %d 件あります。" % len(problems))
        return 1
    print("すべての点検を通りました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
