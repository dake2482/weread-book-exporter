#!/usr/bin/env python3
"""postprocess_ego.py — ego 版抓取结果的两个修正（在 crawl 完成后、export 之前运行）

1. 章节按官方目录（_catalog.json）重排：
   首屏多次重渲染会让最早落盘的几章顺序错乱（如"序"先于"版权信息"）。
   重排会同步改写 md 内的图片引用、raw/chap_*.json 的图片名，并重命名已下载图片文件。

2. meta.json 元数据修正：
   阅读页 __INITIAL_STATE__ 的 intro/publisher 会被推荐位数据污染，
   改从书籍详情页 og:description 取简介，从版权信息章取出版社/ISBN。
"""
import json
import os
import re
import sys
import urllib.request

from crawl_weread import norm

BOOK_ID = sys.argv[1] if len(sys.argv) > 1 else "15132580813abb808g015e9c"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", BOOK_ID + "_v2")
CH_DIR = os.path.join(ROOT, "chapters")
RAW_DIR = os.path.join(ROOT, "raw")
IMG_DIR = os.path.join(ROOT, "images")


def fetch_og_intro():
    url = f"https://weread.qq.com/web/bookDetail/{BOOK_ID}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    m = re.search(r'<meta property="og:description" content="([^"]*)"', html)
    return m.group(1) if m else ""


def catalog_index(title, catalog):
    nt = norm(title)
    if not nt:
        return None
    for i, c in enumerate(catalog):
        if norm(c) == nt:
            return i
    for i, c in enumerate(catalog):
        nc = norm(c)
        if nc and (nc.startswith(nt) or nt.startswith(nc)):
            return i
    return None


def fix_meta():
    meta_path = os.path.join(ROOT, "meta.json")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    changed = []
    intro = fetch_og_intro()
    if intro and intro[:30] != (meta.get("intro") or "")[:30]:
        meta["intro"] = intro
        changed.append("intro")

    # 从版权信息章提取出版社/出版时间/ISBN
    for fn in sorted(os.listdir(CH_DIR)):
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(CH_DIR, fn), encoding="utf-8") as f:
            head = f.read(600)
        if "出版社" in head and "ISBN" in head:
            if "机械工业出版社" in head and meta.get("publisher") != "机械工业出版社":
                meta["publisher"] = "机械工业出版社"
                changed.append("publisher")
            m = re.search(r"ISBN[：:]\s*([\dX]+)", head)
            if m and not meta.get("isbn"):
                meta["isbn"] = m.group(1)
                changed.append("isbn")
            m = re.search(r"出版时间[：:]\s*([\d-]+)", head)
            if m and not meta.get("publish_time"):
                meta["publish_time"] = m.group(1)
                changed.append("publish_time")
            break

    if changed:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"📝 元数据修正: {', '.join(changed) if changed else '无需修正'}")


def reorder_chapters():
    with open(os.path.join(ROOT, "_catalog.json"), encoding="utf-8") as f:
        catalog = json.load(f)

    pairs = []  # (old_num, title)
    for fn in sorted(os.listdir(CH_DIR)):
        m = re.match(r"^(\d+)\.md$", fn)
        if not m:
            continue
        with open(os.path.join(CH_DIR, fn), encoding="utf-8") as f:
            tm = re.match(r"^# (.+)$", f.readline())
        title = tm.group(1).strip() if tm else ""
        pairs.append((int(m.group(1)), title))

    if not pairs:
        print("❌ 无章节可重排")
        return

    unknown = [t for _, t in pairs if catalog_index(t, catalog) is None]
    if unknown:
        print(f"⚠️ {len(unknown)} 章未匹配到目录(将按原顺序排在末尾): {unknown[:5]}")

    ordered = sorted(
        pairs,
        key=lambda p: (
            1 if catalog_index(p[1], catalog) is None else 0,
            catalog_index(p[1], catalog) if catalog_index(p[1], catalog) is not None else 10**6,
            p[0],
        ),
    )

    mapping = {old: new for new, (old, _) in enumerate(ordered, start=1)}
    if mapping == {old: old for old, _ in pairs}:
        print("✅ 章节顺序已符合目录，无需重排")
        return

    moved = [(o, n) for o, n in mapping.items() if o != n]
    print(f"🔀 重排 {len(moved)} 个章节的位置: {moved[:8]}{' ...' if len(moved) > 8 else ''}")

    tmp = os.path.join(ROOT, "_reorder_tmp")
    os.makedirs(tmp, exist_ok=True)

    # 1) md 内容改图片引用并写到临时目录
    for old, new in mapping.items():
        with open(os.path.join(CH_DIR, "%04d.md" % old), encoding="utf-8") as f:
            body = f.read()
        if old != new:
            body = body.replace("images/ch%04d_img" % old, "images/ch%04d_img" % new)
        with open(os.path.join(tmp, "%04d.md" % new), "w", encoding="utf-8") as f:
            f.write(body)

    # 2) raw/chap_*.json 同步改名与图片名
    for old, new in mapping.items():
        src = os.path.join(RAW_DIR, "chap_%04d.json" % old)
        if not os.path.exists(src):
            continue
        with open(src, encoding="utf-8") as f:
            data = json.load(f)
        if old != new:
            for im in data.get("images", []):
                if "name" in im:
                    im["name"] = im["name"].replace("ch%04d_img" % old, "ch%04d_img" % new)
        with open(os.path.join(tmp, "chap_%04d.json" % new), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    # 3) 重命名已下载的图片文件
    ren = 0
    if os.path.isdir(IMG_DIR):
        for old, new in mapping.items():
            if old == new:
                continue
            for fn in os.listdir(IMG_DIR):
                m = re.match(r"^ch%04d_(img\d+\.\w+)$" % old, fn)
                if m:
                    os.rename(os.path.join(IMG_DIR, fn), os.path.join(tmp, "ch%04d_%s" % (new, m.group(1))))
                    ren += 1
    if ren:
        for fn in os.listdir(tmp):
            if re.match(r"^ch\d+_img", fn):
                dst = os.path.join(IMG_DIR, fn)
                if not os.path.exists(dst):
                    os.rename(os.path.join(tmp, fn), dst)

    # 4) 用临时目录内容替换原文件
    for fn in os.listdir(CH_DIR):
        if re.match(r"^\d+\.md$", fn):
            os.remove(os.path.join(CH_DIR, fn))
    for fn in os.listdir(RAW_DIR):
        if re.match(r"^chap_\d+\.json$", fn):
            os.remove(os.path.join(RAW_DIR, fn))
    for fn in os.listdir(tmp):
        if fn.endswith(".md"):
            os.rename(os.path.join(tmp, fn), os.path.join(CH_DIR, fn))
        elif fn.startswith("chap_"):
            os.rename(os.path.join(tmp, fn), os.path.join(RAW_DIR, fn))
    os.rmdir(tmp)
    print(f"✅ 重排完成，共 {len(mapping)} 章")


if __name__ == "__main__":
    fix_meta()
    reorder_chapters()
