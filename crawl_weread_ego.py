#!/usr/bin/env python3
"""crawl_weread_ego.py — crawl_weread.py 的 ego lite 浏览器版驱动

复用 crawl_weread.py 的全部章节重组 / 目录匹配 / 落盘逻辑，
仅将 Playwright 驱动层替换为 ego-browser CLI（ego lite 浏览器）：

  - 登录态直接复用 ego lite 的用户配置（无需扫码，凭证在浏览器侧）
  - Canvas 挂钩通过 page.cdp("Page.addScriptToEvaluateOnNewDocument") 注入，
    并在每次取数前做幂等重挂（页面若发生整页刷新可自愈）
  - 每次翻页 = 一次 `ego-browser nodejs` 子进程调用：点击页面中部 → ArrowRight
    → 等绘制稳定 → drain 字符 → 收集插图，结果以 RESULT: JSON 行返回 Python
  - 翻页节奏（随机延时/长停顿）、断点续传、逐页落盘与原版完全一致

用法: python crawl_weread_ego.py <微信读书链接或ID> [--force]
"""
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request

from crawl_weread import (
    BASE, HOOK, IMGS_JS, PAGE_DELAY_RANGE, LONG_PAUSE_EVERY, LONG_PAUSE_RANGE,
    next_long_pause_at, get_crawl_state, save_crawl_state,
    extract_book_id, download_all_images,
    split_pages, page_lines, match_catalog_title, render_md, norm,
)

EGO_BIN = os.environ.get("EGO_BROWSER_BIN", os.path.expanduser("~/.local/bin/ego-browser"))

# 幂等版 HOOK：重复 evaluate 不会造成 fillText 二次包裹（否则字符会重复入列）
EGO_HOOK = HOOK.replace(
    "(function() {",
    "(function() {\n    if (window.__wr_installed) return;\n    window.__wr_installed = true;",
    1,
)
assert EGO_HOOK != HOOK, "HOOK 幂等护栏插入失败"


# ---------------------------------------------------------------- ego 桥接层

