# -*- coding: utf-8 -*-
"""blog フォルダを GitHub へ送信する（実行者の確認を必ず求める）。

使い方:
    python publish.py

送信前に次を確かめる。ひとつでも引っかかれば中断する。
  - バックアップの ZIP や WordPress の XML が含まれていないか
  - config.json の site_url が設定されているか
  - 何が送られるかを表示し、本人が「はい」と打ったか
"""
import io
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

NEVER_PUBLISH = re.compile(r"(?i)\.(zip|7z|rar|tar|gz|sql|env|pem|key)$|wordpress.*\.xml$|^00_Backup/")


def git(*args, check=True, quiet=False):
    result = subprocess.run(["git"] + list(args), cwd=common.ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        if not quiet:
            print("git %s に失敗しました:\n%s" % (" ".join(args), result.stderr.strip()))
        raise SystemExit(1)
    return result.stdout.strip()


def ask(question):
    print("\n" + question)
    print('  続けるなら「はい」と入力して Enter（やめるなら、そのまま Enter）: ', end="")
    sys.stdout.flush()
    return sys.stdin.readline().strip() in ("はい", "ハイ", "yes", "y", "Y")


def main():
    print("=" * 56)
    print(" GitHub への公開")
    print("=" * 56)
    print("対象フォルダ: %s" % common.ROOT)

    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        print("\nGit が見つかりません。https://git-scm.com/ からインストールしてください。")
        return 1

    cfg = common.load_config()
    if not (cfg.get("site_url") or "").strip():
        print("\n[確認] config.json の site_url がまだ空です。")
        print("       このままでも記事は公開できますが、RSS と sitemap.xml は作られません。")
        print("       GitHub Pages のアドレス（例 https://ユーザー名.github.io/diary）を")
        print("       config.json の site_url と base_path に入れて、先に")
        print("       「2_プレビュー.bat」で作り直すことをおすすめします。")
        if not ask("site_url を空のまま公開しますか？"):
            print("中断しました。")
            return 0

    # リポジトリの用意
    if not os.path.isdir(os.path.join(common.ROOT, ".git")):
        print("\nまだ Git リポジトリではないので、ここで作ります。")
        if not ask("%s を Git リポジトリにしますか？" % common.ROOT):
            print("中断しました。")
            return 0
        git("init")
        git("symbolic-ref", "HEAD", "refs/heads/main")
        print("リポジトリを作りました（ブランチ: main）。")

    # 公開してはいけないファイルが混じっていないか
    git("add", "-A")
    staged = [line for line in git("diff", "--cached", "--name-only").split("\n") if line]
    danger = [f for f in staged if NEVER_PUBLISH.search(f)]
    if danger:
        print("\n*** 中断します：公開してはいけないファイルが含まれています ***")
        for f in danger:
            print("    %s" % f)
        print("\n.gitignore を直してから、もう一度実行してください。")
        git("reset", check=False)
        return 1

    if not staged:
        print("\n前回の公開から変わったところがありません。送信するものはありません。")
        return 0

    # 内容を見せる
    print("\n送信されるファイル: %d 件" % len(staged))
    shown = staged[:12]
    for f in shown:
        print("    %s" % f)
    if len(staged) > len(shown):
        print("    ... ほか %d 件" % (len(staged) - len(shown)))

    remote = git("remote", "get-url", "origin", check=False, quiet=True)
    if not remote:
        print("\n送信先（GitHub のリポジトリ）がまだ設定されていません。")
        print("GitHub でリポジトリを作ってから、そのアドレスを貼り付けてください。")
        print("例: https://github.com/ユーザー名/diary.git")
        print("\n  送信先: ", end="")
        sys.stdout.flush()
        url = sys.stdin.readline().strip()
        if not url:
            print("入力がなかったので中断しました。")
            git("reset", check=False)
            return 0
        git("remote", "add", "origin", url)
        remote = url
    print("\n送信先: %s" % remote)

    if not ask("この内容で GitHub に公開しますか？"):
        print("中断しました（ファイルはそのまま残っています）。")
        git("reset", check=False)
        return 0

    message = "日記を更新"
    if len(sys.argv) > 1:
        message = " ".join(sys.argv[1:])
    git("commit", "-m", message)
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    print("\nGitHub へ送信します（ブランチ: %s）..." % branch)
    result = subprocess.run(["git", "push", "-u", "origin", branch], cwd=common.ROOT)
    if result.returncode != 0:
        print("\n送信に失敗しました。GitHub の認証やリポジトリのアドレスを確認してください。")
        print("コミットは手元に残っているので、直してからもう一度実行できます。")
        return 1

    print("\n公開しました。")
    print("GitHub のリポジトリの Settings → Pages で、")
    print("  Source: Deploy from a branch / Branch: %s / Folder: /docs" % branch)
    print("を選ぶと、サイトが表示されるようになります（初回は数分かかります）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
