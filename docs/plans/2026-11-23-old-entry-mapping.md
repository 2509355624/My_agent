# 旧入口 → 新界面（web/workspace）迁移清单

> 2026-11-23 排查。目的：新界面接手画廊/漫画/跑图后，把旧入口的跳转集中改掉。
> 本清单只记录现状与建议去向，**不代表已改**。

## 1. 页面路由现状

### 5174（主服务，`web/` 是静态根）

| 路由 | 发出文件 | 模块 | 归类 |
| --- | --- | --- | --- |
| `/gallery` | `gallery.html` | 画廊 | 画廊 |
| `/comic` | `comic_studio.html` | 漫画工作台（站主） | 漫画 |
| `/moss` | `moss_studio.html` | moss 漫画台 | 漫画 |
| `/gen` | `gen_pad.html` | 跑图台 | 跑图 |
| `/comic_admin` | `comic_admin.html` | 漫画台管理 | 漫画（管理） |
| `/comic_works` | `comic_works.html` | 漫画作品 | 漫画 |
| `/persona` | `persona.html` | 角色扮演 | 角色 |
| `/novel` | `novel.html` | 小说 | 不整合 |

定义位置：`app/web/pages.py`；`/gen` 在 `app/features/image/routes.py:383`。

### 5175（公网入口，登录在这一层）

| 路由 | 发出文件 | 说明 | 归类 |
| --- | --- | --- | --- |
| `/gallery`、`/gallery.html` | `gallery.html` | 未登录先发 `friend.html` 登录页 | 画廊 |
| `/comic`、`/comic.html` | `comic_studio_friend.html` | 群友版漫画台，**必须登录** | 漫画 |
| `/runbench`、`/runbench.html` | `runbench.html` | 令牌制跑图台（QQ 命令回的链接） | 跑图 |
| `/persona` | `persona.html` | 角色扮演 | 角色 |
| `/novel` | `novel.html` | 小说 | 不整合 |

5175 **没有** `/moss`、`/gen`——这两个只在 5174。

## 2. 页面内跳转指令（要改的就是这些）

### 首页 `web/index.html`

| 行 | 现有 | 指向 | 建议改成 |
| --- | --- | --- | --- |
| 1506 | `window.open('/comic','_blank')` | 漫画工作台 | 新界面漫画模块 |
| 1507 | `window.open('/moss','_blank')` | moss 漫画台 | 新界面漫画模块（切 moss 风格） |
| 1508 | `window.open('/gen','_blank')` | 跑图台 | 新界面创作/聊天出图 |
| 1509 | `window.open('/gallery','_blank')` | 画廊 | 新界面画廊 |

### 画廊 `web/gallery.html`

| 行 | 现有 | 指向 | 建议改成 |
| --- | --- | --- | --- |
| 379 | `<a href="/comic">漫画工作台</a>` | 漫画工作台 | 新界面漫画模块（或去掉，漫画从创作面板进） |

### 漫画工作台 `web/comic_studio.html`

| 行 | 现有 | 指向 | 建议改成 |
| --- | --- | --- | --- |
| 136 | `<a href="/comic_admin">群友管理 →</a>` | 漫画台管理 | 保持不变（管理侧） |
| 137 | `<a href="/gen">🎨 跑图台</a>` | 跑图台 | 新界面创作/聊天出图 |

### 漫画管理 `web/comic_admin.html`

| 行 | 现有 | 指向 | 建议改成 |
| --- | --- | --- | --- |
| 113 | `<a href="/comic_works">作品</a>` | 漫画作品 | 新界面「我的漫画作品」 |

### 管理页 `web/admin.html`

| 行 | 现有 | 指向 | 建议改成 |
| --- | --- | --- | --- |
| 509 | `<a href="/comic_admin">漫画台管理 →</a>` | 漫画台管理 | 保持不变（管理侧） |

### 角色扮演 `web/persona.html`

| 行 | 现有 | 指向 | 建议改成 |
| --- | --- | --- | --- |
| 1265 | `<a id="btnGallery" href="/gallery">` | 画廊 | 新界面画廊 |
| 2039 | `location.href = "/gallery" + q` | 画廊（带参数） | 新界面画廊 |
| 2043 | `location.protocol + "//" + h + ":5175/gallery" + q` | 5175 画廊探测 | 新界面画廊 |
| 2045/2048 | `location.href = "/" + q` | 根路径兜底 | 按新入口调整 |

## 3. 新界面要补的

- 5175 加页面路由：`/workspace` → `web/workspace/index.html`（登录页）。
  未登录发登录页，登录后发聊天页（聊天页待做）。
- 5174 是否需要 `/workspace`：本地开发可直接 `/workspace/index.html`，但登录接口不在 5174，正式入口走 5175。

## 4. 改的时候注意

- 5174 与 5175 是两套页面路由，同一路径可能发不同文件（`/comic` 在 5175 是群友版）。
- 首页 4 个按钮目前都是 `window.open(...,'_blank')` 新标签打开；新界面若是单页 App，应改为同页跳转或页面栈，不再开新标签。
- `persona.html` 的 `/gallery` 跳转带了一段「先探 5174 再探 5175」的兜底逻辑，改入口时整段一起处理，别只改 href。