def ego_run(js, timeout=120, retries=2):
    """执行一段 ego-browser nodejs 脚本，返回其打印的 RESULT: JSON 行。
    多本书并发时 CLI 偶发争用/瞬断，自动重试。"""
    last_err = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(3 + 2 * attempt)
        try:
            proc = subprocess.run(
                [EGO_BIN, "nodejs", "-e", js],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            last_err = RuntimeError("ego-browser 调用超时 (%ds)" % timeout)
            continue
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = None
        for line in out.splitlines():
            if line.startswith("RESULT:"):
                m = line[len("RESULT:"):]
        if m is None:
            last_err = RuntimeError("ego-browser 未返回结果: " + out[-800:])
            continue
        data = json.loads(m)
        if isinstance(data, dict) and data.get("fatal"):
            raise RuntimeError("ego-browser 执行失败: %s" % data["fatal"])
        return data
    raise last_err


def js_preamble(space_arg):
    """连接（或创建）任务空间，并取回页面 p1"""
    return """
const task = await taskSpace(%s);
let page = null;
try { page = task.page("p1"); } catch (e) {}
if (!page) {
  const ps = await task.pages();
  if (ps.length) page = ps[0];
}
if (!page) {
  const tabs = await task.tabs();
  page = tabs.length ? await task.adopt(tabs[0].page) : await task.newPage();
}
""" % (json.dumps(space_arg) if isinstance(space_arg, str) else space_arg)


JS_WAIT_DRAIN = """
const waitStable = async (ms) => {
  let last = 0, ticks = 0;
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    let n = -1;
    try { n = await page.evaluate(() => (typeof window.__wr_count === "function" ? window.__wr_count() : -1)); } catch (e) { n = -1; }
    if (n === -1) return { lost: true, n: 0 };
    if (n > 0) {
      if (n === last) { if (++ticks >= 2) break; }
      else { last = n; ticks = 0; }
    }
    await new Promise((r) => setTimeout(r, 180));
  }
  return { lost: false, n: last };
};
const drain = async () => {
  let st = await waitStable(6000);
  if (st.lost) {  // 页面被整页刷新，挂钩丢失 → 幂等重挂后重试
    try { await page.evaluate(HOOK); } catch (e) {}
    await new Promise((r) => setTimeout(r, 1500));
    st = await waitStable(4000);
  }
  let chars = [];
  try { chars = await page.evaluate(() => (typeof window.__wr_drain === "function" ? window.__wr_drain() : [])); } catch (e) {}
  let imgs = [];
  try { imgs = await page.evaluate("(" + IMGS + ")()"); } catch (e) {}
  let vw = 1200;
  try { vw = await page.evaluate(() => window.innerWidth); } catch (e) {}
  return { chars, imgs, vw };
};
"""


def ego_setup(book_id, space_arg, timeout=360):
    """打开书籍页 → 校验登录 → 切双栏 → 取元数据与目录"""
    url = "https://weread.qq.com/web/reader/%s" % book_id
    js = js_preamble(space_arg) + """
const HOOK = %s;
const IMGS = %s;
const main = async () => {
  try { await page.cdp("Page.addScriptToEvaluateOnNewDocument", { source: HOOK }); } catch (e) {}
  await page.goto(%s, { timeout: 60000 });
  await new Promise((r) => setTimeout(r, 3000));
  try {
    const t = await page.evaluate(() => typeof window.__wr_drain);
    if (t !== "function") await page.evaluate(HOOK);
  } catch (e) { try { await page.evaluate(HOOK); } catch (e2) {} }

  const cookieNames = async () => {
    try {
      const r = await page.cdp("Network.getCookies", { urls: ["https://weread.qq.com"] });
      return (r.cookies || []).map((c) => c.name);
    } catch (e) { return []; }
  };
  let cookies = await cookieNames();
  const t0 = Date.now();
  while (!(cookies.includes("wr_vid") && (cookies.includes("wr_skey") || cookies.includes("wr_rt")))) {
    if (Date.now() - t0 > 300000) throw new Error("login timeout: 请在 ego lite 中登录微信读书后重试");
    await new Promise((r) => setTimeout(r, 3000));
    cookies = await cookieNames();
  }

  // 唤出右侧自动隐藏的工具栏；若处于上下滚动模式则点击切换为水平双栏
  const dims0 = await page.evaluate(() => ({ w: window.innerWidth, h: window.innerHeight }));
  await page.mouse.move(dims0.w - 40, Math.round(dims0.h / 2), { label: "唤出工具栏" });
  await new Promise((r) => setTimeout(r, 600));
  let switched = false;
  const swBtn = await page.evaluate(() => {
    const b = document.querySelector("button.readerControls_item.isNormalReader");
    if (!b) return null;
    const r = b.getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: r.width };
  });
  if (swBtn && swBtn.w > 0) {
    await page.mouse.click(swBtn.x, swBtn.y, { label: "切换双栏翻页" });
    switched = true;
    await new Promise((r) => setTimeout(r, 3000));
  }

  const meta = await page.evaluate(() => { const info = (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.reader && window.__INITIAL_STATE__.reader.bookInfo) || {}; return { title: info.title || document.title.replace(' - 微信读书', '').trim(), author: info.author || '', cover: info.cover || '', intro: info.intro || '', publisher: info.publisher || '' }; });

  const catalogRaw = await page.evaluate(() => Array.from(document.querySelectorAll('.readerCatalog_list_item')).map((e) => e.innerText));
  const dims = await page.evaluate(() => ({ w: window.innerWidth, h: window.innerHeight }));
  return { spaceId: task.spaceId, switched, meta, catalogRaw, vw: dims.w, vh: dims.h };
};
try { console.log("RESULT:" + JSON.stringify(await main())); }
catch (e) { console.log("RESULT:" + JSON.stringify({ fatal: String((e && e.message) || e) })); }
""" % (json.dumps(EGO_HOOK), json.dumps(IMGS_JS), json.dumps(url))
    return ego_run(js, timeout=timeout)


def ego_navigate(space_id, nav_idx, timeout=90):
    """通过目录面板跳转到指定章节，然后清屏并取回首屏数据"""
    js = js_preamble(space_id) + """
const HOOK = %s;
const IMGS = %s;
""" % (json.dumps(EGO_HOOK), json.dumps(IMGS_JS)) + JS_WAIT_DRAIN + """
const main = async () => {
  // 丢弃跳转前累积的历史渲染（阅读器可能恢复到上次阅读位置）
  try { await page.evaluate(() => { if (typeof window.__wr_drain === "function") window.__wr_drain(); }); } catch (e) {}

  const dims = await page.evaluate(() => ({ w: window.innerWidth, h: window.innerHeight }));
  const itemsVisible = () => page.evaluate(() => { const el = document.querySelector(".readerCatalog_list_item"); return !!(el && el.getBoundingClientRect().width > 0); });

  // 打开目录面板：唤出工具栏 → 坐标点击目录按钮
  if (!(await itemsVisible())) {
    await page.mouse.move(dims.w - 40, Math.round(dims.h / 2), { label: "唤出工具栏" });
    await new Promise((r) => setTimeout(r, 600));
    const btn = await page.evaluate(() => { const b = document.querySelector("button.readerControls_item.catalog"); if (!b) return null; const r = b.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: r.width }; });
    if (!btn || btn.w === 0) throw new Error("catalog button not visible");
    await page.mouse.click(btn.x, btn.y, { label: "打开目录" });
    await new Promise((r) => setTimeout(r, 1500));
  }
  if (!(await itemsVisible())) throw new Error("catalog panel did not open");

  // 坐标点击目标章节条目（合成 el.click() 不触发微信读书绑在子元素上的处理器）
  const item = await page.evaluate((i) => { const el = document.querySelectorAll(".readerCatalog_list_item")[i]; if (!el) return null; el.scrollIntoView({ block: "center" }); const r = el.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: r.width }; }, %d);
  if (!item || item.w === 0) throw new Error("catalog item not clickable");
  await page.mouse.click(item.x, item.y, { label: "跳转章节" });
  await new Promise((r) => setTimeout(r, 2500));
  try { await page.keyboard.press("Escape"); } catch (e) {}
  await new Promise((r) => setTimeout(r, 800));
  try { await page.evaluate(HOOK); } catch (e) {}   // 幂等重挂护栏
  const data = await drain();
  return data;
};
try { console.log("RESULT:" + JSON.stringify(await main())); }
catch (e) { console.log("RESULT:" + JSON.stringify({ fatal: String((e && e.message) || e) })); }
""" % (nav_idx,)
    return ego_run(js, timeout=timeout)


def ego_turn_page(space_id, vw, vh, timeout=90):
    """翻到下一页并取回本页字符与插图数据"""
    js = js_preamble(space_id) + """
const HOOK = %s;
const IMGS = %s;
""" % (json.dumps(EGO_HOOK), json.dumps(IMGS_JS)) + JS_WAIT_DRAIN + """
const main = async () => {
  // 优先坐标点击页脚"下一页"按钮（weread 的键盘处理器不认合成键盘事件）
  let btn = await page.evaluate(() => {
    const b = document.querySelector(".renderTarget_pager_button_right");
    if (!b) return null;
    const r = b.getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: r.width };
  });
  if (btn && btn.w > 0) {
    await page.mouse.click(btn.x, btn.y, { label: "下一页" });
  } else {
    await page.mouse.click(%d, %d, { label: "聚焦阅读区" });
    await page.keyboard.press("ArrowRight");
  }
  return await drain();
};
try { console.log("RESULT:" + JSON.stringify(await main())); }
catch (e) { console.log("RESULT:" + JSON.stringify({ fatal: String((e && e.message) || e) })); }
""" % (int(vw / 2), int(vh / 2))
    return ego_run(js, timeout=timeout)


def ego_finish(space_id, timeout=60):
    js = js_preamble(space_id) + """
const main = async () => { await task.finish({ keep: [] }); return { done: true }; };
try { console.log("RESULT:" + JSON.stringify(await main())); }
catch (e) { console.log("RESULT:" + JSON.stringify({ fatal: String((e && e.message) || e) })); }
"""
    try:
        ego_run(js, timeout=timeout)
    except Exception as e:
        print(f"⚠️ 关闭 ego 任务空间失败(可忽略): {e}")


# ---------------------------------------------------------------- 主流程

def main(book_id_or_url, force=False):
    book_id = extract_book_id(book_id_or_url)
    if not book_id:
        print("❌ 错误: 未能解析出有效的微信读书书籍 ID 或链接")
        return False

    book_dir = os.path.join(BASE, "output", book_id + "_v2")
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    os.makedirs(md_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)

    state = get_crawl_state(book_dir)
    existing_mds = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    raw_pages = sorted(f for f in os.listdir(raw_dir) if f.startswith("page_") and f.endswith(".json"))

    if state.get("completed") and existing_mds and not force:
        print(f"📖 检测到书籍《{book_id}》此前已完整抓取（共 {len(existing_mds)} 章），跳过重复抓取。")
        download_all_images(raw_dir, img_dir)
        return True

    print(f"📖 目标书籍 ID: {book_id}")

    # 复用已有的 ego 任务空间（支持进程级断点续传），失效则自动新建
    space_arg = state.get("ego_space_id") or ("weread export " + book_id)
    print(f"🌐 正在通过 ego lite 浏览器打开书籍页面 ...", flush=True)
    try:
        setup = ego_setup(book_id, space_arg)
    except RuntimeError as e:
        if state.get("ego_space_id"):
            print(f"⚠️ 历史任务空间不可用（{e}），自动新建...", flush=True)
            setup = ego_setup(book_id, "weread export " + book_id)
        else:
            raise
    space_id = setup["spaceId"]
    vw, vh = setup.get("vw") or 1200, setup.get("vh") or 900

    book_meta = setup["meta"]
    book_meta["book_id"] = book_id
    with open(os.path.join(book_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(book_meta, f, ensure_ascii=False, indent=2)
    print(f"📚 书名: {book_meta.get('title')}")
    if book_meta.get("author"):
        print(f"✍️ 作者: {book_meta.get('author')}")
    if setup.get("switched"):
        print("🔄 已切换至双栏翻页模式")

    cover_url = book_meta.get("cover")
    if cover_url:
        try:
            req = urllib.request.Request(cover_url, headers={"Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=15).read()
            if len(raw) > 500:
                with open(os.path.join(book_dir, "cover.jpg"), "wb") as f:
                    f.write(raw)
                print("🖼️ 原版高清封面下载完成")
        except Exception as e:
            print(f"⚠️ 封面下载跳过: {e}")

    # 目录清洗（与原版一致）
    catalog = []
    for txt in setup.get("catalogRaw") or []:
        txt = re.sub(r"\s*当前读到.*$", "", txt.strip().replace("\n", " ")).strip()
        if txt:
            catalog.append(txt)
    n = len(catalog)
    with open(os.path.join(book_dir, "_catalog.json"), "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    print(f"📑 目录共 {n} 项: {catalog[:3]} ... {catalog[-2:] if n > 3 else ''}", flush=True)
    if n == 0:
        print("❌ 未读取到目录，退出")
        return False

    # 断点续传：匹配已完成章节
    existing_chap_files = sorted(f for f in os.listdir(raw_dir) if f.startswith("chap_") and f.endswith(".json"))
    completed_titles = []
    for cf in existing_chap_files:
        try:
            with open(os.path.join(raw_dir, cf), encoding="utf-8") as f:
                cdata = json.load(f)
                if cdata.get("title"):
                    completed_titles.append(cdata["title"])
        except Exception:
            pass

    resume_cat_idx = 0
    if completed_titles and catalog:
        last_done_norm = norm(completed_titles[-1])
        for ci, cat in enumerate(catalog):
            if norm(cat) == last_done_norm or last_done_norm.startswith(norm(cat)):
                resume_cat_idx = ci + 1
                break
        if resume_cat_idx >= n:
            print(f"🎉 目录所有 {n} 个章节此前均已全部抓取完成！")
            save_crawl_state(book_dir, {
                "book_id": book_id, "completed": True,
                "saved_chapter_count": len(completed_titles),
                "total_words": state.get("total_words", 0),
            })
            ego_finish(space_id)
            download_all_images(raw_dir, img_dir)
            return True

    if resume_cat_idx > 0:
        print("\n" + "=" * 55)
        print(f"🔄 检测到未完成进度，执行【真·断点续传】：")
        print(f"   - 历史已完成: {len(completed_titles)} 卷/章")
        print(f"   - 续跑跳转点: 目录第 {resume_cat_idx + 1}/{n} 项《{catalog[resume_cat_idx]}》")
        print("=" * 55 + "\n", flush=True)
    nav_idx = min(resume_cat_idx, n - 1)

    # 跳转至起始章节并取回首屏
    first = ego_navigate(space_id, nav_idx)
    chars, imgs = first.get("chars") or [], first.get("imgs") or []
    vw = first.get("vw") or vw
    mid = vw / 2

    cur_title = catalog[nav_idx] if nav_idx < len(catalog) else "扉页"
    seen_titles = set(completed_titles + [cur_title])
    saved_idx = len(completed_titles)
    buf = []
    stale = 0
    seen_last = False
    last_after_seen = 0
    last_cat = norm(catalog[-1]) if catalog else ""
    seen_page_sigs = set()
    seen_imgs = set()

    start_step = len(raw_pages) if resume_cat_idx > 0 else 0
    total_words_accum = state.get("total_words", 0) if resume_cat_idx > 0 else 0
    t_start = time.time()
    long_pause_at = next_long_pause_at(start_step)

    print(f"\n🚀 开始遍历抓取页面 (从第 {start_step} 页开始，翻页节奏与原版一致，全书预计需 1~3 小时)...", flush=True)

    def process_page(step, chars, imgs, got_any_hint=False):
        """与原版一致的页块处理：切页 → 行重组 → 章节匹配 → 实时落盘"""
        nonlocal buf, cur_title, seen_titles, saved_idx, seen_last
        nonlocal seen_page_sigs, seen_imgs, total_words_accum

        pages = split_pages(chars)
        left_imgs = [im for im in imgs if im["left"] < mid]
        right_imgs = [im for im in imgs if im["left"] >= mid]

        got_any = False
        step_words = 0
        page_blocks = []

        for side, pchars in pages:
            lines = page_lines(pchars)
            if not lines:
                continue
            sig = norm("".join(ln["text"] for ln in lines))
            if len(sig) > 8 and sig in seen_page_sigs:
                continue
            if len(sig) > 8:
                seen_page_sigs.add(sig)
            got_any = True

            for ln in lines:
                txt = ln["text"].strip()
                step_words += len(txt)
                matched_title, remainder = match_catalog_title(txt, catalog, seen_titles)

                is_new_chapter = False
                new_title = ""
                if matched_title and norm(matched_title) != norm(cur_title):
                    is_new_chapter = True
                    new_title = matched_title
                elif ln["cls"] == "title" and norm(txt) != norm(cur_title) and len(txt) <= 40:
                    is_new_chapter = True
                    new_title = txt

                if is_new_chapter:
                    if buf:
                        saved_idx += 1
                        body, im_list = render_md(cur_title, buf, saved_idx)
                        txt_len = len(re.sub(r"<[^>]+>", "", body))
                        with open(os.path.join(md_dir, "%04d.md" % saved_idx), "w", encoding="utf-8") as f:
                            f.write(body)
                        with open(os.path.join(raw_dir, "chap_%04d.json" % saved_idx), "w", encoding="utf-8") as f:
                            json.dump({"title": cur_title, "images": im_list, "text_len": txt_len}, f, ensure_ascii=False)
                        print(f"\n  💾 [章节实时落盘] [{saved_idx:02d}] 《{cur_title}》({txt_len} 字)", flush=True)
                        save_crawl_state(book_dir, {
                            "book_id": book_id, "completed": False, "last_step": step,
                            "saved_chapter_count": saved_idx, "last_title": cur_title,
                            "seen_titles": list(seen_titles), "total_words": total_words_accum,
                            "ego_space_id": space_id,
                        })
                    buf = []
                    cur_title = new_title
                    seen_titles.add(cur_title)
                    print(f"\n  📑 [第 {step:03d} 页] 识别新章节: 《{cur_title}》", flush=True)
                    if last_cat and norm(cur_title) == last_cat:
                        seen_last = True
                    if remainder:
                        rem_blk = {"type": "line", "cls": "js", "text": remainder}
                        buf.append(rem_blk)
                        page_blocks.append(rem_blk)
                    continue

                if buf and buf[-1]["type"] == "line" and buf[-1]["text"] == txt and buf[-1]["cls"] == ln["cls"]:
                    continue
                line_blk = {"type": "line", "cls": ln["cls"], "text": txt}
                buf.append(line_blk)
                page_blocks.append(line_blk)

            side_imgs = left_imgs if side == 0 else right_imgs
            for im in side_imgs:
                if im["src"] not in seen_imgs:
                    seen_imgs.add(im["src"])
                    img_blk = {"type": "img", "src": im["src"]}
                    buf.append(img_blk)
                    page_blocks.append(img_blk)
                    got_any = True

        if not pages and imgs:
            for im in imgs:
                if im["src"] not in seen_imgs:
                    seen_imgs.add(im["src"])
                    img_blk = {"type": "img", "src": im["src"]}
                    buf.append(img_blk)
                    page_blocks.append(img_blk)
                    got_any = True

        total_words_accum += step_words
        if got_any:
            with open(os.path.join(raw_dir, "page_%04d.json" % step), "w", encoding="utf-8") as pf:
                json.dump({
                    "step": step, "chapter": cur_title, "words": step_words,
                    "blocks": page_blocks,
                    "lines": [b["text"] for b in page_blocks if b.get("type") == "line"],
                }, pf, ensure_ascii=False)
        return got_any, step_words

    step = start_step
    max_steps = start_step + 5000
    while step < max_steps:
        if step > start_step:
            res = ego_turn_page(space_id, vw, vh)
            chars, imgs = res.get("chars") or [], res.get("imgs") or []
            delay = random.uniform(*PAGE_DELAY_RANGE)
            time.sleep(delay)
            if step >= long_pause_at:
                pause = random.uniform(*LONG_PAUSE_RANGE)
                print(f"  ⏸ 已抓取 {step} 页，触发温和长停顿 {pause:.0f} 秒...", flush=True)
                time.sleep(pause)
                long_pause_at = next_long_pause_at(step)
        # 首屏数据来自 ego_navigate，其余来自 ego_turn_page

        got_any, step_words = process_page(step, chars, imgs)

        elapsed_sec = int(time.time() - t_start)
        elapsed_str = f"{elapsed_sec // 60}分{elapsed_sec % 60}秒"
        if got_any:
            stale = 0
            print(f"  [进度 第 {step:03d} 页] 本页 +{step_words:3d} 字 | 累计 {total_words_accum:6d} 字 | 所在: 《{cur_title[:12]}》 | 已用时 {elapsed_str}", flush=True)
            if seen_last:
                last_after_seen += 1
                if last_after_seen >= 4:
                    print("  🏁 末章内容已抓取完成", flush=True)
                    break
        else:
            stale += 1
            if seen_last and stale >= 2:
                print("  🏁 已到达末章且无新内容，抓取完成", flush=True)
                break
            if stale >= 8:
                print(f"  🏁 连续 {stale} 页无新内容，判定全书抓取结束", flush=True)
                break
        step += 1

    if cur_title and buf:
        saved_idx += 1
        body, im_list = render_md(cur_title, buf, saved_idx)
        txt_len = len(re.sub(r"<[^>]+>", "", body))
        with open(os.path.join(md_dir, "%04d.md" % saved_idx), "w", encoding="utf-8") as f:
            f.write(body)
        with open(os.path.join(raw_dir, "chap_%04d.json" % saved_idx), "w", encoding="utf-8") as f:
            json.dump({"title": cur_title, "images": im_list, "text_len": txt_len}, f, ensure_ascii=False)
        print(f"\n  💾 [末章落盘] [{saved_idx:02d}] 《{cur_title}》({txt_len} 字)", flush=True)

    save_crawl_state(book_dir, {
        "book_id": book_id, "completed": True, "last_step": step,
        "saved_chapter_count": saved_idx, "last_title": cur_title,
        "seen_titles": list(seen_titles), "total_words": total_words_accum,
    })

    print("\n" + "=" * 50)
    print(f"🎉 抓取成功！全书已保存 {saved_idx} 个章节，累计 {total_words_accum} 字")
    print("=" * 50)
    ego_finish(space_id)
    download_all_images(raw_dir, img_dir)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python crawl_weread_ego.py <微信读书链接或ID> [--force]")
        sys.exit(1)
    force_flag = "--force" in sys.argv or "-f" in sys.argv
    bid = [a for a in sys.argv[1:] if not a.startswith("-")][0]
    ok = main(extract_book_id(bid), force=force_flag)
    sys.exit(0 if ok else 1)
