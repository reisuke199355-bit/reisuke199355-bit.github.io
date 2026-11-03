# れいすけの日常の記録

日記ブログの本体です。WordPress から移行し、GitHub Pages で公開しています。

## 構成

| 場所 | 内容 |
|---|---|
| `content/posts/` | 記事のもと（1記事＝1ファイル。`---` で囲んだ情報＋HTML本文） |
| `inbox/` | スマホ・クラウドからの投稿の受け取り箱（TXT を置くと記事になる） |
| `content/pages/` | 固定ページ |
| `templates/` | ページの型 |
| `tools/` | 変換・生成・公開・点検のスクリプト（Python 3、追加のインストール不要） |
| `docs/` | **公開されるサイト本体**（自動生成。直接編集しない） |
| `config.json` | サイト名、URL、カテゴリー、タグの URL 名などの設定 |
| `baseline.json` | 公開済みの記事・旧URL・公開ファイルの記録（自動生成。手で編集しない） |
| `windows/` | Windows へコピーして使う追加の BAT |
| `OPERATIONS.md` | 運用の手引き（検査の読み方、カテゴリーの増やし方、過去記事の直し方） |
| `MANUAL-cloud-posting.md` | **スマホから投稿する手引き**（設定・手順・止まったときの直しかた） |
| `DESIGN-cloud-posting.md` | スマホ投稿・クラウド公開の設計メモ（方式の比較と、選んだ理由） |

## スクリプト

### ふだん使うもの

| コマンド | 役割 |
|---|---|
| `python tools/import_txt.py` | 記事管理フォルダの TXT を記事として取り込む（フォルダ名＝カテゴリー） |
| `python tools/import_inbox.py` | `inbox/` の TXT を記事として取り込む（スマホ・クラウドから） |
| `python tools/sync.py` | クラウドで公開した日記を、手元のフォルダに取り込む |
| `python tools/build.py` | `content/` から `docs/` を組み立てる |
| `python tools/preview.py` | `docs/` をローカルサーバーで表示する |
| `python tools/publish.py` | 自動検査のうえ GitHub へ送信する |

### 点検するもの

| コマンド | 役割 |
|---|---|
| `python tools/check.py` | 総合検査。記事・公開済みの記録・公開物・衛生をまとめて点検（**何も書き換えない**） |
| `python tools/verify.py` | リンクと旧URLからの転送の点検 |
| `python tools/baseline.py` | 公開済みの記事が変わっていないかを記録と見比べる |
| `python tools/selftest.py` | 取り込み・投稿・検査の仕組みが働くことの自動テスト（82項目） |
| `python tools/rehearse_post.py` | クラウド投稿の予行演習（一時フォルダで通しで試す。本番に触らない） |

### ときどき使うもの

| コマンド | 役割 |
|---|---|
| `python tools/set_profile_image.py` | プロフィール画像を設定する |
| `python tools/wp_import.py` | WordPress の XML から取り込む（移行時に1回だけ使用） |

ふだんは、ひとつ上のフォルダにある `1_日記を取り込む.bat` `2_プレビュー.bat`
`3_GitHubへ公開.bat` から操作します。使い方は `../使い方.md` を参照してください。

## まちがいを防ぐ仕組み

| 仕組み | 何を防ぐか |
|---|---|
| カテゴリーの登録制 | 見慣れないフォルダ名が勝手にカテゴリーになるのを防ぐ |
| 日付の必須化 | ファイル名に日付がないもの（説明書など）が記事になるのを防ぐ |
| 記事でない名前の除外 | 説明書・使い方・メモ・README などを記事にしない |
| 本文の中身での重複判定 | 同じ日記が二重に増えるのを防ぐ（太字や `&` を使った日記もそろえて見る） |
| 投稿元の目印（`source_ref`） | 同じ投稿フォームから2回記事ができるのを防ぐ |
| 投稿は作業ブランチ止まり | クラウドからの投稿が、人の確認なしに公開されるのを防ぐ |
| `baseline.json` | 公開済み109記事の本文・日付・URL・旧URL転送が変わるのを防ぐ |
| 作り直しての照合 | `docs/` を手で直した・古いまま公開しているのを見つける |
| 衛生の点検 | WordPress の書き出し・鍵・非公開の原稿が入るのを防ぐ |
| GitHub Actions | 公開のたびに上記すべてを自動で点検する |

