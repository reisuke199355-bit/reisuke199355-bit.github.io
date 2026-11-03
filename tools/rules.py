# -*- coding: utf-8 -*-
"""取り込み・生成・検査・公開で共通に使う決まりごと。

ここ1か所を直せば、import_txt.py / build.py / check.py / publish.py の
判定がすべて揃う。副作用のない関数と定数だけを置く（標準ライブラリのみ）。

このファイルは「何を記事として受け入れ、何を公開してよいか」の唯一の基準。
"""
import datetime
import os
import re
import unicodedata

# ---- ブログそのものの決まり ----

# 最初の記事の日付。これより前の日付の記事は、まちがいとして扱う。
BLOG_START = datetime.date(2026, 7, 18)

# 1件の TXT の上限。これを超えるものは日記ではないと判断する。
MAX_TXT_BYTES = 512 * 1024

# 本文がこれより短いと「短すぎる」として注意を出す（これだけでは、はねない）。
SHORT_BODY_CHARS = 30

# カテゴリー別サブフォルダは1段まで（posts/カテゴリー/日付）。
MAX_SLUG_DEPTH = 2


# ---- URL とフォルダ名 ----

# サイトの骨組みが使う名前。カテゴリーや旧URLに使わせない。
# 固定ページの名前（about / privacy / profile）はここに入れない。
# 固定ページ自身の URL なので、固定ページを点検するときに弾いてしまうため。
# 「ほかのものが固定ページの場所を奪わないか」は reserved_top_level() で見る。
RESERVED_STRUCTURE = frozenset({
    "posts", "categories", "tags", "archive", "page", "assets", "images",
    "index.html", "404.html", "feed.xml", "sitemap.xml", ".nojekyll",
})

# 以前の名前。外から使われていたときのために残す。
RESERVED_URL_NAMES = RESERVED_STRUCTURE


def reserved_top_level(page_slugs=()):
    """docs/ の直下で、ほかのものに奪われてはいけない名前。

    骨組みの名前に、固定ページの名前（about / privacy / profile）を足したもの。
    旧URLの転送フォルダを作るときや、固定ページ同士の衝突を見るときに使う。
    """
    return RESERVED_STRUCTURE | {str(s).strip() for s in page_slugs if str(s).strip()}

# Windows が特別扱いする名前。このフォルダは作れない。
WINDOWS_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)]
)

# ファイル名・フォルダ名に使えない文字。
FORBIDDEN_CHARS = frozenset('\\/:*?"<>|')

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def safe_url_name(name, reserved=None):
    """URL の1区切りとして使える名前か調べる。

    戻り値は (使えるか, 使えない理由)。理由は日本語。
    バックスラッシュは Linux では無害な文字だが Windows ではフォルダの
    区切りになってしまうため、動かす場所によらず必ず拒否する。

    reserved には「ここでは使わせない名前」を渡す。省略すると
    サイトの骨組みの名前（RESERVED_STRUCTURE）だけを見る。
    """
    reserved = RESERVED_STRUCTURE if reserved is None else reserved
    if name is None:
        return False, "名前がありません"
    if not isinstance(name, str):
        return False, "名前が文字列ではありません"
    if name != name.strip():
        return False, "前後に空白があります"
    if not name:
        return False, "空の名前です"
    if CONTROL_CHARS.search(name):
        return False, "使えない制御文字が入っています"
    if "\\" in name:
        return False, "円記号（バックスラッシュ）は使えません"
    bad = sorted(FORBIDDEN_CHARS & set(name))
    if bad:
        return False, "使えない文字が入っています: %s" % " ".join(bad)
    if name in (".", ".."):
        return False, "「.」「..」は使えません"
    if name.startswith(".") or name.endswith("."):
        return False, "先頭または末尾が「.」です"
    lowered = {str(r).lower() for r in reserved}
    if name.lower() in lowered:
        return False, "「%s」はサイトが使っている名前です" % name
    if name.split(".")[0].lower() in WINDOWS_DEVICE_NAMES:
        return False, "「%s」は Windows が特別扱いする名前です" % name
    return True, ""


def url_name_warnings(name):
    """使えるが気をつけたい点を返す（はねない）。"""
    out = []
    if " " in name:
        out.append("名前に空白が入っています（URL が %%20 になります）")
    if unicodedata.normalize("NFKC", name) != name:
        out.append("全角の英数字や記号が入っています（半角との取り違えに注意）")
    return out


