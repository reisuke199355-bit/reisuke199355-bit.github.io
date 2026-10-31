# -*- coding: utf-8 -*-
"""content/ の記事から docs/ の静的サイトを組み立てる。

使い方:
    python build.py

生成したファイルは docs/.build-manifest.json に記録し、次回のビルドで
不要になったものだけを消す。docs/images と docs/assets には手を触れない。
"""
import datetime
import io
import json
import os
import re
import shutil
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

MANIFEST = os.path.join(common.ROOT, ".build-manifest.json")
FORBIDDEN = set('\\/:*?"<>|')
written_files = []


def tpl(name):
    with open(os.path.join(common.TEMPLATES, name), encoding="utf-8") as f:
        return f.read()


def render(template, **kw):
    out = template
    for key, value in kw.items():
        out = out.replace("{{%s}}" % key, "" if value is None else str(value))
    return re.sub(r"\{\{\w+\}\}", "", out)


def write(rel_path, text):
    """docs/ 以下へ書き出し、生成物として記録する。"""
    abs_path = os.path.join(common.DOCS, rel_path.replace("/", os.sep))
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    written_files.append(rel_path)


class Site:
    def __init__(self):
        self.cfg = common.load_config()
        self.site_url = (self.cfg.get("site_url") or "").rstrip("/")
        self.base = tpl("base.html")
        self.posts = list(reversed(common.load_posts()))  # 新しい記事が先
        self.pages = []
        if os.path.isdir(common.CONTENT_PAGES):
            for name in sorted(os.listdir(common.CONTENT_PAGES)):
                if name.endswith(".html"):
                    self.pages.append(common.read_entry(os.path.join(common.CONTENT_PAGES, name)))

    # ---- 共通部品 ----

    def root_for(self, depth):
        return common.relpath_prefix(depth)

    def nav_html(self, root):
        return "".join('<a href="%s%s">%s</a>' % (root, n["href"], common.esc(n["label"]))
                       for n in self.cfg.get("nav", []))

    def footer_nav_html(self, root):
        """フッターに並べるリンク（プライバシーポリシーなど）。"""
        return "".join(' ・ <a href="%s%s">%s</a>' % (root, n["href"], common.esc(n["label"]))
                       for n in self.cfg.get("footer_nav", []))

    def feed_bits(self, root):
        if not self.site_url:
            return "", ""
        link = ('<link rel="alternate" type="application/rss+xml" title="%s" href="%sfeed.xml">\n'
                % (common.esc(self.cfg["site_title"]), root))
        foot = ' ・ <a href="%sfeed.xml">RSS</a>' % root
        return link, foot

    def canonical(self, rel_url):
        if not self.site_url:
            return ""
        return '<link rel="canonical" href="%s/%s">\n' % (self.site_url, rel_url.lstrip("/"))

    def shell(self, rel_out, depth, page_title, description, content,
              og_type="website", canonical_url=None):
        root = self.root_for(depth)
        feed_link, feed_foot = self.feed_bits(root)
        html = render(
            self.base,
            page_title=common.esc(page_title),
            description=common.esc(description),
            og_type=og_type,
            site_title=common.esc(self.cfg["site_title"]),
            root=root,
            nav=self.nav_html(root),
            content=content,
            canonical=self.canonical(canonical_url) if canonical_url else "",
            feed_link=feed_link,
            feed_foot=feed_foot,
            footer_nav=self.footer_nav_html(root),
        )
        write(rel_out, html)

    def cat_slug(self, name):
        return self.cfg.get("category_slug_map", {}).get(name)

    def cat_links(self, categories, root):
        out = []
        for name in categories:
            slug = self.cat_slug(name)
            if slug:
                out.append('<a class="tag" href="%scategories/%s/">%s</a>'
                           % (root, slug, common.esc(name)))
            else:
                out.append('<span class="tag">%s</span>' % common.esc(name))
        return "".join(out)

    def excerpt(self, post, limit=110):
        text = common.strip_tags(post.get("_body", ""))
        return text[:limit] + ("…" if len(text) > limit else "")

    def card(self, post, root):
        return (
            '<li><a class="card" href="%sposts/%s/">'
            '<span class="card-meta"><time datetime="%s">%s</time>%s</span>'
            '<span class="card-title">%s</span>'
            '<span class="card-excerpt">%s</span>'
            "</a></li>"
            % (root, post["slug"],
               post["date"][:10], common.format_date_ja(post["date"]),
               self.cat_links(post.get("categories", []), root),
               common.esc(post["title"]), common.esc(self.excerpt(post)))
        )

    # ---- 各ページ ----

    def build_index(self):
        per = int(self.cfg.get("posts_per_page", 20))
        total = len(self.posts)
        pages = max(1, (total + per - 1) // per)
        list_tpl = tpl("list.html")

        for i in range(pages):
            chunk = self.posts[i * per:(i + 1) * per]
            if i == 0:
                rel_out, depth, rel_url = "index.html", 0, ""
            else:
                rel_out, depth, rel_url = "page/%d/index.html" % (i + 1), 2, "page/%d/" % (i + 1)
            root = self.root_for(depth)

            links = []
            if i + 1 < pages:
                nxt = "%spage/%d/" % (root, i + 2)
                links.append('<a href="%s">← 古い記事</a>' % nxt)
            else:
                links.append("<span></span>")
            links.append('<span class="count">%d / %d ページ（全 %d 記事）</span>'
                         % (i + 1, pages, total))
            if i == 1:
                links.append('<a href="%s">新しい記事 →</a>' % root)
            elif i > 1:
                links.append('<a href="%spage/%d/">新しい記事 →</a>' % (root, i))
            else:
                links.append("<span></span>")

            body = render(
                list_tpl,
                list_title="日記" if i == 0 else "日記（%d ページ目）" % (i + 1),
                list_note='<p class="list-note">%s</p>' % common.esc(self.cfg["site_description"])
                if i == 0 else "",
                items="\n".join(self.card(p, root) for p in chunk),
                pagination='<nav class="pagination">%s</nav>' % "".join(links) if pages > 1 else "",
            )
            title = self.cfg["site_title"] if i == 0 else \
                "%s - %d ページ目" % (self.cfg["site_title"], i + 1)
            self.shell(rel_out, depth, title, self.cfg["site_description"], body,
                       canonical_url=rel_url)
        print("  トップと一覧: %d ページ" % pages)

    def build_posts(self):
        post_tpl = tpl("post.html")
        root = self.root_for(2)
        n = len(self.posts)
        for i, post in enumerate(self.posts):
            newer = self.posts[i - 1] if i > 0 else None   # 一覧は新しい順
            older = self.posts[i + 1] if i + 1 < n else None
            prev_link = next_link = ""
            if older:
                prev_link = ('<a class="prev" href="%sposts/%s/">'
                             '<span class="dir">前の記事</span>'
                             '<span class="label">%s</span></a>'
                             % (root, older["slug"], common.esc(older["title"])))
            if newer:
                next_link = ('<a class="next" href="%sposts/%s/">'
                             '<span class="dir">次の記事</span>'
                             '<span class="label">%s</span></a>'
                             % (root, newer["slug"], common.esc(newer["title"])))
            body = render(
                post_tpl,
                title=common.esc(post["title"]),
                date_iso=post["date"][:10],
                date_label=common.format_date_ja(post["date"]),
                category_links=self.cat_links(post.get("categories", []), root),
                body=post["_body"],
                prev_link=prev_link,
                next_link=next_link,
            )
            rel_url = "posts/%s/" % post["slug"]
            self.shell(rel_url + "index.html", 2,
                       "%s - %s" % (post["title"], self.cfg["site_title"]),
                       self.excerpt(post, 100), body,
                       og_type="article", canonical_url=rel_url)
        print("  記事ページ: %d 件" % n)

    def build_archive(self):
        groups = []
        for post in self.posts:
            key = post["date"][:7]
            if not groups or groups[-1][0] != key:
                groups.append((key, []))
            groups[-1][1].append(post)
        root = self.root_for(1)
        chunks = []
        for key, items in groups:
            year, month = key.split("-")
            rows = "".join(
                '<li><span class="d">%d/%d</span><a href="%sposts/%s/">%s</a></li>'
                % (int(p["date"][5:7]), int(p["date"][8:10]), root, p["slug"],
                   common.esc(p["title"]))
                for p in items)
            chunks.append('<section class="month-group"><h2 class="month-head">%d年%d月'
                          '（%d件）</h2><ul class="compact">%s</ul></section>'
                          % (int(year), int(month), len(items), rows))
        body = ('<div class="list-head"><h1 class="list-title">全記事一覧</h1>'
                '<p class="list-note">全 %d 記事。投稿日の新しい順に並んでいます。</p></div>%s'
                % (len(self.posts), "".join(chunks)))
        self.shell("archive/index.html", 1,
                   "全記事一覧 - %s" % self.cfg["site_title"],
                   "全 %d 記事の一覧。" % len(self.posts), body, canonical_url="archive/")
        print("  全記事一覧: 1 ページ（%d か月分）" % len(groups))

    def build_categories(self):
        buckets = {}
        for post in self.posts:
            for name in post.get("categories", []):
                buckets.setdefault(name, []).append(post)
        root = self.root_for(2)
        count = 0
        for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            slug = self.cat_slug(name)
            if not slug:
                print("  [注意] カテゴリー「%s」はURL用の名前が未設定のため作成しません。" % name)
                continue
            body = render(
                tpl("list.html"),
                list_title="カテゴリー：%s" % common.esc(name),
                list_note='<p class="list-note">%d 記事</p>' % len(items),
                items="\n".join(self.card(p, root) for p in items),
                pagination="",
            )
            rel_url = "categories/%s/" % slug
            self.shell(rel_url + "index.html", 2,
                       "%s - %s" % (name, self.cfg["site_title"]),
                       "カテゴリー「%s」の記事 %d 件。" % (name, len(items)), body,
                       canonical_url=rel_url)
            count += 1
        print("  カテゴリー: %d ページ" % count)

    def build_pages(self):
        page_tpl = tpl("page.html")
        for page in self.pages:
            body = render(page_tpl, title=common.esc(page["title"]), body=page["_body"])
            rel_url = "%s/" % page["slug"]
            self.shell(rel_url + "index.html", 1,
                       "%s - %s" % (page["title"], self.cfg["site_title"]),
                       common.strip_tags(page["_body"])[:100], body,
                       canonical_url=rel_url)
        print("  固定ページ: %d 件" % len(self.pages))

    def build_redirects(self):
        """旧サイトのURLと同じ場所に、新しい記事への案内ページを置く。"""
        redirect_tpl = tpl("redirect.html")
        root = self.root_for(1)
        pairs = []
        for post in self.posts:
            legacy = urllib.parse.unquote(post.get("legacy_slug", "") or "")
            if legacy:
                pairs.append((legacy, "posts/%s/" % post["slug"], post["title"]))
        for page in self.pages:
            legacy = urllib.parse.unquote(page.get("legacy_slug", "") or "")
            if legacy and legacy not in ("ホーム",):
                pairs.append((legacy, "%s/" % page["slug"], page["title"]))
        pairs.append(("日記", "archive/", "全記事一覧"))

        made = skipped = 0
        for legacy, target, title in pairs:
            if FORBIDDEN & set(legacy) or legacy in (".", ".."):
                print("  [スキップ] 旧URL「%s」はフォルダ名に使えない文字を含みます。" % legacy)
                skipped += 1
                continue
            html = render(
                redirect_tpl,
                title=common.esc(title),
                target=root + target,
                root=root,
                site_title=common.esc(self.cfg["site_title"]),
                canonical=self.canonical(target),
            )
            write("%s/index.html" % legacy, html)
            made += 1
        print("  旧URLからの転送ページ: %d 枚（スキップ %d 件）" % (made, skipped))

    def build_404(self):
        """404 は任意のパスで表示されるため、相対パスを使わず自己完結させる。"""
        base_path = self.cfg.get("base_path") or "/"
        if not base_path.endswith("/"):
            base_path += "/"
        html = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ページが見つかりません - %(site)s</title>
<meta name="robots" content="noindex">
<style>
body{margin:0;background:#fcfbf8;color:#272523;font-family:"Hiragino Kaku Gothic ProN","Yu Gothic UI","Noto Sans JP",system-ui,sans-serif;line-height:1.95}
main{max-width:40rem;margin:0 auto;padding:72px 16px}
.notice{padding:28px 24px;border:1px solid #e6e2da;border-radius:10px;background:#fff}
h1{margin:0 0 .7em;font-size:1.1875rem;line-height:1.6}
p{margin:0 0 1em}
a{color:#2f6d62;text-underline-offset:.18em}
a.button{display:inline-block;padding:10px 20px;border-radius:10px;background:#2f6d62;color:#fff;font-weight:700;text-decoration:none}
.sub{margin:0;color:#6b6760;font-size:.875rem}
@media (prefers-color-scheme:dark){
body{background:#16171a;color:#e4e2dd}
.notice{background:#1e2024;border-color:#2c2f34}
a{color:#7fc9b8}
a.button{background:#7fc9b8;color:#14201e}
.sub{color:#a3a09a}
}
</style>
</head>
<body>
<main>
  <div class="notice">
    <h1>ページが見つかりません</h1>
    <p>お探しのページは移動したか、削除された可能性があります。</p>
    <p><a class="button" href="%(base)s">トップへ戻る</a></p>
    <p class="sub"><a href="%(base)sarchive/">全記事一覧から探す</a></p>
  </div>
</main>
</body>
</html>
""" % {"site": common.esc(self.cfg["site_title"]), "base": base_path}
        write("404.html", html)
        print("  404 ページ: 1 件（トップへのリンクは base_path = %s）" % base_path)

    def build_feed_and_sitemap(self):
        if not self.site_url:
            print("  [注意] config.json の site_url が空のため feed.xml と sitemap.xml は作りません。")
            print("         GitHub Pages のアドレスが決まったら site_url を設定して再ビルドしてください。")
            return
        tz = self.cfg.get("timezone", "+09:00")

        def rfc822(value):
            try:
                dt = datetime.datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return ""
            return dt.strftime("%a, %d %b %Y %H:%M:%S ") + tz.replace(":", "")

        items = []
        for post in self.posts[:30]:
            url = "%s/posts/%s/" % (self.site_url, post["slug"])
            items.append(
                "<item><title>%s</title><link>%s</link><guid isPermaLink=\"true\">%s</guid>"
                "<pubDate>%s</pubDate><description>%s</description></item>"
                % (common.esc(post["title"]), url, url, rfc822(post["date"]),
                   common.esc(self.excerpt(post, 160))))
        write("feed.xml",
              '<?xml version="1.0" encoding="UTF-8"?>\n'
              '<rss version="2.0"><channel>'
              "<title>%s</title><link>%s/</link><description>%s</description>"
              "<language>ja</language>%s</channel></rss>\n"
              % (common.esc(self.cfg["site_title"]), self.site_url,
                 common.esc(self.cfg["site_description"]), "".join(items)))

        urls = ["", "archive/"]
        urls += ["posts/%s/" % p["slug"] for p in self.posts]
        urls += ["%s/" % p["slug"] for p in self.pages]
        urls += ["categories/%s/" % self.cat_slug(c) for c in self.cfg.get("category_slug_map", {})
                 if self.cat_slug(c)]
        write("sitemap.xml",
              '<?xml version="1.0" encoding="UTF-8"?>\n'
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>\n'
              % "".join("<url><loc>%s/%s</loc></url>" % (self.site_url, u) for u in urls))
        print("  feed.xml と sitemap.xml: 作成しました（%s）" % self.site_url)

    def build(self):
        os.makedirs(common.DOCS, exist_ok=True)
        write(".nojekyll", "")
        print("サイトを組み立てます。")
        self.build_index()
        self.build_posts()
        self.build_archive()
        self.build_categories()
        self.build_pages()
        self.build_redirects()
        self.build_404()
        self.build_feed_and_sitemap()


def sweep_old_files():
    """前回のビルドで作ったが今回は作らなかったファイルだけを消す。"""
    if not os.path.exists(MANIFEST):
        return 0
    with open(MANIFEST, encoding="utf-8") as f:
        previous = set(json.load(f).get("files", []))
    removed = 0
    for rel in sorted(previous - set(written_files), reverse=True):
        abs_path = os.path.join(common.DOCS, rel.replace("/", os.sep))
        if os.path.isfile(abs_path):
            os.remove(abs_path)
            removed += 1
            folder = os.path.dirname(abs_path)
            while folder.startswith(common.DOCS) and folder != common.DOCS:
                if os.path.isdir(folder) and not os.listdir(folder):
                    shutil.rmtree(folder, ignore_errors=True)
                    folder = os.path.dirname(folder)
                else:
                    break
    return removed


def main():
    if not os.path.isdir(common.CONTENT_POSTS) or not os.listdir(common.CONTENT_POSTS):
        print("content/posts に記事がありません。先に記事を取り込んでください。")
        return 1
    site = Site()
    site.build()
    removed = sweep_old_files()
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump({"files": sorted(written_files),
                   "built_at": datetime.datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False, indent=1)
    print("\n完了：%d ファイルを docs/ に出力しました（不要になった %d ファイルを削除）。"
          % (len(written_files), removed))
    print("記事 %d 件・固定ページ %d 件。" % (len(site.posts), len(site.pages)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
