# -*- coding: utf-8 -*-
"""GitHub の投稿フォーム（Issue）から届いた日記を、記事として取り込む。

使い方（GitHub Actions から呼ぶ）:
    python post_from_issue.py --json 結果.json --pr-body 本文.md --comment 返事.md

受け取り方:
    画面からの入力は使わない。次の環境変数から読む。
        ISSUE_BODY    投稿フォームの本文（必須）
        ISSUE_NUMBER  Issue の番号（必須）
        ISSUE_TITLE   Issue の題（省略可・記録には使わない）

    環境変数で受け取るのは、安全のためでもある。
    ワークフローの run: に本文を直接書くと、本文に書かれた文字が
    コマンドとして実行されてしまう（コマンド差し込み）。
    環境変数なら、どんな文字が入っていても文字のまま渡る。

安全のための決まり:
    - 「公開の確認」に印が付いていないものは取り込まない
    - 登録されていないカテゴリーは断る（勝手に登録しない）
    - 同じ Issue からの二重取り込みはしない（source_ref で見分ける）
    - 公開済みとして記録されている記事は上書きしない
    - 本文は HTML として安全な形に直してから記事にする（ingest.to_html）

このツールは記事ファイルを1件書き出すだけ。組み立て・検査・送信はしない。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import ingest
import rules

common.use_utf8_stdout()

# 投稿フォーム（.github/ISSUE_TEMPLATE/post.yml）の見出し。
# ここに載っている見出しだけを区切りとして扱う。本文の中に
# 「### なにか」と書かれていても、区切りとは見なさない。
FIELD_BODY = "本文"
FIELD_DATE = "日付"
FIELD_TITLE = "タイトル"
FIELD_CATEGORY = "カテゴリー"
FIELD_TAGS = "タグ"
FIELD_CONFIRM = "公開の確認"
FIELDS = (FIELD_BODY, FIELD_DATE, FIELD_TITLE, FIELD_CATEGORY,
          FIELD_TAGS, FIELD_CONFIRM)

HEADING = re.compile(r"^###[ \t]*(.+?)[ \t]*$", re.M)
CHECKED = re.compile(r"^[ \t]*[-*][ \t]*\[[xX]\]", re.M)
# GitHub が「未入力」のときに入れる文字。
NO_RESPONSE = ("_No response_", "*No response*", "_未入力_")


def parse_form(text):
    """投稿フォームの本文を、見出しごとの値に分ける。

    フォームの形をしていなければ None を返す。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    marks = [(m.start(), m.end(), m.group(1))
             for m in HEADING.finditer(text) if m.group(1) in FIELDS]
    if not marks:
        return None
    out = {}
    for i, (_start, end, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out[name] = text[end:stop].strip("\n")
    return out


def value_of(form, name):
    """未入力を空文字にそろえて取り出す。"""
    raw = (form.get(name) or "").strip()
    if raw in NO_RESPONSE:
        return ""
    return raw


class Refused(Exception):
    """取り込まない理由（画面と Issue への返事に、そのまま出す）。"""

    def __init__(self, reason, hint=""):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


def build(catalog, cfg, body_raw, number):
    """フォームの本文から記事を1件作る。戻り値は ingest.Plan。"""
    form = parse_form(body_raw)
    if form is None:
        raise Refused(
            "投稿フォームの形になっていません。",
            "GitHub の Issues → New issue →「日記を投稿する」から投稿してください。")

    if FIELD_CONFIRM not in form or not CHECKED.search(form[FIELD_CONFIRM]):
        raise Refused(
            "「公開の確認」に印が付いていません。",
            "このリポジトリは公開リポジトリです。公開してよいと決めた文章だけを"
            "投稿してください。確認の印を付けて、この Issue を編集してください。")

    body_text = value_of(form, FIELD_BODY)
    if not body_text:
        raise Refused("本文が空です。", "「本文」の欄に日記を書いてください。")

    # 題とタグは、本文の先頭に書いてあっても読む（TXT の書き方と同じ）。
    # 欄に入力があれば、そちらを優先する。
    head_title, head_tags, body_text = ingest.split_header(body_text)
    title = value_of(form, FIELD_TITLE) or head_title or None

    tags = ingest.normalize_tags(value_of(form, FIELD_TAGS)) or head_tags

    day_text = value_of(form, FIELD_DATE)
    day, why = ingest.parse_date(day_text)
    if why:
        raise Refused(why, "「日付」の欄を直して、この Issue を編集してください。")
    if day is None:
        day = ingest.local_today(cfg)

    category = value_of(form, FIELD_CATEGORY) or catalog.default_category

    plan = catalog.plan(
        title=title, body_text=body_text, day=day, category=category, tags=tags,
        source="issue",
        # もとのファイル名のかわり。日付を含めておくことで、検査（A5）の
        # 「もとの名前に日付が入っているか」の点検がそのまま効く。
        source_name="%s-issue-%d.txt" % (day.isoformat(), number),
        # 同じ Issue からの二重取り込みを止めるための目印。
        # 日付や題を直して送り直されても、同じ Issue なら増やさない。
        source_ref="issue-%d" % number,
        digest=ingest.text_digest(body_raw),
        today=ingest.local_today(cfg))

    if plan.code == "already":
        raise Refused("この投稿は、すでに取り込み済みです。",
                      "二重投稿はしません。")
    if not plan.ok:
        raise Refused(plan.reason, HINTS.get(plan.code, ""))
    return plan


HINTS = {
    "empty-body": "「本文」の欄に日記を書いてください。",
    "too-long": "本文が大きすぎます。分けて投稿してください。",
    "same-body": "同じ本文の記事がすでにあります。二重投稿はしません。",
    "same-source": "同じ日付・同じ Issue 番号で取り込み済みです。",
    "same-ref": "この Issue からは、すでに記事を作っています。二重投稿はしません。",
    "date-out-of-range": "「日付」の欄を、ブログの範囲内の日付に直してください。",
    "unknown-category": "「カテゴリー」の欄を、登録済みのものから選んでください。"
                        "新しいカテゴリーは、手元の 1_日記を取り込む.bat から登録します。",
    "bad-tag": "「タグ」の欄に、URL に使えない文字が入っています。",
    "bad-category-url": "「カテゴリー」の欄を選び直してください。",
    "bad-slug": "「日付」の欄を直してください。",
    "outside": "「カテゴリー」の欄を選び直してください。",
    "frozen": "その日付の記事は公開済みとして記録されています。"
              "直すときは手元で baseline.py --修正許可 を実行してください。",
}


def write_text(path, text):
    if not path:
        return
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip() + "\n")