def safe_slug(slug, reserved=None):
    """記事や固定ページの URL（slug）として使える文字列か調べる。

    カテゴリー別の記事は「カテゴリー/日付」の形になるため「/」は1つまで許す。
    戻り値は (使えるか, 使えない理由)。
    reserved は safe_url_name と同じ意味。記事は posts/ の下に入るので
    骨組みの名前とぶつからない。reserved=() を渡して文字の安全さだけを見る。
    """
    if not slug or not isinstance(slug, str):
        return False, "URL（slug）がありません"
    if slug != slug.strip():
        return False, "URL の前後に空白があります"
    if "\\" in slug:
        return False, "URL に円記号（バックスラッシュ）が入っています"
    if slug.startswith("/"):
        return False, "URL が「/」で始まっています（フォルダの外に出てしまいます）"
    if slug.endswith("/"):
        return False, "URL が「/」で終わっています"
    if "//" in slug:
        return False, "URL に空の区切りがあります"
    if os.path.isabs(slug):
        return False, "URL が絶対パスになっています"
    parts = slug.split("/")
    if len(parts) > MAX_SLUG_DEPTH:
        return False, "URL の階層が深すぎます（%d段まで）" % MAX_SLUG_DEPTH
    for part in parts:
        ok, why = safe_url_name(part, reserved)
        if not ok:
            return False, "URL の「%s」の部分が使えません: %s" % (part, why)
    return True, ""


def under(root, path):
    """path が root の中に収まっているか。シンボリックリンクも解いて確かめる。"""
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    return path_real == root_real or path_real.startswith(root_real + os.sep)


# ---- カテゴリー ----

def allowed_categories(cfg):
    """取り込みを許すカテゴリー名の一覧。

    config.json の categories があればそれを正とする。
    無い古い設定でも動くよう、category_slug_map のキーと default_category に戻る。
    """
    listed = cfg.get("categories")
    if isinstance(listed, list) and listed:
        return [str(c).strip() for c in listed if str(c).strip()]
    fallback = list((cfg.get("category_slug_map") or {}).keys())
    default = (cfg.get("default_category") or "").strip()
    if default and default not in fallback:
        fallback.insert(0, default)
    return fallback


# ---- 記事ではないファイルの見分け ----

# ファイル名にこれらが入っていたら、日記ではなく説明や覚え書きとみなす。
NON_ARTICLE_WORDS = (
    "説明書", "使い方", "手順書", "手順", "テンプレート", "ひな形", "雛形",
    "メモ", "覚書", "覚え書き", "下書き", "インストール", "設定方法",
    "readme", "read me", "manual", "howto", "how-to", "template",
    "sample", "example", "テスト", "test",
)

# 編集ソフトが作る一時ファイル・控え。
TEMP_NAME = re.compile(r"(?i)(^~\$|^\.|\.bak$|\.orig$|\.tmp$|\.swp$|\(\d+\)$|- コピー$|- copy$)")

# 本文にこれらが多く出てきたら、説明書の可能性を注意する（はねない）。
DOC_BODY_WORDS = (
    "このファイル", "このフォルダ", "次の手順", "以下の手順", "以下のコマンド",
    "インストール", "ダブルクリック", "テンプレート", "README",
    ".bat", ".txt を", "python ",
)


