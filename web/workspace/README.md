# workspace — 全新界面

V3 聊天优先界面的独立功能文件夹。当前有两块：登录页、聊天页壳。画廊/漫画/个人页在
`js/views.js` 里只有空壳，内容按 P1/P2 往里填。

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
| `js/api.js` | 同源 JSON 请求（`get`/`post`）+ 登录接口 |
| `js/login.js` | 两步流程、QQ/验证码校验、60s 倒计时、`sessionStorage` 暂存 QQ |
| `js/gen.js` | 生图接口：渠道 / 提交 / 状态 / 取图 / 默认画风 |
| `js/chat.js` | 消息渲染、折叠、复制、重画、轮询 |
| `js/stack.js` | 页面栈 push/back、左滑退回、画风抽屉 |
| `js/views.js` | 子页壳（左 X、中标题、右「我的」），内容位留空 |

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

## 校验规则（与后端一致）

- QQ 号：`^[1-9][0-9]{4,11}$`（5–12 位，不以 0 开头）
- 验证码：6 位数字，60s 后可重发
- QQ 号存 `sessionStorage`（本标签页有效），验证通过后 `location.reload()` 由服务器重判落点

## 测试

```
D:\AI\confyui_env\Scripts\python.exe tests/test_workspace_login.py
D:\AI\confyui_env\Scripts\python.exe tests/test_workspace_chat.py
```
