# -*- coding: utf-8 -*-
"""docs/ をローカルサーバーで表示して確認する。

使い方:
    python preview.py            8000番から空いているポートを探して起動
    python preview.py 8080       ポートを指定して起動
    python preview.py --no-open  ブラウザを自動で開かない

GitHub Pages と同じように http:// で開くため、日本語URLや相対パスの
動きをそのまま確認できる。Ctrl+C で止める。
"""
import functools
import http.server
import os
import socket
import socketserver
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

common.use_utf8_stdout()


class Handler(http.server.SimpleHTTPRequestHandler):
    """GitHub Pages に近い挙動にする（404.html を返す、ログを静かにする）。"""

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            page = os.path.join(common.DOCS, "404.html")
            if os.path.isfile(page):
                body = open(page, "rb").read()
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
        super().send_error(code, message, explain)

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else ""
        if str(status).startswith(("4", "5")):
            sys.stderr.write("  %s %s\n" % (status, args[0]))


class Server(socketserver.ThreadingTCPServer):
    """接続ごとにスレッドを分ける。

    1つずつしか応答しない作りだと、ブラウザが先回りで開いた接続に
    サーバー全体が待たされて、ほかのページが開けなくなることがある。
    """

    allow_reuse_address = True
    daemon_threads = True


def free_port(start):
    for port in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def main():
    if not os.path.isdir(common.DOCS) or not os.path.isfile(os.path.join(common.DOCS, "index.html")):
        print("docs/index.html がありません。先に build.py を実行してください。")
        return 1

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = free_port(int(args[0]) if args else 8000)
    url = "http://localhost:%d/" % port

    handler = functools.partial(Handler, directory=common.DOCS)
    with Server(("127.0.0.1", port), handler) as httpd:
        print("プレビューを開始しました: %s" % url)
        print("公開フォルダ: %s" % common.DOCS)
        print("止めるときは、このウィンドウで Ctrl+C を押してください。\n")
        if "--no-open" not in sys.argv[1:]:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nプレビューを終了しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