def non_article_name(filename):
    """記事として扱うべきでないファイル名か。理由を返す（無ければ None）。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    if TEMP_NAME.search(stem) or TEMP_NAME.search(os.path.basename(filename)):
        return "編集ソフトの一時ファイル・控えの名前です"
    low = stem.lower()
    for word in NON_ARTICLE_WORDS:
        if word.lower() in low:
            return "名前に「%s」が入っています（説明や覚え書きの可能性）" % word
    return None


def non_article_words(filename):
    """ファイル名に入っている「記事らしくない語」をすべて返す。

    non_article_name() は、最初に当たった語で理由を1つ返す。
    こちらは当たった語そのものを返すので、呼び出し側で
    「その語は、この置き場所なら当たり前か」を考えられる。
    """
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    low = stem.lower()
    return [w for w in NON_ARTICLE_WORDS if w.lower() in low]


def doc_like_body(body_text):
    """本文が説明書らしいか。当てはまった語の一覧を返す。"""
    return [w for w in DOC_BODY_WORDS if w in (body_text or "")]


# ---- 日付 ----

DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")

# 8桁の数字を日付として使うのは、区切り記号か文字列の端に接しているときだけ。
# これで「v20260101_final」「製品20260715番」のような誤検出を避ける。
EIGHT_DIGITS = re.compile(r"(?:^|(?<=[-_.\s]))(\d{4})(\d{2})(\d{2})(?=$|[-_.\s])")

DATE_PATTERNS = (
    re.compile(r"(\d{4})[-_.](\d{1,2})[-_.](\d{1,2})"),
    re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日"),
)


def date_from_name(name):
    """ファイル名から日付を読む。読めなければ None。

    同じ形が何度も出てくる場合に備えて、見つかった位置を順に試す。
    最初の候補が「2026年13月40日」のような不正な日付でも、あきらめない。
    """
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    for pattern in DATE_PATTERNS:
        for m in pattern.finditer(stem):
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    for m in EIGHT_DIGITS.finditer(stem):
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


def date_in_range(day, today=None):
    """記事の日付として受け入れられるか。理由を返す（問題なければ None）。"""
    today = today or datetime.date.today()
    if day < BLOG_START:
        return "ブログの開始日（%s）より前の日付です" % BLOG_START.isoformat()
    if day > today + datetime.timedelta(days=1):
        return "未来の日付です（今日は %s）" % today.isoformat()
    return None


# ---- フロントマター ----

REQUIRED_POST_KEYS = ("title", "date", "slug", "categories")
REQUIRED_PAGE_KEYS = ("title", "slug")


# ---- 公開してはいけないもの ----

# ビルドが作る、公開して当然のファイル。ここに書いたものだけを例外にする。
# （拡張子でまとめて許すことはしない。feed.xml と sitemap.xml だけを名指しする）
PUBLIC_GENERATED = frozenset({
    "docs/feed.xml",
    "docs/sitemap.xml",
})

NEVER_PUBLISH_RULES = (
    # OS が作る不要ファイルを先に見る。Thumbs.db を「データベース」と
    # 言われても分かりにくいので、理由を正しく出すため順番が大事。
    (re.compile(r"(?i)(^|/)(Thumbs\.db|desktop\.ini|\.DS_Store)$"),
     "OS が作る不要ファイル"),
    (re.compile(r"(?i)\.(zip|7z|rar|tar|gz|tgz|bz2|xz|lzh|cab)$"),
     "書庫ファイル（バックアップの取り違え）"),
    (re.compile(r"(?i)\.xml$"),
     "XML（WordPress の書き出しの可能性）"),
    (re.compile(r"(?i)\.(sql|db|sqlite|sqlite3|mdb)$"),
     "データベースの書き出し"),
    (re.compile(r"(?i)\.(pem|key|ppk|pfx|p12|crt|cer|der|jks|keystore)$"),
     "鍵や証明書"),
    (re.compile(r"(?i)(^|/)id_(rsa|dsa|ecdsa|ed25519)"),
     "SSH の秘密鍵"),
    (re.compile(r"(?i)(^|/)\.env"),
     "環境変数の設定（認証情報が入ることが多い）"),
    (re.compile(r"(?i)(^|/)(secret|secrets|token|tokens|credential|credentials|"
                r"apikey|api_key|password)[^/]*\.(json|ya?ml|txt|ini|cfg|conf|xml)$"),
     "認証情報らしいファイル"),
    (re.compile(r"(?i)\.(docx?|odt|rtf|pages|xlsx?|ods|pptx?|odp)$"),
     "文書ファイル（非公開の原稿の可能性）"),
    (re.compile(r"(?i)\.(bak|orig|tmp|swp|swo)$"),
     "編集ソフトの控え・一時ファイル"),
    (re.compile(r"(^|/)(00_Backup|01_Analysis|02_Archive|10_下書き|20_素材|90_旧)/"),
     "公開しないフォルダの中身"),
    (re.compile(r"(?i)(^|/)wp-content/"),
     "WordPress の内部フォルダ"),
    (re.compile(r"(?i)(^|/)~\$"),
     "Office の一時ファイル"),
)


def never_publish(path):
    """公開してはいけないファイルなら日本語の理由を返す。問題なければ None。"""
    # 「./」が付いていたら外すだけ。lstrip("./") を使ってはいけない。
    # lstrip は文字の集合を削るので、「.env」が「env」になり、
    # 「.DS_Store」が「DS_Store」になって、どちらも素通りしてしまう。
    rel = (path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel:
        return None
    if rel in PUBLIC_GENERATED:
        return None
    for pattern, reason in NEVER_PUBLISH_RULES:
        if pattern.search(rel):
            return reason
    return None


# 本文や設定に混じってはいけない文字列。
LEAK_PATTERNS = (
    (re.compile(r"(?i)[A-Za-z]:[\\/]+Users[\\/]"), "Windows の個人フォルダのパス"),
    (re.compile(r"(?i)gh[pousr]_[A-Za-z0-9]{16,}"), "GitHub のアクセストークンらしい文字列"),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9]{20,}"), "API キーらしい文字列"),
    (re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"), "秘密鍵"),
    (re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret)\s*[:=]\s*\S{6,}"),
     "パスワードや API キーらしい記述"),
)


def leaks_in(text):
    """文字列に含まれる漏えいの疑いを返す。値そのものは返さない。"""
    return [reason for pattern, reason in LEAK_PATTERNS if pattern.search(text or "")]


# ---- 意図された例外 ----

# 旧URLが記事の題と一致しない4件。WordPress 時代の URL をそのまま保つため、
# このままにしておく。検査ではこの表に載っているものを警告しない。
LEGACY_EXCEPTIONS = {
    "2026-10-03": "10月3日",
    "2026-07-21": "2026年7月2１日",          # 全角の１
    "hello-world": "初投稿】ブログ、はじめました。日常の記録と頭",
    "circleback-review": "議事録アプリ「circleback」の感想",
}

# 投稿日が日記の日付より何日後までを当たり前とみなすか。
# 既存の 2026-08-07 は2日後。これを超えると注意を出す（はねない）。
POST_DATE_GAP_WARN = 3

# ビルドが作らない公開ファイル。再現性の照合では守れないので、
# baseline.json に指紋を残して見張る。
NON_GENERATED_DOCS_PREFIX = ("images/", "assets/")
