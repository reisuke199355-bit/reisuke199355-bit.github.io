# -*- coding: utf-8 -*-
"""どの入口から来た日記でも、同じ決まりで記事ファイルにする。

役目:
    「題・本文・日付・カテゴリー・タグ」の5つが決まったあと、
    記事ファイルを1件書き出すところまでを、入力元を問わず同じ処理で行う。

    ここに置いてあるのは「1件ぶんの段取り」だけ。
    何を受け入れるかの決まりは tools/rules.py に、
    記事ファイルの読み書きは tools/common.py にある。

この部品を使う入口:
    tools/import_txt.py        Windows の 10_記事管理 の TXT（これまでの方法）
    tools/import_inbox.py      inbox/ に置かれた TXT（スマホ・クラウドから）
    tools/post_from_issue.py   GitHub の投稿フォーム（Issue）から

なぜ1か所にまとめるか:
    入口が増えても「二重投稿を止める」「登録されていないカテゴリーを断る」
    「公開済みの記事を上書きしない」の判定が入口ごとにずれないようにするため。
    入口を足すときに、ガードを書き直す必要がない。

画面に文字を出さない（print しない）。
何を出すかは入口ごとに違うので、理由は戻り値（Plan.code / Plan.reason）で返す。
"""
import collections
import datetime
import hashlib
import html
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baseline
import common
import rules

# 記事の「タグ:」の行。全角コロンでも読む。
TAG_LINE = re.compile(r"^\s*(?:タグ|tags?)\s*[:：]\s*(.*)$", re.IGNORECASE)
# 「2026年10月30日」「2026-10-30」だけの行は、本文ではなくタイトルとみなす
DATE_ONLY = re.compile(r"^\s*\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?\s*$")
BOLD = re.compile(r"\*\*(.+?)\*\*")
TAG_SEP = re.compile(r"[,，、/／]+")

# 記事の投稿時刻。日付だけが分かっているときに使う（既存109記事と同じ 21:00）。
POST_TIME = datetime.time(21, 0)

# 1件の本文の上限。TXT の上限（rules.MAX_TXT_BYTES）と同じ考え方で、
# フォームから送られた文章にも同じ上限を当てる。
MAX_BODY_BYTES = rules.MAX_TXT_BYTES


# TXT を読むときに試す文字コード。メモ帳の「ANSI」保存（cp932）も読む。
ENCODINGS = ("utf-8-sig", "utf-8", "cp932")


def read_text(path):
    """文字コードを順に試して読む。あわせて中身のハッシュも返す。

    どの文字コードでも読めなかった場合は replaced=True を返す。
    置換文字（文字化け）のまま記事にしてしまわないよう、呼び出し側で弾く。
    """
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc, digest, False
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(一部置換)", digest, True


def split_header(text):
    """本文より前の「# タイトル」「タグ:」を取り出して、残りを本文として返す。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title, tags, i = None, [], 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            if title is None and not tags:
                break          # 先頭の空行は本文の一部として扱う
            i += 1
            break              # 見出しの終わり
        if title is None and line.startswith("#"):
            title = line.lstrip("#").strip()
            i += 1
            continue
        if title is None and DATE_ONLY.match(line):
            # 1行目が日付だけ。WordPress 時代の日記と同じ形なのでタイトルにする
            title = line
            i += 1
            continue
        m = TAG_LINE.match(line)
        if m:
            tags = [t.strip() for t in TAG_SEP.split(m.group(1)) if t.strip()]
            i += 1
            continue
        break                  # 見出しではない＝ここから本文
    return title, tags, "\n".join(lines[i:])


def to_html(text):
    """プレーンテキストを段落と改行の HTML にする。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", text) if b.strip()]
    out = []
    for block in blocks:
        lines = [BOLD.sub(r"<strong>\1</strong>", html.escape(line.strip()))
                 for line in block.split("\n") if line.strip()]
        if lines:
            out.append("<p>%s</p>" % "<br>".join(lines))
    return "\n\n".join(out) + "\n"


