# AGENTS.md — my_agent_restructure

## 项目

本仓库是个人 Agent 平台的重构分支：Flask + 原生 HTML/CSS/JS，无构建步骤。
只在这里工作：`D:\AI\my_agent_restructure`，分支 `my_agent_restructure`。
旧目录 `D:\AI\agent_my_test` 和 `main` 分支不动。

## 开工前先读

1. `docs/development-standard.md`：完整开发规范（v1.0，已生效）。
2. `docs/architecture.md`：目标架构与迁移顺序。
3. 本次涉及的 `app/features/<x>/README.md` 和现有实现。

## 常用命令

```powershell
# Python（不在 PATH）
D:\AI\confyui_env\Scripts\python.exe

# 全量测试（有既有红项，只对比“新增红项”）
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests

# 手机端冒烟
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_*mobile.py"

# 结构守卫
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_repo_layout.py"

# 编译
D:\AI\confyui_env\Scripts\python.exe -m compileall -q app tools
```

Git 不在 PATH：`D:\APP\git\Git\cmd\git.exe`。

## 目录契约

```text
app/
  bootstrap.py      # create_app("main" / "admin" / "friend")
  core/             # config / paths / logging / security / errors
  features/         # 按业务分：chat image comic persona rag novel card qq admin
  gateways/         # 5175 群友网关、5176 管理网关
  integrations/     # comfy / llm / qq / volc
  persistence/      # json_store / job_ledger / db / migrations
  workers/          # 生图、漫画、看门狗
  web/              # 页面路由与静态挂载
web/                # 现有 HTML 原样保留
tests/              # unit / integration / contract / e2e
tools/              # legacy / generators / ops
docs/               # 规范、架构、ADR、plan、测试基线
```

一个 feature 的默认文件：`routes.py`（HTTP）、`service.py`（业务）、
`repository.py`（存储）、`jobs.py`（后台任务）、`models.py`（DTO）、`README.md`。
不是每个都要建；先有 routes + service，需要再抽。

依赖方向：`routes → service → repository/integrations`。反向 import 是 blocker。
`service.py` 不 import Flask，不碰 request/jsonify。

## 硬规则

- 方案先行：结构性改动先写 `docs/plans/<日期>-<主题>.md`。
- 新行为先写失败测试；修 bug 先写复现测试。
- 函数 ≤ 50 行、嵌套 ≤ 4、圈复杂度 ≤ 10；循环嵌套 ≤ 2 层。
- 新文件 600 行开始拆，1,000 行 blocker；拆按职责/用例，不按 part1/part2。
- 老 God 文件冻结增长：新职责放新 feature；下次修改至少抽出一块。
- 不新增万能 `utils/`；共享代码等第二个调用方出现再抽。
- 不静默吞异常；不 `except Exception: pass`；不写假成功路径。
- 不提交 `.env`、token、cookie、密码、`state/`、`agents/`、`skills/`、聊天记录。
- 不 `git add -A` 盲加；不 `reset --hard` / `clean` / `push --force`。
- 不重启常驻进程、不点 `.bat`、不 `taskkill`；重启由站主手动做。
- 不做无关重构；不顺手改格式；发现无关问题只记录。

## 工作流

需求复述（目标/非目标/影响面/验收）→ 方案卡 → 测试设计 → 分模块实现 →
编译串联（compileall + import smoke）→ 回归（定向 + 全量对比新增红项）→
真机验收（用户做）→ 提交交付。

## 交付

必须写清：做了什么 / 遗留什么 / 设计偏差。
没跑过的命令不说通过；没验证的页面不说完成。
真机验收由用户在手机上完成，AI 不代替。
