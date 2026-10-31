# -*- coding: utf-8 -*-
"""WordPress の書き出し XML から記事と画像を取り込む（1回だけ実行する移行用ツール）。

使い方:
    python wp_import.py <WordPress.xml> <wp-content.zip>

原本は読み取りのみで扱い、content/posts と docs/images に出力する。
"""
import collections
import io
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

NS = {
    "wp": "http://wordpress.org/export/1.2/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
}
IMAGE_DIR_IN_DOCS = "images"


def text_of(el, path):
    found = el.find(path, NS)
    return (found.text or "") if found is not None else ""


def find_gallery_span(src, start):
    """ギャラリー外側の <figure> に対応する </figure> の位置を返す。"""
    depth = 0
    for m in re.finditer(r"<(/?)figure\b", src[start:]):
        if m.group(1):
            depth -= 1
            if depth == 0:
                return start + m.start(), start + m.end() + src[start + m.end():].index(">") + 1
        else:
            depth += 1
    return None, None


def clean_body(raw, image_map, rel_prefix):
    """Gutenberg ブロックの注釈と WordPress 固有のクラスを外し、画像を相対パスへ差し替える。"""
    s = raw

    # ギャラリーの外側 <figure> を <div class="gallery"> に置き換える
    while True:
        m = re.search(r'<figure class="wp-block-gallery[^"]*">', s)
        if not m:
            break
        close_start, close_end = find_gallery_span(s, m.start())
        if close_start is None:
            s = s[:m.start()] + '<div class="gallery">' + s[m.end():]
            break
        s = (s[:m.start()] + '<div class="gallery">' + s[m.end():close_start]
             + "</div>" + s[close_end:])

    # ブロック注釈を削除
    s = re.sub(r"<!--\s*/?wp:[^>]*?-->", "", s)

    # WordPress 固有のクラス属性を削除
    s = re.sub(r'\s+class="(?:wp-block-heading|wp-block-image[^"]*|wp-image-\d+'
               r'|is-style-[^"]*|has-[^"]*)"', "", s)

    # 画像の src をローカルの相対パスへ
    def fix_img(m):
        url = m.group(2)
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        local = image_map.get(path.lstrip("/"))
        if local:
            return "%s%s%s%s" % (m.group(1), rel_prefix, local, m.group(3))
        return m.group(0)

    s = re.sub(r'(<img[^>]+src=")([^"]+)(")', fix_img, s)
    s = re.sub(r"(<img\b(?![^>]*\sloading=)[^>]*?)(/?>)", r'\1 loading="lazy"\2', s)

    # 空の段落と余分な空行を整える
    s = re.sub(r"<p>\s*(?:<br\s*/?>)?\s*</p>", "", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip() + "\n"


def main():
    if len(sys.argv) < 3:
        print("使い方: python wp_import.py <WordPress.xml> <wp-content.zip>")
        return 1
    xml_path, zip_path = sys.argv[1], sys.argv[2]
    cfg = common.load_config()
    slug_map = cfg.get("legacy_slug_map", {})

    root = ET.parse(xml_path).getroot()
    channel = root.find("channel")
    items = channel.findall("item")

    posts, pages = [], []
    for it in items:
        post_type = text_of(it, "wp:post_type")
        status = text_of(it, "wp:status")
        if status != "publish" or post_type not in ("post", "page"):
            continue
        rec = {
            "id": text_of(it, "wp:post_id"),
            "title": text_of(it, "title"),
            "date": text_of(it, "wp:post_date"),
            "legacy_url": text_of(it, "link"),
            "legacy_slug": text_of(it, "wp:post_name"),
            "content": text_of(it, "content:encoded"),
            "categories": [c.text or "" for c in it.findall("category")
                           if c.get("domain") == "category"],
            "tags": [c.text or "" for c in it.findall("category")
                     if c.get("domain") == "post_tag"],
        }
        (posts if post_type == "post" else pages).append(rec)

    print("公開記事 %d 件 / 公開固定ページ %d 件を XML から読み込みました。"
          % (len(posts), len(pages)))

    # 本文が参照している画像だけを ZIP から取り出す
    referenced = set()
    for rec in posts + pages:
        for url in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', rec["content"], re.I):
            referenced.add(urllib.parse.unquote(urllib.parse.urlparse(url).path).lstrip("/"))

    image_map = {}
    copied = 0
    with zipfile.ZipFile(zip_path) as zf:
        names = {n for n in zf.namelist() if not n.endswith("/")}
        for path in sorted(referenced):
            candidate = path if path in names else None
            if candidate is None:
                base = os.path.basename(path)
                hits = [n for n in names if os.path.basename(n) == base]
                candidate = hits[0] if hits else None
            if candidate is None:
                print("  [欠落] ZIP 内に見つかりません: %s" % path)
                continue
            rel = candidate.split("uploads/", 1)[-1] if "uploads/" in candidate \
                else os.path.basename(candidate)
            out_rel = "%s/%s" % (IMAGE_DIR_IN_DOCS, rel)
            out_abs = os.path.join(common.DOCS, out_rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out_abs), exist_ok=True)
            with zf.open(candidate) as src, open(out_abs, "wb") as dst:
                dst.write(src.read())
            image_map[path] = out_rel
            copied += 1
            print("  [画像] %s -> docs/%s (%.1f KB)"
                  % (os.path.basename(path), out_rel, os.path.getsize(out_abs) / 1024))
    print("画像 %d 件を取り込みました（参照 %d 件）。" % (copied, len(referenced)))

    # 記事を書き出す
    used_slugs = {}
    written = 0
    for rec in sorted(posts, key=lambda r: r["date"]):
        diary_date = common.diary_date_from_title(rec["title"])
        slug = slug_map.get(rec["id"]) or diary_date or rec["date"][:10]
        if slug in used_slugs:
            n = 2
            while "%s-%d" % (slug, n) in used_slugs:
                n += 1
            slug = "%s-%d" % (slug, n)
        used_slugs[slug] = rec["id"]

        # 記事ページは docs/posts/<slug>/index.html に出るので画像までは ../../
        body = clean_body(rec["content"], image_map, "../../")
        meta = collections.OrderedDict()
        meta["title"] = rec["title"]
        meta["date"] = rec["date"]
        meta["slug"] = slug
        if diary_date:
            meta["diary_date"] = diary_date
        meta["categories"] = rec["categories"]
        meta["tags"] = rec["tags"]
        meta["legacy_slug"] = rec["legacy_slug"]
        meta["legacy_url"] = rec["legacy_url"]
        meta["wp_id"] = rec["id"]
        meta["source"] = "wordpress"
        common.write_entry(os.path.join(common.CONTENT_POSTS, slug + ".html"), meta, body)
        written += 1

    print("記事 %d 件を content/posts へ書き出しました。" % written)

    # 固定ページ
    for rec in pages:
        slug = {"ホーム": "about", "プロフィール": "profile"}.get(rec["title"].strip())
        if not slug:
            print("  [スキップ] 固定ページ「%s」は記事一覧で代替します。" % rec["title"])
            continue
        body = clean_body(rec["content"], image_map, "../")
        meta = collections.OrderedDict()
        meta["title"] = rec["title"]
        meta["slug"] = slug
        meta["legacy_slug"] = rec["legacy_slug"]
        meta["legacy_url"] = rec["legacy_url"]
        meta["wp_id"] = rec["id"]
        meta["source"] = "wordpress"
        common.write_entry(os.path.join(common.CONTENT_PAGES, slug + ".html"), meta, body)
        print("  [固定ページ] 「%s」 -> content/pages/%s.html" % (rec["title"], slug))
    return 0


if __name__ == "__main__":
    sys.exit(main())
