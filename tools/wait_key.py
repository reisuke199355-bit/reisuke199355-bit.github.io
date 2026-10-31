# -*- coding: utf-8 -*-
"""BAT の最後に日本語の案内を出して、キー入力を待つ。

cmd.exe は日本語を含む BAT ファイルを正しく読めないことがあるため、
BAT 側は英数字だけにして、画面に出す日本語はすべて Python から出している。

使い方:
    python wait_key.py            終了の案内だけ
    python wait_key.py preview    「次はプレビュー」の案内を添える
    python wait_key.py publish    「次は公開」の案内を添える
    python wait_key.py error      失敗したときの案内
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

MESSAGES = {
    "preview": "取り込みとサイトの作り直しが終わりました。\n"
               "次は「2_プレビュー.bat」を開いて、表示を確認してください。",
    "publish": "表示に問題がなければ「3_GitHubへ公開.bat」で公開できます。",
    "error": "エラーが起きたため中断しました。上に出ている内容を確認してください。\n"
             "python が見つからない場合は、Python のインストールを確認してください。",
}


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    message = MESSAGES.get(key)
    if message:
        print("\n" + message)
    print("\n閉じるには Enter を押してください。", end="")
    sys.stdout.flush()
    try:
        sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
