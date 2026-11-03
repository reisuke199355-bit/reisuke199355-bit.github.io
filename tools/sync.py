# -*- coding: utf-8 -*-
"""クラウドで公開した日記を、手元のフォルダに取り込む（早送りだけ）。

使い方:
    python sync.py            GitHub の最新を手元に取り込む
    python sync.py --見るだけ  取り込まず、どれだけ違うかだけ見る

なぜ必要か:
    スマホから投稿してプルリクエストを取り込むと、GitHub 側だけが新しく
    なる。その状態で「3_GitHubへ公開.bat」を実行すると、送信のときに
    「先に取り込んでください」と断られてしまう。
    このツールを先に動かしておけば、そのまま公開できる。

安全のための決まり:
    - 早送り（fast-forward）できるときだけ取り込む。手元の変更は壊さない
    - 手元に書きかけがあるときは、何もせずに知らせる
    - git の履歴は書き換えない（rebase も force も使わない）
    - baseline.json は自分では作り直さない。必要なら案内だけ出す
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

common.use_utf8_stdout()


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=common.ROOT,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def out(*args):
    result = git(*args)
    return result.stdout.strip() if result.returncode == 0 else ""


def main():
    look_only = any(a in ("--見るだけ", "--dry-run") for a in sys.argv[1:])
    print("=" * 56)
    print(" クラウドの投稿を手元に取り込みます")
    print("=" * 56)
    print("対象フォルダ: %s" % common.ROOT)

    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        print("\nGit が見つかりません。https://git-scm.com/ からインストールしてください。")
        return 1
    if not os.path.isdir(os.path.join(common.ROOT, ".git")):
        print("\nまだ Git リポジトリではありません。先に「3_GitHubへ公開.bat」で公開してください。")
        return 1

    remote = out("remote", "get-url", "origin")
    if not remote:
        print("\n送信先（GitHub のリポジトリ）がまだ設定されていません。")
        print("先に「3_GitHubへ公開.bat」で公開してください。")
        return 1
    branch = out("rev-parse", "--abbrev-ref", "HEAD") or "main"
    print("送信先: %s" % remote)
    print("ブランチ: %s" % branch)

    dirty = [line for line in (out("status", "--porcelain") or "").split("\n") if line]
    if dirty:
        print("\n[中止] 手元に、まだ公開していない変更があります（%d 件）。" % len(dirty))
        for line in dirty[:10]:
            print("       %s" % line)
        if len(dirty) > 10:
            print("       ... ほか %d 件" % (len(dirty) - 10))
        print("\n先に「3_GitHubへ公開.bat」で公開するか、変更を元に戻してから")
        print("もう一度このツールを動かしてください。")
        return 1

    print("\nGitHub の最新を見に行きます...")
    fetched = git("fetch", "origin", branch)
    if fetched.returncode != 0:
        print("\n取得に失敗しました。ネットワークと GitHub の認証を確認してください。")
        print(fetched.stderr.strip()[-800:])
        return 1

    behind_ahead = out("rev-list", "--left-right", "--count",
                       "FETCH_HEAD...HEAD")
    try:
        behind, ahead = (int(n) for n in behind_ahead.split())
    except ValueError:
        print("\n[中止] 手元と GitHub の関係が読み取れませんでした。")
        return 1

    if behind == 0 and ahead == 0:
        print("\n手元と GitHub は同じです。取り込むものはありません。")
        return 0
    if behind == 0:
        print("\n手元のほうが %d 件進んでいます。取り込むものはありません。" % ahead)
        print("「3_GitHubへ公開.bat」で公開できます。")
        return 0
    if ahead > 0:
        print("\n[中止] 手元と GitHub が枝分かれしています"
              "（手元 %d 件 / GitHub %d 件）。" % (ahead, behind))
        print("このツールは早送りだけを行います。枝分かれは直せません。")
        print("どちらを残すか決めてから、手で合わせてください。")
        return 1

    print("\nGitHub のほうが %d 件新しくなっています。" % behind)
    titles = out("log", "--oneline", "--no-decorate", "HEAD..FETCH_HEAD")
    for line in (titles or "").split("\n")[:10]:
        if line:
            print("    %s" % line)
    names = out("diff", "--name-only", "HEAD", "FETCH_HEAD")
    new_posts = [n for n in (names or "").split("\n")
                 if n.startswith("content/posts/") and n.endswith(".html")]
    if new_posts:
        print("\n  記事の変化: %d 件" % len(new_posts))
        for name in new_posts[:10]:
            print("    %s" % name)

    if look_only:
        print("\n--見るだけ なので、取り込みませんでした。")
        return 0

    merged = git("merge", "--ff-only", "FETCH_HEAD")
    if merged.returncode != 0:
        print("\n取り込めませんでした（早送りできません）。")
        print(merged.stderr.strip()[-800:])
        return 1

    print("\n取り込みました。手元と GitHub がそろいました。")
    if new_posts:
        print("\nクラウドから入った記事があります。")
        print("この記事も「公開済み」として凍結しておくなら、次を実行してください。")
        print("  python tools/baseline.py --update")
        print("（やらなくても公開はできます。検査が「新しい記事が N 件」と知らせるだけです）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