def option(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main():
    args = sys.argv[1:]
    json_out = option(args, "--json")
    pr_out = option(args, "--pr-body")
    comment_out = option(args, "--comment")

    body_raw = os.environ.get("ISSUE_BODY") or ""
    number_raw = (os.environ.get("ISSUE_NUMBER") or "").strip()

    print("=" * 56)
    print(" 投稿フォームから記事を作ります")
    print("=" * 56)

    if not number_raw.isdigit():
        print("[中止] Issue 番号が渡されていません（環境変数 ISSUE_NUMBER）。")
        return 1
    number = int(number_raw)
    print("投稿の番号: #%d" % number)

    if not body_raw.strip():
        print("[中止] 投稿の本文が空です（環境変数 ISSUE_BODY）。")
        return 1

    cfg = common.load_config()
    catalog = ingest.Catalog(cfg)
    print("使えるカテゴリー: %s" % " / ".join(catalog.allowed_categories()))
    print()

    try:
        plan = build(catalog, cfg, body_raw, number)
    except Refused as refused:
        print("[取り込みません] %s" % refused.reason)
        if refused.hint:
            for line in refused.hint.split("\n"):
                print("                 %s" % line)
        if json_out:
            write_text(json_out, json.dumps(
                {"ok": False, "reason": refused.reason, "hint": refused.hint,
                 "issue": number}, ensure_ascii=False, indent=1))
        if comment_out:
            write_text(comment_out, "\n".join([
                "## 記事を作れませんでした",
                "",
                "- 理由: %s" % refused.reason,
            ] + (["- 直しかた: %s" % refused.hint] if refused.hint else []) + [
                "",
                "この Issue を編集すると、もう一度試します。",
            ]))
        return 1

    catalog.write(plan)
    print("  [取り込み] #%d → /posts/%s/  「%s」 %d字%s"
          % (number, plan.slug, plan.title, plan.chars,
             "  タグ: " + "・".join(plan.tags) if plan.tags else ""))
    if plan.added_suffix:
        print("             [注意] /posts/%s/ はすでにあるため、別の記事として足します。"
              % plan.added_suffix)
    if plan.chars < rules.SHORT_BODY_CHARS:
        print("             [注意] 本文が %d 字しかありません。" % plan.chars)
    print("\n記事ファイル: %s"
          % os.path.relpath(plan.out_path, common.ROOT).replace(os.sep, "/"))

    info = {
        "ok": True, "issue": number, "slug": plan.slug, "title": plan.title,
        "diary_date": plan.day.isoformat(), "category": plan.category,
        "tags": plan.tags, "chars": plan.chars,
        "path": os.path.relpath(plan.out_path, common.ROOT).replace(os.sep, "/"),
    }
    if json_out:
        write_text(json_out, json.dumps(info, ensure_ascii=False, indent=1))

    if pr_out:
        write_text(pr_out, "\n".join([
            "## 日記を1件追加します",
            "",
            "| 項目 | 内容 |",
            "|---|---|",
            "| 題 | %s |" % plan.title,
            "| 日付 | %s |" % plan.day.isoformat(),
            "| カテゴリー | %s |" % plan.category,
            "| タグ | %s |" % ("・".join(plan.tags) if plan.tags else "（なし）"),
            "| 文字数 | %d 字 |" % plan.chars,
            "| 公開される場所 | `/posts/%s/` |" % plan.slug,
            "| もとの投稿 | #%d |" % number,
            "",
            "### この変更に入っているもの",
            "",
            "- `%s`（記事のもと）" % info["path"],
            "- `docs/` の作り直し（組み立て直した公開物）",
            "",
            "### 済んでいる検査",
            "",
            "- 自動テスト（`tools/selftest.py`）",
            "- 総合検査（`tools/check.py`）… 記事・公開済みの記録・公開物・衛生",
            "- リンクと旧URL転送の点検（`tools/verify.py`）",
            "",
            "検査はこの投稿の実行の中で、すべて済ませています。",
            "このプルリクエストは GitHub の自動トークンで作ったため、"
            "ここでの自動検査は「承認待ち」で止まります（GitHub の決まり）。",
            "もう一度走らせたいときは、下の **Approve workflows to run** を押してください。",
            "",
            "### 公開するには",
            "",
            "このプルリクエストを **Merge** すると公開されます（数分で反映）。",
            "公開したくないときは **Close** して、ブランチを削除してください。",
        ]))

    if comment_out:
        write_text(comment_out, "\n".join([
            "## 記事を作りました",
            "",
            "- 題: %s" % plan.title,
            "- 日付: %s" % plan.day.isoformat(),
            "- カテゴリー: %s" % plan.category,
            "- 公開される場所: `/posts/%s/`" % plan.slug,
            "",
            "検査はすべて通りました。下のプルリクエストを Merge すると公開されます。",
        ]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
