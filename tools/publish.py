# -*- coding: utf-8 -*-
"""blog フォルダを GitHub へ送信する（実行者の確認を必ず求める）。

使い方:
    python publish.py

使い方（つづき）:
    python publish.py --検査を省く   自動検査をとばして送信する（おすすめしません）

送信前に次を確かめる。ひとつでも引っかかれば中断する。
  - 自動検査（check.py）で重大な問題が見つかっていないか
  - バックアップの ZIP や WordPress の XML が含まれていないか
    （RSS の feed.xml と sitemap.xml は公開して当然のものなので、止めない）
  - config.json の site_url が設定されているか
  - 何が送られるかを表示し、本人が「はい」と打ったか

ふだんの日記の公開は、これまでと同じ手順のまま。
検査に通れば、追加の操作は何もいらない。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import rules

common.use_utf8_stdout()


def git(*args, check=True, quiet=False):
    result = subprocess.run(["git"] + list(args), cwd=common.ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        if not quiet:
            print("git %s に失敗しました:\n%s" % (" ".join(args), result.stderr.strip()))
        raise SystemExit(1)
    return result.stdout.strip()


def git_paths(*args):
    """ファイルの一覧を得る。日本語のファイル名もそのままの形で受け取る。

    git は既定で日本語のパスを \\346\\227\\245 のような形に書き換えてしまう。
    画面に出すときに読めないだけでなく、名前での判定もずれるため -z を使う。
    """
    result = subprocess.run(["git"] + list(args) + ["-z"], cwd=common.ROOT,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return []
    return [p for p in result.stdout.split("\0") if p]


def ask(question):
    print("\n" + question)
    print('  続けるなら「はい」と入力して Enter（やめるなら、そのまま Enter）: ', end="")
    sys.stdout.flush()
    return sys.stdin.readline().strip() in ("はい", "ハイ", "yes", "y", "Y")


def run_checks():
    """送信の前に自動検査を走らせる。重大な問題があれば False を返す。

    検査は何も書き換えない（作り直しての照合は一時フォルダで行う）。
    """
    print("\n送信の前に、自動検査を行います（1秒ほど）...")
    result = subprocess.run([sys.executable,
                             os.path.join(common.ROOT, "tools", "check.py")],
                            cwd=common.ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode == 0:
        for line in (result.stdout or "").split("\n"):
            if "[注意]" in line:
                print(line)
        print("  [OK] 自動検査を通りました。")
        return True

    print("\n" + "*" * 56)
    print("*** 中断します：自動検査で問題が見つかりました ***")
    print("*" * 56)
    shown = 0
    for line in (result.stdout or "").split("\n"):
        if "[NG]" in line:
            print(line)
            shown += 1
            if shown >= 15:
                print("  ... ほか（くわしくは python tools/check.py）")
                break
    if not shown:
        print((result.stdout or result.stderr or "").strip()[-1500:])
    print("\n直しかたは OPERATIONS.md に書いてあります。")
    print("くわしく見るには、次を実行してください。")
    print("  python tools/check.py")
    return False


def main():
    args = sys.argv[1:]
    skip_checks = any(a in ("--検査を省く", "--skip-checks") for a in args)
    print("=" * 56)
    print(" GitHub への公開")
    print("=" * 56)
    print("対象フォルダ: %s" % common.ROOT)
    if skip_checks:
        print("[注意] --検査を省く が指定されています。自動検査は行いません。")

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

    # 送信するものを決める前に、自動検査を済ませておく。
    # 検査は何も書き換えないので、ここで止まっても手元のファイルはそのまま。
    if not skip_checks and not run_checks():
        return 1

    # 公開してはいけないファイルが混じっていないか
    git("add", "-A")
    staged = git_paths("diff", "--cached", "--name-only")
    danger = [(f, rules.never_publish(f)) for f in staged]
    danger = [(f, why) for f, why in danger if why]
    if danger:
        print("\n*** 中断します：公開してはいけないファイルが含まれています ***")
        for f, why in danger:
            print("    %s" % f)
            print("        %s" % why)
        print("\n.gitignore を直し、必要なら git rm --cached で取り除いてから、")
        print("もう一度実行してください。")
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

    words = [a for a in args if not a.startswith("--")]
    message = " ".join(words) if words else "日記を更新"
    git("commit", "-m", message)
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    print("\nGitHub へ送信します（ブランチ: %s）..." % branch)
    result = subprocess.run(["git", "push", "-u", "origin", branch], cwd=common.ROOT)
    if result.returncode != 0:
        print("\n送信に失敗しました。GitHub の認証やリポジトリのアドレスを確認してください。")
        print("コミットは手元に残っているので、直してからもう一度実行できます。")
        print("\nスマホから投稿した日記を GitHub 側で公開したあとは、")
        print("先に GitHub の最新を取り込む必要があります。次のどちらかを実行してください。")
        print("  「4_クラウドの投稿を受け取る.bat」")
        print("  python tools/sync.py")
        return 1

    print("\n公開しました。")
    print("GitHub のリポジトリの Settings → Pages で、")
    print("  Source: Deploy from a branch / Branch: %s / Folder: /docs" % branch)
    print("を選ぶと、サイトが表示されるようになります（初回は数分かかります）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
