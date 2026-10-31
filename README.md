# れいすけの日常の記録

日記ブログの本体です。WordPress から移行し、GitHub Pages で公開しています。

## 構成

| 場所 | 内容 |
|---|---|
| `content/posts/` | 記事のもと（1記事＝1ファイル。`---` で囲んだ情報＋HTML本文） |
| `content/pages/` | 固定ページ |
| `templates/` | ページの型 |
| `tools/` | 変換・生成・公開のスクリプト（Python 3、追加のインストール不要） |
| `docs/` | **公開されるサイト本体**（自動生成。直接編集しない） |
| `config.json` | サイト名、URL、カテゴリー・タグの URL 名などの設定 |

## スクリプト

| コマンド | 役割 |
|---|---|
| `python tools/build.py` | `content/` から `docs/` を組み立てる |
| `python tools/preview.py` | `docs/` をローカルサーバーで表示する |
| `python tools/import_txt.py` | 記事管理フォルダの TXT を記事として取り込む（フォルダ名＝カテゴリー） |
| `python tools/publish.py` | 確認のうえ GitHub へ送信する |
| `python tools/wp_import.py` | WordPress の XML から取り込む（移行時に1回だけ使用） |

ふだんは、ひとつ上のフォルダにある `1_日記を取り込む.bat` `2_プレビュー.bat`
`3_GitHubへ公開.bat` から操作します。使い方は `../使い方.md` を参照してください。

## GitHub Pages の設定

- Source: `Deploy from a branch`
- Branch: `main`
- Folder: `/docs`

`docs/.nojekyll` を置いているため、Jekyll による変換は行われず、ファイルがそのまま配信されます。

## 移行の記録

- 移行元: WordPress 7.1.4（`reisuke-ambition.com`）
- 移行した記事: 106件（2026-07-19 〜 2026-10-30 投稿）
- 画像: 本文が参照している4件
- 旧URLからの転送ページ: 108枚（`docs/` 直下の日本語フォルダ）