def body_key(text):
    """本文の中身で重複を見分けるための指紋。

    改行の書き方・BOM・空白のちがいは同じものとして扱う。
    以前はバイト単位の一致しか見ていなかったため、末尾に改行を1つ足しただけで
    同じ日記が二重に増えてしまっていた。

    空白はすべて取り除く（1つにまとめるのではない）。これは、もとの TXT では
    改行だった場所が、記事では <br> になるため。空白1つにまとめる数えかたでは、
    同じ日記なのに TXT 側と記事側で指紋が変わってしまい、重複を見逃していた。
    check.py の本文照合と同じ数えかたに揃えている。

    さらに、書いたままの TXT と、記事になったあとの HTML をそろえる。
        **太字** は、記事では <strong>太字</strong> になる → ** を取り除く
        & < > " は、記事では &amp; &lt; &gt; &quot; になる → もとの文字に戻す
    この2つをそろえないと、太字や「&」を使った日記を、名前だけ変えて
    置き直したときに「同じ本文だ」と気づけず、二重に増えてしまう。
    実際の日記は太字をよく使うので、ここは効いてくる。
    """
    normalized = html.unescape((text or "").replace("﻿", ""))
    normalized = normalized.replace("**", "")
    normalized = re.sub(r"\s+", "", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def text_digest(text):
    """送られてきた文章そのものの指紋（source_sha256 に入れる値）。

    TXT の取り込みではファイルのバイト列から作る。フォームから届いた
    文章にはファイルが無いので、UTF-8 に直したうえで同じ作り方をする。
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def unique_slug(base, taken):
    slug = base
    n = 2
    while slug in taken:
        slug = "%s-%d" % (base, n)
        n += 1
    return slug


def normalize_tags(value):
    """タグの指定を一覧にする。文字列でも一覧でも受け取れる。

    「,」「、」「/」などで区切る。前後の空白は落とし、同じものは1つにする。
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = TAG_SEP.split(value)
    else:
        parts = []
        for item in value:
            parts.extend(TAG_SEP.split(str(item)))
    out = []
    for part in parts:
        tag = part.strip().strip("#＃").strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def local_today(cfg=None):
    """設定の timezone（既定 +09:00）で「今日」を求める。

    GitHub Actions の時計は UTC なので、そのまま date.today() を使うと
    日本の深夜に書いた日記が前の日のものになってしまう。
    """
    cfg = cfg or {}
    raw = str(cfg.get("timezone") or "+09:00").strip()
    m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", raw)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        offset = datetime.timedelta(hours=int(m.group(2)),
                                    minutes=int(m.group(3) or 0)) * sign
    else:
        offset = datetime.timedelta(hours=9)
    return (datetime.datetime.now(datetime.timezone.utc)
            + offset).date()


DATE_INPUT = re.compile(r"^\s*(\d{4})\s*[-/._年]\s*(\d{1,2})\s*[-/._月]\s*(\d{1,2})\s*日?\s*$")


def parse_date(value):
    """入力された日付を読む。戻り値は (日付, 読めなかった理由)。

    どちらか片方だけが入る。空の入力は (None, None) を返す（＝未指定）。
    """
    text = (value or "").strip()
    if not text:
        return None, None
    m = DATE_INPUT.match(text)
    if not m:
        day = rules.date_from_name(text)
        if day:
            return day, None
        return None, ("日付を読み取れません: %r（例 2026-11-02 / 2026年11月2日）" % text)
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), None
    except ValueError:
        return None, ("ありえない日付です: %r" % text)


class Plan:
    """1件ぶんの段取り。書き出してよいか、だめならなぜかを持つ。

    code の意味:
        new              新しい記事として書き出してよい
        reimport         既にある記事に入れ直してよい（--force のとき）
        already          同じ中身のものが取り込み済み（黙って見送る）
        empty-body       本文がない
        same-body        すでにある記事と本文が同じ
        same-source      同じ名前で取り込み済み（中身は違う）
        same-ref         同じ投稿（Issue など）から取り込み済み
        too-long         本文が大きすぎる
        unknown-category 登録されていないカテゴリー
        bad-tag          タグが URL に使えない
        date-out-of-range 日付がブログの範囲の外
        bad-category-url カテゴリー名が URL に使えない
        bad-slug         記事の URL を作れない
        outside          置き場所がフォルダの外になる
        frozen           公開済みとして記録されている記事
    """

    WRITABLE = ("new", "reimport")

    def __init__(self, code, reason="", **kw):
        self.code = code
        self.reason = reason
        self.existing = kw.get("existing")
        self.slug = kw.get("slug")
        self.out_path = kw.get("out_path")
        self.meta = kw.get("meta")
        self.body = kw.get("body")
        self.chars = kw.get("chars", 0)
        self.added_suffix = kw.get("added_suffix")
        self.body_id = kw.get("body_id")
        self.digest = kw.get("digest")
        self.title = kw.get("title")
        self.category = kw.get("category")
        self.tags = kw.get("tags") or []
        self.day = kw.get("day")
        self.source_name = kw.get("source_name")
        self.source_ref = kw.get("source_ref")

    @property
    def ok(self):
        return self.code in self.WRITABLE

    @property
    def is_reimport(self):
        return self.code == "reimport"


