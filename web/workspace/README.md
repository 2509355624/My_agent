# workspace — 全新界面

V3 聊天优先界面的独立功能文件夹。当前已接：登录页、聊天页壳、画廊（AI 创作）、
我的漫画作品、创作面板（图片生成 / 漫画生成）、我的（作品 / 收藏）。

## 模块边界

| 文件 | 职责 |
| --- | --- |
| `index.html` | 登录页，两个步骤 section（QQ 号 → 验证码），一步一个主按钮 |
| `chat.html` | 聊天页，只有消息区 + 输入栏（无顶栏、无底栏） |
| `css/tokens.css` | 颜色 / 玻璃层 / 三档阴影 / 滚动条变量，默认白色（不跟随系统），可手动切深色并记住 |
| `css/base.css` | 重置 + 手机优先居中容器 + 背景光晕 + 细滚动条 |
| `css/login.css` | 登录页样式 |
| `css/chat.css` | 气泡、AI 卡片、悬浮玻璃输入栏、chip |
| `css/sheet.css` | 页面栈视图 + 底部玻璃抽屉 |
| `css/gallery.css` | 画廊：文件夹标签条、两列墙、骨架屏、全屏查看器 |
| `css/comic.css` | 我的漫画作品：作品列表、整本翻页、缩略条、底部动作 |
| `css/create.css` | 创作面板：「图片生成 / 漫画生成」两个入口、漫画模式标记、结果卡缩略格 |
| `css/me.css` | 我的：两个 tab、两列作品/收藏卡片、空状态 |
| `js/api.js` | 同源 JSON 请求（`get`/`post`）+ 登录接口 |
| `js/login.js` | 两步流程、QQ/验证码校验、60s 倒计时、`sessionStorage` 暂存 QQ |
| `js/gen.js` | 生图接口：渠道 / 提交 / 状态 / 取图 / 默认画风 |
| `js/chat.js` | 消息渲染、折叠、复制、重画、轮询；导出通用卡片壳 `WSChat.card()` |
| `js/stack.js` | 页面栈 push/replace/back、左滑退回、画风抽屉 |
| `js/views.js` | 子页壳（左 X、中段、右「我的」）+ 子页注册口，`push` 可带 `opts` |
| `js/gallery.js` | 画廊：列表 / 分类 / 翻页 / 查看器（收藏、下载） |
| `js/comic.js` | 我的漫画作品：列表 / 整本翻页 / 重画 / 加气泡 / 下载 |
| `js/create.js` | 创作面板：模式切换 + 漫画生成全链路（编剧 → 开画 → 逐页出图） |
| `js/me.js` | 我的：作品 / 收藏两个 tab，点开复用画廊查看器 |

依赖方向：页面 → `chat.js`/`stack.js` → `gen.js` → `api.js`。
`api.js` 不碰 DOM，`chat.js` 不拼接口地址，`views.js` 不认识接口。

## 怎么预览

- **聊天页（推荐）**：`http://localhost:5174/workspace/chat.html`
  5174 的 `static_folder` 就是 `web/`，所以静态页与 `/api/gen/*` 同源，聊天页能真出图。
- **登录页**：双击 `index.html` 能看两步切换；**真收验证码必须挂 5175**（`/login/*` 只在那边）。

5175 还没有 `/workspace` 路由，接入时加一条：

```python
@app.route("/workspace")
def workspace_page():
    return send_from_directory(WEB_DIR, "workspace/index.html")

@app.route("/workspace/chat")
def workspace_chat():
    return send_from_directory(WEB_DIR, "workspace/chat.html")
```

## 子页怎么写

`views.js` 只负责壳：左 ✕、中段、右「我的」。某个子页想自己接管中段或内容，
在它的模块里注册即可：

```js
WSViews.register("gallery", { top: fn(中段节点), mount: fn(内容区节点) });
```

没注册的子页还是默认样子（中段标题 + 一行占位）。注意 `sheet.css` 里
`.vtop > button` 只钉两端那两个圆按钮，中段换成标签条时不会再被压成 38px。

## 视觉约定

- **白色玻璃风**：`--glass / --glass-2` 是半透明底，配 `backdrop-filter: blur()` 才有层次；
  单独用半透明会显脏。`--hair` 是细边框（比 `--line` 淡），`--shadow-1/2/3` 是贴边/悬浮/浮层三档。
