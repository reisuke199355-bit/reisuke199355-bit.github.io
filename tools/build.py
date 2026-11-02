# -*- coding: utf-8 -*-
"""content/ の記事から docs/ の静的サイトを組み立てる。

使い方:
    python build.py

生成したファイルの一覧は、リポジトリ直下の .build-manifest.json に記録し、
次回のビルドで不要になったものだけを消す（この控えは GitHub へ送らない）。
docs/images と docs/assets には手を触れない。
"""
import datetime
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import rules

common.use_utf8_stdout()

MANIFEST = os.path.join(common.ROOT, ".build-manifest.json")
FORBIDDEN = set('\\/:*?"<>|')
written_files = []
# 「出力先 -> それを作ろうとした相手」の控え。二重書き込みを見つけるために使う。
written_by = {}

PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def tpl(name):
    with open(os.path.join(common.TEMPLATES, name), encoding="utf-8") as f:
        return f.read()


def render(template, **kw):
    """型の {{名前}} を値に差し替える。

    型の中だけを1回見て差し替える。差し込んだ値の中をもう一度見ないので、
    記事の本文に {{...}} と書かれていても、それが消えたり差し替わったりしない。
    （以前は値を入れたあとに全体から {{語}} を消していたため、本文の
      {{...}} が黙って消えてしまっていた）
    """
    def pick(m):
        key = m.group(1)
        if key not in kw:
            return ""              # 型に残った差し込み記号は空にする
        value = kw[key]
        return "" if value is None else str(value)
    return PLACEHOLDER.sub(pick, template)


