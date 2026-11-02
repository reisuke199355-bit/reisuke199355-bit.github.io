# -*- coding: utf-8 -*-
"""プロフィール画像を設定する。

使い方:
    python set_profile_image.py <画像ファイル>    PNG または JPG を指定する
    python set_profile_image.py --clear           画像を外して仮アイコンに戻す

することは3つ。
  1. 指定された画像の原本を「20_素材」へ控え、読み取り専用にする。
  2. 正方形に切り出して小さくしたものを docs/images/ に置く（公開されるのはこちら）。
  3. config.json の profile.image を、置いた画像に合わせる。

元の画像ファイルには手を触れない。
"""
import json
import os
import shutil
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

common.use_utf8_stdout()

SIZE = 320                      # 表示は76pxなので、画面が高精細でも足りる大きさ
OUT_REL = "images/profile"      # 拡張子はあとで付ける
MATERIALS = "../20_素材"
ALLOWED = {".png", ".jpg", ".jpeg"}


def materials_dir():
    path = os.path.normpath(os.path.join(common.ROOT, MATERIALS))
    os.makedirs(path, exist_ok=True)
    return path


def save_config(image_rel):
    import collections
    path = os.path.join(common.ROOT, "config.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f, object_pairs_hook=collections.OrderedDict)
    cfg.setdefault("profile", collections.OrderedDict())["image"] = image_rel
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def remove_existing():
    removed = []
    for ext in (".png", ".jpg"):
        path = os.path.join(common.DOCS, "images", "profile" + ext)
        if os.path.isfile(path):
            os.remove(path)
            removed.append(os.path.basename(path))
    return removed


def clear():
    removed = remove_existing()
    save_config("")
    print("プロフィール画像を外しました。頭文字の仮アイコンが表示されます。")
    if removed:
        print("消したファイル: %s" % "・".join(removed))
    print("「2_プレビュー.bat」を実行すると画面に反映されます。")
    return 0


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "--clear":
        return clear()

    src = os.path.abspath(args[0].strip('"'))
    if not os.path.isfile(src):
        print("ファイルが見つかりません: %s" % src)
        return 1
    ext = os.path.splitext(src)[1].lower()
    if ext not in ALLOWED:
        print("PNG か JPG を指定してください（指定されたもの: %s）" % (ext or "拡張子なし"))
        return 1

    try:
        from PIL import Image
    except ImportError:
        print("画像を縮小するための Pillow が入っていません。")
        print("コマンドプロンプトで次を実行してから、もう一度お試しください。")
        print("    pip install Pillow")
        return 1

    # 1. 原本を控える（元のファイルは動かさない）
    keep = os.path.join(materials_dir(), "profile_原本" + ext)
    if os.path.exists(keep):
        os.chmod(keep, stat.S_IWRITE)
    shutil.copy2(src, keep)
    os.chmod(keep, stat.S_IREAD)
    print("原本を控えました（読み取り専用）: %s" % keep)

    # 2. 正方形に切り出して小さくする
    with Image.open(src) as im:
        im.load()
        width, height = im.size
        side = min(width, height)
        box = ((width - side) // 2, (height - side) // 2,
               (width - side) // 2 + side, (height - side) // 2 + side)
        im = im.crop(box)
        if side > SIZE:
            im = im.resize((SIZE, SIZE), Image.LANCZOS)

        remove_existing()
        if ext == ".png":
            out_ext, out_rel = ".png", OUT_REL + ".png"
            im = im.convert("RGBA")
            params = {"optimize": True}
        else:
            out_ext, out_rel = ".jpg", OUT_REL + ".jpg"
            im = im.convert("RGB")
            params = {"quality": 86, "optimize": True, "progressive": True}
        out_path = os.path.join(common.DOCS, out_rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        im.save(out_path, **params)

    size_kb = os.path.getsize(out_path) / 1024
    print("公開用の画像を作りました: docs/%s（%dx%d / %.0f KB）"
          % (out_rel, im.size[0], im.size[1], size_kb))

    # 3. 設定に反映する
    save_config(out_rel)
    print("config.json の profile.image を「%s」にしました。" % out_rel)
    print("\n「2_プレビュー.bat」を実行すると、トップページとプロフィールに表示されます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