- **输入栏是浮层**：`.composer` 绝对定位在底部，消息从它下面滚过去，模糊才看得出来。
  消息区底部靠 `--composer-h` 留白，这个值由 `chat.js` 的 `fitComposer()` 按输入栏实际高度写回，
  输入框长高、旋转屏幕时会重算。改输入栏高度不用手动同步 padding。
- **滚动条全站收细**（`base.css` 里的 `*::-webkit-scrollbar`）；输入框内高按「一行文字 + 内边距 + 边框」
  精确给到 44px，不留 1~2px 溢出，否则会一直冒出原生灰条。
- **层级顺序**：`chat.css` 在 `sheet.css` 之前加载，`.view-root` 的透明底必须写在 `sheet.css`
  （否则被 `.view` 的实色底盖掉，整页会白成一片）。

## 聊天页的几个约定

- **输入上限 20000 字**（`maxlength`），超过 1000 字的用户消息默认折叠成 300 字 + 「展开全部」。
- **排队**：提交后立刻插一张卡，每 2s 轮询 `/api/gen/status`，用 `queue_depth` 显示「前面还有 N 人」。
- **轮询有上限**：连续失败 5 次就停，卡片标「状态断了」，不无限打接口。
- **重画**＝重新提交同一条提示词，换种子（非 NAI 渠道每次提交都随机 seed），表现为新发一张卡。
- **画风**只设默认渠道（`localStorage["ws_skill"]`），不影响「渠道 + 需求」这种点名生图方式。
- **渠道中文名**取自 `/api/gen/channels` 的 `label`；`label` 缺失时退回 id，并在右侧显示
  `summary` 后半句当一行说明。想让抽屉显示中文名，去 `skills/<id>/skill.md` 补 `label`。

## 创作面板（输入栏「＋」）

两个入口是**平级**的，选谁只决定「这句话拿去干什么」：

- **图片生成**（默认）：`chat.js` 直接 `POST /api/gen/submit`，一回一图。
- **漫画生成**：选完之后输入栏上方出现蓝色「漫画生成」标记（点 ✕ 退出），
  发送的那句话会被当成剧情 brief，交给 `create.js` 走完整条漫画链：
  `POST /api/comic/script` → 轮询 `script_status` → 剧本好了**直接** `POST /api/comic/start`
  → 轮询 `status`，卡片里每画好一页就补一格 3 列缩略图 → 全部出来了给「打开整本 / 下载全部」。

约定：

- **不插「看剧本 / 点确认」这一步**。用户跑漫画只看结果（2026-11-23 原话），
  剧本在后台落盘但不摆在界面上；想看剧本去旧漫画台。
- 一话默认 10 页（`PANELS`），与漫画台默认值一致；页数选择器这一期不做。
- 卡片的缩略格按 9:16 **完整显示**（`object-fit: contain`），不裁边，和大图同一个构图。
- 轮询靠 `setTimeout` 链 + `MAX_FAIL` / `IDLE_MAX` 双保险收手；页面退出不影响后台任务，
  结果在「我的漫画作品」里还在。

## 「我的」页

子页右上角的人形按钮进（栈里 `replace`，✕ 回聊天页）。只有两个 tab，没有头像昵称，
也没有点赞（2026-11-23 定）：

- **作品** → `/api/gal/mine`，两列作品集卡片，点开进画廊查看器。
- **收藏** → `/api/gal/favs`，按单张图排，点开直接定位到收藏的那张。
  各来源的 `idx` 基数不一致（磁盘从 0、上传从 1），所以查看器按 **item id** 找位置，
  不用 `idx`。
- 两页都滑到底自动加页（24 条一页），空的时候给引导文案，不摆破图。

`WSGallery.open()` 里会**懒建**查看器：从漫画页等直接 push「我的」时没逛过画廊，
不补这一步点卡片会完全没反应。

## 校验规则（与后端一致）

- QQ 号：`^[1-9][0-9]{4,11}$`（5–12 位，不以 0 开头）
- 验证码：6 位数字，60s 后可重发
- QQ 号存 `sessionStorage`（本标签页有效），验证通过后 `location.reload()` 由服务器重判落点

## 测试

```
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_workspace_*.py"
```

`tests/` 没有 `__init__.py`，只能走 `discover` 或整文件路径，不要 `-m unittest tests.xxx`。
