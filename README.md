# 📚 WeRead Book Exporter (`weread-book-exporter`)

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/Engine-Playwright-2EAD33.svg)](https://playwright.dev/)
[![ego lite](https://img.shields.io/badge/Engine-ego%20lite-6E7BFF.svg)](https://lite.ego.app/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

将微信读书 Web 端已购或会员书籍**无损抓取全书全文**，并一键导出为 **精排 PDF**、**标准 EPUB** 以及 **纯 Markdown 笔记（适配 Obsidian / Notion）**。

自动保留原书的**目录结构、标题层级、引文/重点段落样式与高清插图**，提供舒适的大字号双字体排版，方便个人深度学习、全文检索与知识库归档。

<p align="center">
  <img src="assets/preview.png" alt="WeRead Book Exporter 排版预览" width="100%">
</p>

---

## ✨ 核心特性

### 1. 100% 全书完整抓取（攻克网页端翻页漏字问题）
许多传统的微信读书抓取脚本在默认的上下滚动模式下运行，受限于网页端超长虚拟滚动容器的分片机制，翻页时极易跳过章节下半部分内容。
- **自动双栏翻页模式**：自动检测并切换至水平双栏模式，逐页完整遍历全书；
- **精准页面重组**：在底层捕获 Canvas 绘制指令与分栏坐标，精准还原每一页的文本流与段落顺序，杜绝跨页丢字、重复或错乱。

### 2. 原生级层次还原与断句修复
- **多级样式识别**：根据画布渲染的字号与字重，自动区分**章节标题、独立引文/核心段落、加粗小标题与普通正文**；
- **智能断句拼接**：自动修复因页面折行、翻页或插图穿插导致的句子中断，生成连贯自然的自然段；
- **插图自动下载**：自动提取并下载原书全部高清配图，按阅读顺序内嵌至对应位置。

### 3. 一键导出三大常用格式
| 导出格式 | 适用场景 | 特性说明 |
| :--- | :--- | :--- |
| **精排 PDF (`.pdf`)** | iPad 批注、电脑阅读、打印 | **苹方正文 + 楷体引文**大字号排版，内置封面、可点击目录页、PDF 侧边栏书签大纲 (Outline) 与页码 |
| **标准 EPUB (`.epub`)** | Apple Books / Kindle / 阅读器 | 内嵌多字体排版样式、完整目录导航树与原书高清插图 |
| **Markdown (`.md`)** | Obsidian / Notion / 个人知识库 | 提供**纯 Markdown 版**（引文自动转为 `>` 引用块）与**带样式标签版**，方便检索与做笔记 |

### 4. 温和抓取与防频控机制
- **随机波动翻页**：翻页间隔延长至 8～20 秒动态随机等待，告别机械固定的秒级请求；
- **分段长停顿保护**：每抓取 10～20 页自动触发 1～2 分钟长停顿，大幅降低服务端接口压力，确保大体量长篇书籍能够稳定平滑跑完全程。

---

## 🚀 快速开始 · 方式一：Playwright 驱动（弹出窗口，扫码一次）

### 1. 安装依赖（国内极速安装，仅需几秒）

```bash
# 克隆仓库
git clone https://github.com/WANGXIN-567/weread-book-exporter.git
cd weread-book-exporter

# 使用国内高速镜像安装依赖（推荐腾讯云或阿里云，稳定不报 403）
pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple/
# 备用阿里云镜像：
# pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

> ⚡ **免下载 200MB 浏览器**：代码内置了智能浏览器检测，会自动直接调用电脑自带的 **Microsoft Edge** 或 **Google Chrome**；如果系统未安装上述浏览器，可执行 `playwright install chromium` 补充下载。

### 2. 一键抓取并导出全格式电子书（核心推荐）

直接复制微信读书网页版的完整链接（支持任意已购/会员书籍）：

```bash
# 直接粘贴整条网页链接（或书籍 ID 均可）：
python main.py https://weread.qq.com/web/reader/3b132980813ab94eeg013129
```

> **提示**：
> - 首次运行时会自动弹出浏览器窗口，使用微信扫码登录一次即可。
> - 脚本会自动完成「全书章节遍历 ➔ 原版高清封面下载 ➔ 插图下载 ➔ 精排 PDF 渲染 ➔ 双字体 EPUB 导出 ➔ Markdown 笔记生成」全流程！
> - 所有文件最终保存在 `output/` 目录下。

*(高级用法：如需分步运行，也可单独执行 `python crawl_weread.py <链接或ID>` 与 `python export_book.py <链接或ID>`)*

---

## 🤖 方式二：使用 ego lite 浏览器抓取（免弹窗 · 免扫码 · 后台静默）

[ego lite](https://lite.ego.app/) 是一款面向 AI Agent 的 Chromium 浏览器。本仓库提供 **Playwright 流程的等价替代驱动 `crawl_weread_ego.py`**，直接复用 ego lite 里已登录的微信读书会话，全程不弹窗、不扫码、不抢占你的鼠标和标签页，适合后台批量导出。

### 1. 前置条件

- 安装并打开 [ego lite](https://lite.ego.app/)（首次启动会自动把 `ego-browser` CLI 装到 `~/.local/bin/ego-browser`）；
- **在 ego lite 中登录一次微信读书**（之后凭证常驻浏览器，无需重复登录）；
- 本仓库依赖照常安装（`pip install -r requirements.txt`，无需 Playwright 浏览器）。

### 2. 抓取 → 修正 → 导出

```bash
# ① 全书抓取（翻页节奏、断点续传、逐页落盘与 Playwright 版完全一致）
python crawl_weread_ego.py https://weread.qq.com/web/reader/<book_id>

# ② 后处理：章节按官方目录重排 + 元数据修正（简介/出版社/ISBN）
python postprocess_ego.py <book_id>

# ③ 导出三件套（PDF / EPUB / Markdown）
python export_book.py <book_id>
```

### 3. 实现要点（适配 ego-browser 的坑位经验）

| 微信读书网页行为 | ego 驱动的应对 |
| :--- | :--- |
| 点击处理器绑在子元素上，容器合成 `el.click()` 无效 | `evaluate` 读取元素矩形 → **坐标级 `mouse.click`** |
| 右侧工具栏悬停才显示 | 先 `mouse.move` 唤出，再取坐标点击 |
| 合成 `ArrowRight` 键盘事件不翻页 | 改点页脚 **`.renderTarget_pager_button_right`** 按钮 |
| Canvas 挂钩需在页面脚本前注入 | `cdp("Page.addScriptToEvaluateOnNewDocument")` + **幂等护栏**防重复挂钩 |
| 阅读器可能恢复上次阅读位置 | 跳转章节前**清空字符缓冲**，杜绝混入旧位置内容 |

> 断点续传与 Playwright 版共用同一套 `output/<book_id>_v2/` 落盘结构，两套驱动可交替使用；任务空间 ID 记录在 `_state.json` 的 `ego_space_id`，中断后重跑自动续传。

---

## 🛠️ 工作流程

```mermaid
flowchart LR
    A["微信读书网页版\n(自动切换双栏翻页)"] --> B["底层文本与插图捕获\n逐页提取字号/字重/坐标"]
    B --> C["段落重组与清洗\n合并跨行断句 & 去重"]
    C --> D["多格式导出引擎\nPDF / EPUB / Markdown"]
```

---

## ⚠️ 免责声明 (Disclaimer)

1. 本工具仅用于个人学习、读书笔记整理与前端渲染技术研究。
2. 请支持正版阅读，仅对您个人合法拥有阅读权限的书籍使用本工具，导出的文件请勿用于任何商业用途或公开传播。
3. 本仓库仅包含通用转换脚本，不包含任何受版权保护的书籍内容或个人登录信息。

---

如果这个工具对你的阅读与笔记整理有帮助，欢迎点亮一颗 **Star ⭐** 支持！