class Catalog:
    """いまある記事の控え。二重投稿と上書きを見分けるために使う。

    1回の取り込みで何件も扱えるよう、書き出した記事を remember() で
    覚えていく（同じ実行の中での重なりも見つけられる）。
    """

    def __init__(self, cfg=None, posts=None, pages=None):
        self.cfg = cfg if cfg is not None else common.load_config()
        self.posts = common.load_posts() if posts is None else posts
        self.pages = common.load_pages() if pages is None else pages
        self.default_category = self.cfg.get("default_category") or "日記"
        self.category_map = self.cfg.get("category_slug_map") or {}
        self.tag_map = self.cfg.get("tag_slug_map") or {}

        self.taken = {p["slug"] for p in self.posts if p.get("slug")}
        self.by_hash = {p["source_sha256"]: p
                        for p in self.posts if p.get("source_sha256")}
        # 本文の中身そのもので重複を見る。既存の記事すべてを相手にする。
        self.by_body = {}
        for post in self.posts:
            self.by_body.setdefault(
                body_key(common.strip_tags(post.get("_body", ""))), post)
        # 同じファイル名を同じカテゴリーに置き直した場合も、同じ記事として扱う
        self.by_source = {(tuple(p.get("categories") or [""])[0], p["source_file"]): p
                          for p in self.posts if p.get("source_file")}
        # クラウドからの投稿は「どの投稿から来たか」で見分ける（Issue 番号など）。
        # 日付や題を直して送り直されても、同じ投稿なら二重にしない。
        self.by_ref = {p["source_ref"]: p for p in self.posts if p.get("source_ref")}

        self.frozen = set((baseline.load_baseline() or {}).get("posts", {}))
        self.unlocked = baseline.load_unlocked()
        self.reserved_top = rules.reserved_top_level(
            p.get("slug") for p in self.pages)

    def allowed_categories(self):
        """いま受け入れるカテゴリー。config.json を見るので、
        取り込みの途中でカテゴリーが登録されても追いつく。"""
        return rules.allowed_categories(self.cfg)

    def plan(self, title=None, body_text="", day=None, category=None, tags=(),
             source="txt", source_name=None, source_ref=None, digest=None,
             force=False, force_import=False, today=None, check_category=True,
             resolve_day=None):
        """1件ぶんの段取りを組む。書き出しはしない。

        day は日付（datetime.date）。どこから日付を読むかは入口ごとに違うので、
        ここへ来るまでに決めておく。

        resolve_day を渡すと、二重投稿の見分けが済んだあとで日付を決める。
        「取り込み済みのものは、日付が読めなくても黙って見送る」という
        これまでの出方を保つために、この順番が要る。
        戻り値は (日付, 読めなかった理由) の2つ組。
        """
        category = category or self.default_category
        tags = list(tags or [])

        seen = self.by_hash.get(digest) if digest else None
        if seen and not force:
            return Plan("already", existing=seen)

        if not (body_text or "").strip():
            return Plan("empty-body", "見出しやタグの行しかなく、本文がありません。")

        if len((body_text or "").encode("utf-8")) > MAX_BODY_BYTES:
            return Plan("too-long", "本文が大きすぎます（%.1f MB）。日記ではないようです。"
                        % (len(body_text.encode("utf-8")) / 1024 / 1024))

        body_id = body_key(body_text)
        same_body = self.by_body.get(body_id)
        if same_body and not force:
            return Plan("same-body",
                        "すでにある記事と本文が同じです（/posts/%s/）。"
                        % same_body.get("slug", "?"), existing=same_body)

        if not seen and source_ref:
            same_ref = self.by_ref.get(source_ref)
            if same_ref and not force:
                return Plan("same-ref",
                            "同じ投稿から取り込み済みです（/posts/%s/）。"
                            % same_ref.get("slug", "?"), existing=same_ref)
            if same_ref and force:
                seen = same_ref

        if not seen and source_name:
            same_name = self.by_source.get((category, source_name))
            if same_name and not force:
                return Plan("same-source",
                            "同じ名前で取り込み済みです（/posts/%s/ ／ 中身が違います）。"
                            % same_name["slug"], existing=same_name)
            if same_name and force:
                seen = same_name

        if day is None and resolve_day is not None:
            day, why = resolve_day()
            if day is None:
                return Plan("no-date", why or "日付が決まっていません。")
        if day is None:
            return Plan("no-date", "日付が決まっていません。")

        out_of_range = rules.date_in_range(day, today)
        if out_of_range and not force_import:
            return Plan("date-out-of-range",
                        "%s（%s）" % (out_of_range, day.isoformat()))

        if check_category:
            allowed = self.allowed_categories()
            if category not in allowed:
                return Plan("unknown-category",
                            "「%s」は登録されていないカテゴリーです。使えるのは %s です。"
                            % (category, " / ".join(allowed)))

        for tag in tags:
            url = common.url_name(tag, self.tag_map)
            ok, why = rules.safe_url_name(url, reserved=rules.RESERVED_STRUCTURE)
            if not url:
                return Plan("bad-tag", "タグ「%s」は URL に使えません: 空の名前になります"
                            % tag)
            if not ok:
                return Plan("bad-tag", "タグ「%s」は URL に使えません: %s" % (tag, why))

        if not title:
            title = "%d年%d月%d日" % (day.year, day.month, day.day)

        added_suffix = None
        if seen and force:
            slug = seen["slug"]
            out_path = seen["_path"]
        else:
            base = day.isoformat()
            if category != self.default_category:
                cat_url = common.url_name(category, self.category_map)
                ok, why = rules.safe_url_name(cat_url, reserved=self.reserved_top)
                if not ok:
                    return Plan("bad-category-url",
                                "カテゴリー「%s」は URL に使えません: %s" % (category, why))
                base = "%s/%s" % (cat_url, base)
            slug = unique_slug(base, self.taken)
            if slug != base:
                added_suffix = base
            ok, why = rules.safe_slug(slug, reserved=())
            if not ok:
                return Plan("bad-slug", "記事の URL を作れません: %s" % why)
            out_path = os.path.join(common.CONTENT_POSTS, slug + ".html")

        # 書き込む先が content/posts の中に収まっているか、必ず確かめる。
        if not rules.under(common.CONTENT_POSTS, os.path.dirname(out_path)):
            return Plan("outside",
                        "記事の置き場所がフォルダの外になります: %s" % out_path)

        # 公開済みとして記録されている記事は、どんな経路でも上書きしない。
        # 修正したいときは、人が baseline.py --修正許可 を実行する必要がある。
        if slug in self.frozen and slug not in self.unlocked:
            return Plan("frozen",
                        "/posts/%s/ は公開済みとして記録されている記事です。" % slug,
                        slug=slug)

        meta = collections.OrderedDict()
        meta["title"] = title
        meta["date"] = datetime.datetime.combine(day, POST_TIME) \
            .strftime("%Y-%m-%d %H:%M:%S")
        meta["slug"] = slug
        meta["diary_date"] = day.isoformat()
        meta["categories"] = [category]
        meta["tags"] = tags
        meta["source"] = source
        if source_name:
            meta["source_file"] = source_name
        if digest:
            meta["source_sha256"] = digest
        if source_ref:
            meta["source_ref"] = source_ref

        body = to_html(body_text)
        return Plan("reimport" if seen else "new",
                    existing=seen, slug=slug, out_path=out_path, meta=meta,
                    body=body, chars=len(common.strip_tags(body)),
                    added_suffix=added_suffix, body_id=body_id, digest=digest,
                    title=title, category=category, tags=tags, day=day,
                    source_name=source_name, source_ref=source_ref)

    def remember(self, plan):
        """書き出した（または書き出す予定の）記事を控える。

        確認のみ（--dry-run）のときも呼ぶ。同じ実行の中での重なりを
        正しく見せるため。
        """
        if not plan.ok:
            return
        stub = {"slug": plan.slug, "_path": plan.out_path}
        self.taken.add(plan.slug)
        if plan.digest:
            self.by_hash[plan.digest] = stub
        if plan.source_name:
            self.by_source[(plan.category, plan.source_name)] = stub
        if plan.source_ref:
            self.by_ref[plan.source_ref] = stub
        if plan.body_id:
            self.by_body[plan.body_id] = stub

    def write(self, plan):
        """記事ファイルを書き出す。上書きになるときは控え（.bak）を作る。

        戻り値は控えのパス（作らなかったときは None）。
        """
        if not plan.ok:
            raise ValueError("書き出せない段取りです: %s" % plan.code)
        backup = None
        if os.path.exists(plan.out_path):
            backup = plan.out_path + ".bak"
            shutil.copy2(plan.out_path, backup)
        common.write_entry(plan.out_path, plan.meta, plan.body)
        return backup
