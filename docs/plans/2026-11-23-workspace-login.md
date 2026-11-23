# 全新界面第一块：登录页（2026-11-23）

## 目标

在 `web/workspace/` 建一个**独立功能文件夹**，作为全新 V3 聊天优先界面的入口。本轮只做登录页：两步流程（输入 QQ 号 → 下一步跳到验证码页 → 获取验证码 → 登录），豆包风白色卡片，可切深色。

## 非目标

- 不做聊天页、画廊、漫画、画风抽屉（后续分期）。
- 不改登录后端（`/login/send`、`/login/verify` 现成，在 5175）。
- 不改 5174/5175 路由、不碰任何常驻服务。
- 不做头像昵称、短信/邮箱登录、注册。

## 现状与影响面

- 登录接口只在 **5175**（`friend_app.py`）：`/login/send`、`/login/verify`、`/me`；cookie 为 `friend_auth.COOKIE_NAME`。
- 5174 把 `web/` 挂成静态根，但**没有**这些登录路由 → 新页的 fetch 必须同源，即页面要挂在 5175 下才能真登录。
- 静态页在 5175 用 `send_from_directory(WEB_DIR, ...)` 单页发出；无通用静态目录转发。
- 因此本轮交付：一套纯静态、可独立打开的登录页文件；**接入路由留到 P0 整壳时统一做**（那时一次性给 `/workspace` 加页面路由 + 同源 fetch）。

## 方案 A / B

- **A（推荐）**：`web/workspace/` 下按职责拆 `index.html` + `css/`（tokens/base/login）+ `js/`（api/login/ui）；纯静态、无构建、可双击预览 UI，接口调用集中在一个 `api.js`。
- B：单文件 HTML 全塞进去 → 违反「不写屎山」和 ARCH-10（文件行数），否。

选 A。理由：规范 G2/ARCH-01/UI-04 都要求按职责分、不引框架；登录逻辑与接口封装分离，后续聊天页可直接复用 `api.js`。

## 文件结构

```
web/workspace/
  index.html          登录页结构（两个 section：qq / code）
  css/tokens.css      设计变量：明暗主题、颜色、圆角、间距
  css/base.css        重置 + 手机居中容器
  css/login.css       登录页样式
  js/api.js           登录接口封装（同源相对路径）
  js/login.js         两步流程 + 倒计时 + 校验 + 调 api
  README.md           模块边界 / 怎么预览 / 怎么接路由
tests/test_workspace_login.py   静态结构守卫
docs/plans/2026-11-23-workspace-login.md（本文）
```

## 数据与兼容性

- 接口路径与返回结构完全复用现有：`POST /login/send {qq}` → `{ok,message,error}`；`POST /login/verify {qq,code}` → `{ok,error}` + Set-Cookie。
- QQ 校验同后端：`^[1-9][0-9]{4,11}$`；验证码 6 位数字；倒计时 60s。
- 不影响任何旧 URL、旧页面、旧 cookie；新文件夹独立。

## 测试计划

静态守卫（HTMLParser + 文本断言，沿用 `test_*_mobile.py` 风格）：
1. 三 CSS + 两 JS + README 都存在，单文件 < 600 行。
2. 两个步骤 section 都在，按钮文案正确（下一步 / 获取验证码 / 登录）。
3. 接口只用同源相对路径（`/login/send`、`/login/verify`），无硬编码域名。
4. 明暗主题变量存在，默认白、可切深色。
5. 手机宽度视口 meta + 单主按钮。
6. JS `node --check` 通过。

## 回滚方式

删除 `web/workspace/` 与测试文件即可；不触碰任何现有文件与路由，零回归面。

## 需要用户确认的点

无阻塞项。登录接口挂载（5175 加 `/workspace` 路由）留待 P0 整壳时统一处理，本轮先交付可独立预览的界面。