def write(rel_path, text, made_by=""):
    """docs/ 以下へ書き出し、生成物として記録する。

    公開フォルダの外へ出ないこと、同じ場所へ2回書かないことを必ず確かめる。
    2回書きを止めるのが大事で、旧URLの転送ページと生成ページがぶつかっても、
    これまでは後から書いたほうが黙って勝っていた。
    """
    docs_real = os.path.realpath(common.DOCS)
    abs_path = os.path.realpath(os.path.join(common.DOCS, rel_path.replace("/", os.sep)))
    if abs_path != docs_real and not abs_path.startswith(docs_real + os.sep):
        print("\n*** 中断します：公開フォルダの外へ書き出そうとしました ***")
        print("    書き出し先: %s" % rel_path)
        print("    記事の URL（slug）や旧URLに「..」や円記号が入っていないか"
              "確かめてください。")
        raise SystemExit(1)
    key = rel_path.replace("\\", "/").lower()
    if key in written_by:
        print("\n*** 中断します：同じ場所へ2回書き出そうとしました ***")
        print("    場所: docs/%s" % rel_path)
        print("    1回目: %s" % written_by[key])
        print("    2回目: %s" % (made_by or "(不明)"))
        print("    どちらかの名前が重なっています。"
              "カテゴリー名・記事の URL・旧URL を見直してください。")
        raise SystemExit(1)
    written_by[key] = made_by or rel_path
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
        self.has_tags = any(self.visible_tags(p) for p in self.posts)
        self.pages = []
        if os.path.isdir(common.CONTENT_PAGES):
            for name in sorted(os.listdir(common.CONTENT_PAGES)):
                if name.endswith(".html"):
                    self.pages.append(common.read_entry(os.path.join(common.CONTENT_PAGES, name)))

    # ---- 共通部品 ----

    def root_for(self, depth):
        return common.relpath_prefix(depth)

    def social_url(self, key):
        """X などの外部サービスのURL。設定ファイル1か所で管理する。

        config.json の social_hidden に名前を入れておくと、URL は残したまま
        リンクだけ画面に出さない（アカウント凍結中などの一時的な非表示用）。
        """
        if key in (self.cfg.get("social_hidden") or []):
            return ""
        return (self.cfg.get("social", {}).get(key) or "").strip()

    def nav_html(self, root):
        out = []
        for n in self.cfg.get("nav", []):
            if n.get("social"):
                url = self.social_url(n["social"])
                if not url:
                    continue
                out.append('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
                           % (common.esc(url), common.esc(n["label"])))
            else:
                out.append('<a href="%s%s">%s</a>'
                           % (root, n.get("href", ""), common.esc(n["label"])))
        return "".join(out)

    X_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18.244 2.25h3.308'
              'l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835'
              'L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>')

    def x_link(self, label="X でのつぶやき"):
        url = self.social_url("x")
        if not url:
            return ""
        return ('<a class="x-link" href="%s" target="_blank" rel="noopener noreferrer">'
                '%s<span>%s</span></a>' % (common.esc(url), self.X_ICON, common.esc(label)))

    def avatar_html(self, root):
        """プロフィール画像。用意されていなければ頭文字の仮アイコンを出す。"""
        prof = self.cfg.get("profile", {})
        rel = (prof.get("image") or "").lstrip("/")
        name = prof.get("name") or self.cfg["site_title"]
        if rel and os.path.isfile(os.path.join(common.DOCS, rel.replace("/", os.sep))):
            return ('<img class="avatar" src="%s%s" width="76" height="76" alt="%s のプロフィール画像">'
                    % (root, common.quote_path(rel), common.esc(name)))
        return ('<span class="avatar avatar-blank" aria-hidden="true">%s</span>'
                % common.esc(name[:1]))

    def profile_block(self, root):
        """トップページのプロフィール紹介欄。"""
        prof = self.cfg.get("profile", {})
        name = prof.get("name") or self.cfg["site_title"]
        links = []
        if prof.get("link"):
            links.append('<a href="%s%s">くわしいプロフィール</a>' % (root, prof["link"]))
        x = self.x_link("Xのプロフィール")
        if x:
            links.append(x)
        return ('<section class="home-profile">%s<div class="profile-body">'
                '<p class="profile-name">%s</p><p class="profile-text">%s</p>'
                '<p class="profile-links">%s</p></div></section>'
                % (self.avatar_html(root), common.esc(name),
                   common.esc(prof.get("tagline", "")), "".join(links)))

    def footer_nav_html(self, root):
        """フッターに並べるリンク（タグ一覧・プライバシーポリシーなど）。

        タグの付いた記事が1件も無いときは、タグ一覧へのリンクを出さない
        （ページ自体を作らないので、リンク切れになってしまうため）。
        """
        items = [n for n in self.cfg.get("footer_nav", [])
                 if n.get("href") != "tags/" or self.has_tags]
        return "".join(' ・ <a href="%s%s">%s</a>' % (root, n["href"], common.esc(n["label"]))
                       for n in items)

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
              og_type="website", canonical_url=None, made_by=None):
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
        write(rel_out, html, made_by=made_by or ("ページ「%s」" % page_title))

    def cat_slug(self, name):
        """カテゴリーの URL 名。設定になければ日本語のフォルダ名をそのまま使う。"""
        return common.url_name(name, self.cfg.get("category_slug_map", {}))

    def tag_slug(self, name):
        """タグの URL 名。設定になければ日本語のタグ名をそのまま使う。"""
        return common.url_name(name, self.cfg.get("tag_slug_map", {}))

    def chips(self, names, root, kind, link=True):
        """カテゴリー名・タグ名を小さな見出しとして並べる。

        link=False のときはリンクにしない。一覧のカードは全体がリンクなので、
        その中にリンクを入れると入れ子になり、ブラウザがカードを途中で
        打ち切ってしまう（タイトルが押せなくなる）ため。
        """
        folder, cls = ("categories", "tag") if kind == "category" else ("tags", "tag tag-plain")
        slug_of = self.cat_slug if kind == "category" else self.tag_slug
        out = []
        for name in names:
            slug = slug_of(name)
            if link and slug:
                out.append('<a class="%s" href="%s%s/%s/">%s</a>'
                           % (cls, root, folder, common.quote_path(slug), common.esc(name)))
            else:
                out.append('<span class="%s">%s</span>' % (cls, common.esc(name)))
        return "".join(out)

    def visible_tags(self, post):
        """画面に出すタグ。

        カテゴリーと同じ名前のタグは、同じ言葉が二度並ぶだけなので出さない。
        記事ファイルのほうは書き換えず、表示のときだけ取り除いている。
        あとから足したカテゴリーにも、そのまま同じ決まりが効く。
        """
        cats = set(post.get("categories", []))
        return [t for t in post.get("tags", []) if t not in cats]

    def tag_row(self, post, root):
        """記事ページの下に出すタグの行。出すタグが無ければ何も出さない。"""
        chips = self.tag_links(self.visible_tags(post), root)
        if not chips:
            return ""
        return ('<p class="post-tags"><span class="post-tags-label">タグ</span>%s</p>'
                % chips)

    def cat_links(self, categories, root, link=True):
        return self.chips(categories, root, "category", link)

    def tag_links(self, tags, root, link=True):
        return self.chips(tags, root, "tag", link)

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
            % (root, common.quote_path(post["slug"]),
               post["date"][:10], common.format_date_ja(post["date"]),
               self.cat_links(post.get("categories", []), root, link=False),
               common.esc(post["title"]), common.esc(self.excerpt(post)))
        )

    def month_groups(self):
        """投稿月ごとに記事をまとめる（新しい順）。"""
        groups = []
        for post in self.posts:
            key = post["date"][:7]
            if not groups or groups[-1][0] != key:
                groups.append((key, []))
            groups[-1][1].append(post)
        return groups

    def category_counts(self):
        buckets = {}
        for post in self.posts:
            for name in post.get("categories", []):
                buckets.setdefault(name, []).append(post)
        return sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    def aside_html(self, root):
        """トップページの横（スマホでは下）に並べる、カテゴリーと月別の案内。"""
        cats = "".join(
            '<li><a href="%scategories/%s/"><span>%s</span><span class="n">%d</span></a></li>'
            % (root, common.quote_path(self.cat_slug(name)), common.esc(name), len(items))
            for name, items in self.category_counts() if self.cat_slug(name))
        box_cats = ('<section class="side-box"><h2 class="side-title">カテゴリー</h2>'
                    '<ul class="side-list">%s</ul>'
                    '<p class="side-more"><a href="%scategories/">カテゴリー一覧へ</a></p>'
                    '</section>' % (cats, root))

        limit = int(self.cfg.get("home", {}).get("show_months", 12))
        groups = self.month_groups()
        months = "".join(
            '<li><a href="%sarchive/#%s"><span>%d年%d月</span>'
            '<span class="n">%d</span></a></li>'
            % (root, key, int(key[:4]), int(key[5:7]), len(items))
            for key, items in groups[:limit])
        more = ('<p class="side-more"><a href="%sarchive/">すべての月を見る</a></p>' % root)             if len(groups) > limit else             ('<p class="side-more"><a href="%sarchive/">全記事一覧へ</a></p>' % root)
        box_months = ('<section class="side-box"><h2 class="side-title">月別アーカイブ</h2>'
                      '<ul class="side-list">%s</ul>%s</section>' % (months, more))
        return box_cats + box_months

    # ---- 各ページ ----

    def build_index(self):
        per = int(self.cfg.get("posts_per_page", 20))
        total = len(self.posts)
        pages = max(1, (total + per - 1) // per)
        list_tpl = tpl("list.html")
        home_tpl = tpl("home.html")

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

            inner = render(
                list_tpl,
                list_title="新着記事" if i == 0 else "新着記事（%d ページ目）" % (i + 1),
                list_note="",
                items="\n".join(self.card(p, root) for p in chunk),
                pagination='<nav class="pagination">%s</nav>' % "".join(links) if pages > 1 else "",
            )
            body = render(
                home_tpl,
                profile=self.profile_block(root) if i == 0 else "",
                list=inner,
                aside=self.aside_html(root),
            )
            title = self.cfg["site_title"] if i == 0 else \
                "%s - %d ページ目" % (self.cfg["site_title"], i + 1)
            self.shell(rel_out, depth, title, self.cfg["site_description"], body,
                       canonical_url=rel_url)
        print("  トップと一覧: %d ページ" % pages)

    def build_posts(self):
        post_tpl = tpl("post.html")
        n = len(self.posts)
        for i, post in enumerate(self.posts):
            # カテゴリー別のサブフォルダに入る記事は 1 階層深くなる
            rel_url = "posts/%s/" % post["slug"]
            depth = rel_url.count("/")
            root = self.root_for(depth)
            newer = self.posts[i - 1] if i > 0 else None   # 一覧は新しい順
            older = self.posts[i + 1] if i + 1 < n else None
            prev_link = next_link = ""
            if older:
                prev_link = ('<a class="prev" href="%sposts/%s/">'
                             '<span class="dir">前の記事</span>'
                             '<span class="label">%s</span></a>'
                             % (root, common.quote_path(older["slug"]),
                                common.esc(older["title"])))
            if newer:
                next_link = ('<a class="next" href="%sposts/%s/">'
                             '<span class="dir">次の記事</span>'
                             '<span class="label">%s</span></a>'
                             % (root, common.quote_path(newer["slug"]),
                                common.esc(newer["title"])))
            body = render(
                post_tpl,
                title=common.esc(post["title"]),
                date_iso=post["date"][:10],
                date_label=common.format_date_ja(post["date"]),
                category_links=self.cat_links(post.get("categories", []), root),
                tag_links=self.tag_row(post, root),
                body=post["_body"],
                prev_link=prev_link,
                next_link=next_link,
            )
            self.shell(rel_url + "index.html", depth,
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
                % (int(p["date"][5:7]), int(p["date"][8:10]), root,
                   common.quote_path(p["slug"]), common.esc(p["title"]))
                for p in items)
            chunks.append('<section class="month-group" id="%s">'
                          '<h2 class="month-head">%d年%d月（%d件）</h2>'
                          '<ul class="compact">%s</ul></section>'
                          % (key, int(year), int(month), len(items), rows))
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
                print("  [注意] カテゴリー「%s」はURLに使える名前になりません。" % name)
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

    def build_category_index(self):
        """カテゴリーの総合ページ。ナビの「カテゴリー」から開く。"""
        root = self.root_for(1)
        rows = "".join(
            '<li><a href="%scategories/%s/"><span>%s</span><span class="n">%d 記事</span></a></li>'
            % (root, common.quote_path(self.cat_slug(name)), common.esc(name), len(items))
            for name, items in self.category_counts() if self.cat_slug(name))
        body = ('<div class="list-head"><h1 class="list-title">カテゴリー一覧</h1>'
                '<p class="list-note">%d 種類のカテゴリーがあります。</p></div>'
                '<section class="side-box"><ul class="side-list">%s</ul></section>'
                % (len(self.category_counts()), rows))
        self.shell("categories/index.html", 1,
                   "カテゴリー一覧 - %s" % self.cfg["site_title"],
                   "カテゴリーの一覧。", body, canonical_url="categories/")
        print("  カテゴリー一覧: 1 ページ")

    def build_tags(self):
        """タグごとの一覧と、タグの総合ページを作る。タグが1つも無ければ作らない。"""
        buckets = {}
        for post in self.posts:
            for name in self.visible_tags(post):
                buckets.setdefault(name, []).append(post)
        if not buckets:
            print("  タグ: 0 ページ（タグの付いた記事がありません）")
            return

        root = self.root_for(2)
        count = 0
        for name, items in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            slug = self.tag_slug(name)
            if not slug:
                print("  [注意] タグ「%s」はURLに使える名前になりません。" % name)
                continue
            body = render(
                tpl("list.html"),
                list_title="タグ：%s" % common.esc(name),
                list_note='<p class="list-note">%d 記事　<a href="%s">タグ一覧へ</a></p>'
                          % (len(items), root + "tags/"),
                items="\n".join(self.card(p, root) for p in items),
                pagination="",
            )
            rel_url = "tags/%s/" % slug
            self.shell(rel_url + "index.html", 2,
                       "%s - %s" % (name, self.cfg["site_title"]),
                       "タグ「%s」の記事 %d 件。" % (name, len(items)), body,
                       canonical_url=rel_url)
            count += 1

        # タグの総合ページ
        inner = self.root_for(1)
        chips = "".join(
            '<a class="tag tag-plain" href="%s%s/"><span>%s</span>'
            '<span class="tag-count">%d</span></a>'
            % (inner + "tags/", common.quote_path(self.tag_slug(name)),
               common.esc(name), len(items))
            for name, items in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            if self.tag_slug(name))
        body = ('<div class="list-head"><h1 class="list-title">タグ一覧</h1>'
                '<p class="list-note">%d 種類のタグがあります。</p></div>'
                '<div class="tag-cloud">%s</div>' % (len(buckets), chips))
        self.shell("tags/index.html", 1, "タグ一覧 - %s" % self.cfg["site_title"],
                   "記事に付けたタグの一覧。", body, canonical_url="tags/")
        print("  タグ: %d ページ（総合ページを含めて %d 枚）" % (count, count + 1))

    def build_pages(self):
        page_tpl = tpl("page.html")
        profile_slug = (self.cfg.get("profile", {}).get("link") or "").strip("/")
        for page in self.pages:
            inner = page["_body"]
            if page["slug"] == profile_slug:
                root = self.root_for(1)
                x = self.x_link("X を見る")
                inner = ('<div class="page-profile-head">%s<div class="profile-body">'
                         '<p class="profile-name">%s</p>'
                         '<p class="profile-links">%s</p></div></div>%s'
                         % (self.avatar_html(root),
                            common.esc(self.cfg.get("profile", {}).get("name") or ""),
                            x, inner))
            body = render(page_tpl, title=common.esc(page["title"]), body=inner)
            rel_url = "%s/" % page["slug"]
            self.shell(rel_url + "index.html", 1,
                       "%s - %s" % (page["title"], self.cfg["site_title"]),
                       common.strip_tags(page["_body"])[:100], body,
                       canonical_url=rel_url)
        print("  固定ページ: %d 件" % len(self.pages))

    def build_redirects(self):
        """旧サイトのURLと同じ場所に、新しい記事への案内ページを置く。

        対応表は common.legacy_pairs() が作る。verify.py と baseline.py も
        同じものを使うので、作る枚数と点検する枚数が食い違わない。
        """
        redirect_tpl = tpl("redirect.html")
        root = self.root_for(1)
        pairs = common.legacy_pairs(self.posts, self.pages)
        # 公開フォルダの直下で、サイト自身が使っている名前。
        # 旧URLがここに当たると、作ったページを転送ページが覆いかぶしてしまう。
        reserved = rules.reserved_top_level(p.get("slug") for p in self.pages)

        made = skipped = 0
        for legacy, target, title in pairs:
            ok, why = rules.safe_url_name(legacy, reserved=reserved)
            if not ok:
                print("  [スキップ] 旧URL「%s」は転送ページのフォルダ名に使えません: %s"
                      % (legacy, why))
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
            write("%s/index.html" % legacy, html,
                  made_by="旧URL「%s」からの転送ページ" % legacy)
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
body{margin:0;background:#f3f5f6;color:#333b40;font-family:"Hiragino Kaku Gothic ProN","Yu Gothic UI","Noto Sans JP",system-ui,sans-serif;line-height:1.95}
main{max-width:40rem;margin:0 auto;padding:72px 16px}
.notice{padding:28px 24px;border:1px solid #e2e8ea;border-radius:10px;background:#fff;box-shadow:0 1px 3px rgba(45,60,66,.07)}
h1{margin:0 0 .7em;font-size:1.1875rem;line-height:1.6}
p{margin:0 0 1em}
a{color:#27756c;text-underline-offset:.18em}
a.button{display:inline-block;padding:10px 20px;border-radius:10px;background:#27756c;color:#fff;font-weight:700;text-decoration:none}
a.button:hover{background:#1e5e57}
.sub{margin:0;color:#5f6c73;font-size:.875rem}
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
        write("404.html", html, made_by="404 ページ")
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
            url = "%s/posts/%s/" % (self.site_url, common.quote_path(post["slug"]))
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
                 common.esc(self.cfg["site_description"]), "".join(items)),
              made_by="feed.xml")

        cats, tags = set(), set()
        for post in self.posts:
            cats.update(post.get("categories", []))
            tags.update(self.visible_tags(post))
        urls = ["", "archive/"]
        urls += ["posts/%s/" % p["slug"] for p in self.posts]
        urls += ["%s/" % p["slug"] for p in self.pages]
        urls += ["categories/%s/" % self.cat_slug(c) for c in sorted(cats) if self.cat_slug(c)]
        if tags:
            urls += ["tags/"]
            urls += ["tags/%s/" % self.tag_slug(t) for t in sorted(tags) if self.tag_slug(t)]
        write("sitemap.xml",
              '<?xml version="1.0" encoding="UTF-8"?>\n'
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>\n'
              % "".join("<url><loc>%s/%s</loc></url>"
                        % (self.site_url, common.quote_path(u)) for u in urls),
              made_by="sitemap.xml")
        print("  feed.xml と sitemap.xml: 作成しました（%s）" % self.site_url)

    def validate(self):
        """組み立てる前に、記事の情報が使える形かどうかを確かめる。

        途中で落ちると docs/ が作りかけのまま公開されてしまうため、
        1ファイルも書き出さないうちに、まとめて調べて日本語で知らせる。
        """
        problems = []
        for post in self.posts:
            where = os.path.relpath(post.get("_path", "?"), common.ROOT)
            slug = post.get("slug")
            if not slug:
                problems.append("%s に URL（slug）が書かれていません" % where)
            else:
                ok, why = rules.safe_slug(slug, reserved=())
                if not ok:
                    problems.append("%s の URL が使えません: %s" % (where, why))
            date = post.get("date") or ""
            if not rules.DATE_VALUE.match(date):
                problems.append("%s の投稿日の形が違います: %r（例 2026-11-02 21:00:00）"
                                % (where, date))
            if not post.get("title"):
                problems.append("%s に題（title）が書かれていません" % where)
        reserved = rules.reserved_top_level(p.get("slug") for p in self.pages)
        for page in self.pages:
            where = os.path.relpath(page.get("_path", "?"), common.ROOT)
            slug = page.get("slug")
            if not slug:
                problems.append("%s に URL（slug）が書かれていません" % where)
                continue
            others = {p.get("slug") for p in self.pages
                      if p is not page and p.get("slug")}
            ok, why = rules.safe_slug(slug, reserved=rules.RESERVED_STRUCTURE | others)
            if not ok:
                problems.append("%s の URL が使えません: %s" % (where, why))
        for name in {c for p in self.posts for c in p.get("categories", [])}:
            slug = self.cat_slug(name)
            if not slug:
                problems.append("カテゴリー「%s」から URL に使える名前が作れません" % name)
                continue
            ok, why = rules.safe_url_name(slug, reserved=reserved)
            if not ok:
                problems.append("カテゴリー「%s」の URL が使えません: %s" % (name, why))
        if problems:
            print("\n*** 中断します：記事の情報に問題があります ***")
            for line in problems[:20]:
                print("    %s" % line)
            if len(problems) > 20:
                print("    ... ほか %d 件" % (len(problems) - 20))
            print("\n直してから、もう一度実行してください。"
                  "（公開フォルダには何も書き出していません）")
            raise SystemExit(1)

    def build(self):
        self.validate()
        os.makedirs(common.DOCS, exist_ok=True)
        write(".nojekyll", "", made_by=".nojekyll")
        print("サイトを組み立てます。")
        self.build_index()
        self.build_posts()
        self.build_archive()
        self.build_categories()
        self.build_category_index()
        self.build_tags()
        self.build_pages()
        self.build_redirects()
        self.build_404()
        self.build_feed_and_sitemap()


def sweep_old_files():
    """前回のビルドで作ったが今回は作らなかったファイルだけを消す。

    控えに書かれた場所が公開フォルダの外を指していたら消さない。
    一度おかしな場所が記録されると、関係ないファイルを消してしまうため。
    """
    if not os.path.exists(MANIFEST):
        return 0
    with open(MANIFEST, encoding="utf-8") as f:
        previous = set(json.load(f).get("files", []))
    docs_real = os.path.realpath(common.DOCS)
    removed = 0
    for rel in sorted(previous - set(written_files), reverse=True):
        abs_path = os.path.realpath(os.path.join(common.DOCS, rel.replace("/", os.sep)))
        if not abs_path.startswith(docs_real + os.sep):
            print("  [注意] 公開フォルダの外を指す記録は無視しました: %s" % rel)
            continue
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