くわしい手順は `OPERATIONS.md` にあります。

### カテゴリーを増やす

`10_記事管理` に新しいフォルダを作って TXT を置き、`1_日記を取り込む.bat` を実行すると、
登録するかどうかを日本語で尋ねられます。「はい」と答えると `config.json` に登録され、
そのまま取り込まれます。

### 過去記事を直す

公開済みの記事には鍵がかかっています。意図して直すときは次の順に進めます。

```bash
python tools/baseline.py --修正許可 2026-08-06
```

```bash
python tools/build.py
```

```bash
python tools/baseline.py --update
```

くわしくは `OPERATIONS.md` の「過去記事を直したいとき」を参照してください。

## GitHub Pages の設定

- Source: `Deploy from a branch`
- Branch: `main`
- Folder: `/docs`

`docs/.nojekyll` を置いているため、Jekyll による変換は行われず、ファイルがそのまま配信されます。

この設定は変更しません。GitHub Actions の自動検査は**点検だけ**を行い、
公開（デプロイ）には関わりません。

## 自動検査（GitHub Actions）

`.github/workflows/check.yml` が、push とプルリクエストのたびに
`selftest.py` → `check.py` → `verify.py` を実行し、結果を日本語で表示します。

- 権限は読み取りだけ（`contents: read`）。リポジトリには書き込みません
- GitHub Pages の設定には触りません
- `main` へ直接 push したこと自体では失敗しません
  （`3_GitHubへ公開.bat` がそうしているため）。
  リンク切れ・公開済み記事の改変・秘密情報の混入など、
  **ほんとうの問題が見つかったときだけ**赤くなります

結果は Actions タブの **Summary** で日本語で読めます。
赤くなったときの手順は `OPERATIONS.md` にあります。

## スマホからの投稿（V02）

パソコンを起動しなくても、スマートフォンやブラウザから日記を投稿できます。

```
inbox に TXT を置く／投稿フォームに書く
      │
      ▼  GitHub Actions（記事づくり → 組み立て → 検査）
作業ブランチ ＋ プルリクエスト
      │
      ▼  人が Merge を押す
GitHub Pages で公開
```

- **main へは自動で入りません。** 公開は人が Merge したときだけです
- 検査に落ちたときは記事は作られず、理由が日本語で届きます
- 判定の決まりは `tools/rules.py` の1か所。入口が増えても同じ決まりが当たります
- これまでの BAT の手順は、まったく変わりません

| ワークフロー | いつ動くか | 何をするか |
|---|---|---|
| `.github/workflows/post-from-inbox.yml` | 作業ブランチの `inbox/` に push | 記事づくり → 検査 → プルリクエスト |
| `.github/workflows/post-from-issue.yml` | **いまは止めてある**（戻すまで動かない） | 同上 |
| `.github/workflows/post-rehearsal.yml` | `tools/` などを変えたとき | 投稿の仕組みの予行演習（書き込みの権限なし） |

> このリポジトリは公開リポジトリです。GitHub に渡した文章は、
> 公開される前から第三者が読め、**取り消せません**。
> 書きかけの下書きは GitHub に置かず、端末の中に置いてください。
> くわしくは `MANUAL-cloud-posting.md` の冒頭を読んでください。

手順・最初に1回だけ必要な設定・止まったときの直しかたは
`MANUAL-cloud-posting.md` にまとめてあります。

## 移行の記録

- 移行元: WordPress 7.1.4（`reisuke-ambition.com`）
- 移行した記事: 106件（2026-07-19 〜 2026-10-30 投稿）
- 画像: 本文が参照している4件
- 旧URLからの転送ページ: 108枚（`docs/` 直下の日本語フォルダ）
