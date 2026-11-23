# workspace — 全新界面（登录页起步）

V3 聊天优先界面的独立功能文件夹。本模块当前只有登录页，后续聊天/画廊/漫画按子模块逐步加进来。

## 模块边界

- `index.html`：登录页结构，两个步骤 section（QQ 号 → 验证码），一步一个主按钮。
- `css/tokens.css`：明暗主题设计变量，默认白、跟随系统、可手动切并记住。
- `css/base.css`：重置 + 手机优先居中容器（桌面收成一张卡）。
- `css/login.css`：登录页样式。
- `js/api.js`：登录接口封装（同源相对路径 + cookie）。
- `js/login.js`：两步流程、QQ/验证码校验、60s 倒计时、`sessionStorage` 暂存 QQ。

依赖方向：`index.html → js/login.js → js/api.js`。`api.js` 不碰 DOM，`login.js` 不拼接口地址。

## 怎么预览

纯静态，直接双击 `index.html` 就能看 UI 和两步切换。**但真登录需要接口**：页面必须挂在 5175 下。

## 接口与挂载

登录接口只在 **5175**：
- `POST /login/send {qq}` → `{ok, message, error}`
- `POST /login/verify {qq, code}` → `{ok, error}` + Set-Cookie
- cookie 由 5175 签发，前端 fetch 用同源相对路径 + `credentials: same-origin`。

**当前 5175 还没有 `/workspace` 页面路由**，本轮按规范不改常驻服务。接入时（P0 整壳）在 `friend_app.py` 加一条路由，把 `web/workspace/index.html` 发出去即可，例如：

```python
@app.route("/workspace")
def workspace_page():
    return send_from_directory(WEB_DIR, "workspace/index.html")
```

5174 也能用静态根直接开 `/workspace/index.html`，但那里没有 `/login/*`，登录会 404 —— 所以正式入口走 5175。

## 校验规则（与后端一致）

- QQ 号：`^[1-9][0-9]{4,11}$`（5–12 位，不以 0 开头）
- 验证码：6 位数字，60s 后可重发
- QQ 号存 `sessionStorage`（本标签页有效），验证通过后 `location.reload()` 由服务器重判落点

## 测试

```
D:\AI\confyui_env\Scripts\python.exe tests/test_workspace_login.py
```
