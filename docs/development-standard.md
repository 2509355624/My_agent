# 开发规范 v1.0

> 状态：**已确认，生效**（2026-11-17）。
> 已确认：Flask + 模块化单体；Django 暂不接；硬闸门启用；函数/文件行数上限启用。
> 目的不是“多写一份文档”，而是让 AI 和人都按同一套可检查的规则开发，避免再次出现 God 文件、文字墙页面和互相 import 的乱麻。

---

## 0. 文档怎么分工

不要把所有规则塞进一个巨大的 `开发规范.md`，AI 每次读不完，也不会照做。建议四层：

| 文件 | 给谁看 | 长度 | 作用 |
|---|---|---|---|
| `AGENTS.md`（根目录） | AI 每次开工先读 | 60–100 行 | 硬闸门、常用命令、目录边界、禁止事项 |
| `docs/development-standard.md`（本文） | 人 + AI 按需查 | 可长 | 完整规范、规则编号、审查清单 |
| `docs/architecture.md` | 人 + AI 按需查 | 可长 | 目标架构、依赖方向、迁移顺序 |
| `docs/adr/NNNN-*.md` | 人 | 1–2 页 | 重要决策记录：为什么选 Flask、为什么暂不换 Django、数据库怎么接 |

`README.md` 继续给人看，不承担 AI 约束。

---

## 1. 项目事实（规范的前提）

- 现有系统是 Flask + 原生 HTML/CSS/JS，无构建步骤。
- 116 个 `app/` 模块、约 70,159 行；165 个测试文件、约 65,761 行；全量测试约 155 条既有红项。
- 三个独立进程：5174 主站、5175 群友网关、5176 管理网关；另有 QQ 机器人和看门狗。
- 当前最大问题不是“框架选错”，是**边界不清**：`config` 被 83 个模块 import，`main.py` 89 条路由，`persona.py` 441 KB。
- 因此规范的第一目标是：**阻止新的 God 文件和新的大杂烩页面**，同时允许老代码分阶段变干净。

### 参考项目结论

- `baseProject`：适合看工程骨架——Django 项目配置、核心 app、工具目录、部署脚本齐全。但它把配置放在单个 `settings.py`，`djBase` 仍是工具大口袋。
- `dj-xiyou`：适合看真实业务规模下的痛点——`goku` 183 个 py，`views2.py` 68 KB，`models.py` 64 KB，`viewsIM.py` 36 KB。**这正是我们要避免的形态。**
- 结论：借 `baseProject` 的骨架和 `dj-tianqi` 的 settings/services/repositories 分层；不复制 `dj-xiyou` 的单体 app。

---

## 2. 七条工程原则

1. **方案先行**：没写清目标、边界、影响面、验证方式，不写代码。
2. **最小改动**：只碰任务需要的文件；不顺手重构、不改无关格式。
3. **模块优先**：按业务功能分，不按 `views/services/utils` 三个大口袋分。
4. **测试先行**：新行为先写失败测试；修 bug 先写复现测试。
5. **证据交付**：没跑过的命令不说通过；没验证的页面不说完成。
6. **根因修复**：不静默吞异常、不造假成功路径、不用 `try/except: pass` 掩盖。
7. **简单可读优先**：能 3 行说清不要写 30 行；不提前抽象、不堆设计模式；命名直白，嵌套浅，一个函数一个目的。代码首先是给人看的，其次才是给机器跑的。

---

## 3. 标准工作流（八个闸门）

| 闸门 | 产出 | 通过条件 |
|---|---|---|
| G1 需求复述 | 5 行以内：目标 / 非目标 / 用户场景 / 影响模块 / 验收方式 | 用户确认，或歧义已消 |
| G2 方案卡 | `docs/plans/<日期>-<主题>.md`（结构性改动必须） | 有备选方案、取舍、回滚点 |
| G3 测试设计 | 要新增/修改的测试清单 | 每条测试能对应一个行为 |
| G4 分模块实现 | 一次只改一个模块/一个 feature | diff 可控，旧路径兼容 |
| G5 编译串联 | `compileall` + import smoke | 入口能 import，无语法错 |
| G6 回归 | 定向测试 + 全量对比红项 | 无“新增红项” |
| G7 真机/人工验收 | 手机或本机实际打开页面/接口 | 用户确认，或明确记录未验 |
| G8 提交交付 | commit + 交付报告 | 做了什么 / 遗留什么 / 设计偏差 |

### 方案卡模板

```markdown
# <主题>

## 目标
## 非目标
## 现状与影响面（文件/模块/接口）
## 方案 A / B（推荐哪个，为什么）
## 数据与兼容性（URL、返回结构、旧 import）
## 测试计划
## 回滚方式
## 需要用户确认的点
```

### 规模判断

- 只改 1 个文件、行为不变：聊天里说清即可，不写方案卡。
- 改 2–5 个文件或涉及接口/数据：方案卡 + 定向测试。
- 改跨 feature、入口、配置、数据库：方案卡 + ADR + 回归清单。

---

## 4. AI 协作协议

### 开工前

- 先读 `AGENTS.md`，再读本次涉及的 feature README / 现有实现。
- 用 5 行复述：目标、非目标、影响面、验证方式、最大风险。
- 有歧义先问，**一次最多问 1 个真正阻塞的问题**；能自己查证的不问。
- 不把“我觉得”当事实；路径、命令、接口形状以代码为准。

### 开工中

- 每完成一个模块，给一句进度：改了什么、验证了什么、下一步。
- 不跨模块“顺手优化”；发现无关问题记下来，不当下改。
- 同一方法连续失败 2 次，停止重试，换方案或报告阻塞。
- 不重启常驻进程、不点 `.bat`、不 `taskkill`；改完只做静态/只读验证，重启由站主手动做。

### 交付时

必须覆盖三项：

1. 做了什么（文件、行为、接口）。
2. 遗留什么（未验证、未迁移、已知风险）。
3. 设计偏差（如果有，为什么）。

禁止只说“完成了”“应该没问题”“测试都过了”而不给命令和输出。

---

## 5. 模块化规则

规则编号供 review 引用。

- **ARCH-01**：按 feature 分目录，不按技术层分大口袋。目标形态：
  ```text
  app/features/image/
  ├── routes.py
  ├── service.py
  ├── repository.py
  ├── jobs.py
  └── models.py   # 需要时才有
  ```
- **ARCH-02**：依赖方向固定 `routes → service → repository/integrations`；反向 import 是 blocker。
- **ARCH-03**：`routes.py` 只做 HTTP：解析参数、鉴权、调 service、返回 JSON；不写业务分支和文件读写。
- **ARCH-04**：`service.py` 不 import Flask，不碰 `request` / `jsonify` / `session`；方便单测和以后换框架。
- **ARCH-05**：`repository.py` 只做取/存，不判断业务规则；JSON 和 SQL 两套实现跑同一组 contract test。
- **ARCH-06**：`integrations/` 只包外部系统（ComfyUI、LLM、QQ、火山），不写“什么时候该出图”。
- **ARCH-07**：feature 之间不直接 import 内部文件；跨 feature 只走对方 `service.py` 的公开函数。
- **ARCH-08**：共享代码只有在**第二个调用方出现后**才抽到 `core/`；不要为“以后可能用”提前抽象。
- **ARCH-09**：不建万能 `utils/`。路径、日志、安全、错误放 `core/`；业务工具放对应 feature。
- **ARCH-10**：不新增 God 文件。新文件 600 行开始规划拆分；1,000 行是**硬警戒线**，不是切菜线；1,000–1,200 行只有单一职责且已有拆分计划时允许；1,500 行是硬 blocker（生成代码/迁移文件除外）。
- **ARCH-10.1**：拆文件按“一个职责/一个用例”拆，不按行数机械切。比如 `image/service.py` 超限，拆成 `image/service_generate.py`、`image/service_audit.py`，不拆成 `part1.py`、`part2.py`；更不允许为了 78 行拆出 `config2.py`。
- **ARCH-10.2**：老 God 文件（`persona.py`、`main.py` 等）冻结增长：新职责一律放新 feature；下一次修改必须至少抽出一块，不能只加不减。
- **ARCH-10.3**：文件行数只是信号，不是目标。一个 1,100 行但只做“配置”的文件，仍然要按配置域拆；一个 500 行但混了 HTTP、业务、存储的文件，也必须拆。
- **ARCH-11**：老文件（`persona.py`、`main.py` 等）不要求一次拆完；但**不允许继续往里面加新职责**。新职责放新 feature。
- **ARCH-12**：模块变包是首选兼容手段：`app/persona.py` → `app/features/persona/__init__.py`，先原样 re-export，调用方不用改。
- **ARCH-13**：配置按职能拆，不做一个万能 `config.py`。目标形态是 `app/core/config/` 包：
  `paths.py`（路径）、`env.py`（环境变量解析）、`llm.py`（模型/provider/降级链）、
  `image.py`（ComfyUI/渠道/额度）、`rag.py`（RAG/向量库）、`web.py`（端口/页面/画廊）、
  `qq.py`（QQ）、`persona.py`（角色扮演模型）、`runtime.py`（上下文/看门狗/通知）。
  每个模块只放自己域内的常量与函数，跨域引用走 `paths` 或 `env`。
- **ARCH-14**：提示词统一放 `app/prompts/` 主文件夹，按“feature + 用例”分子目录和文件，
  不做一个 `prompts.py` 大字符串。例：`prompts/comic/story.py`（编剧）、
  `prompts/comic/panel.py`（分镜）、`prompts/comic/character.py`（角色一致性）；
  `prompts/image/generate.py`、`prompts/image/audit.py`；
  `prompts/persona/roleplay.py`、`prompts/persona/memory.py`、`prompts/persona/opening.py`。
  一个文件只负责一个场景，改漫画编剧不会碰到生图审核。

---

## 6. Python 代码规则

- **PY-01**：函数 ≤ 50 行，圈复杂度 ≤ 10，嵌套 ≤ 4。新代码硬约束；老代码逐步。
- **PY-02**：一个函数只做一件事；参数 > 5 个时用 dataclass / config 对象，不堆位置参数。
- **PY-03**：公共函数写类型注解；内部函数可选，但跨模块调用必须有。
- **PY-04**：禁止 `from x import *`；禁止可变默认参数（`def f(x=[])`）。
- **PY-05**：禁止裸 `except:`、`except Exception: pass`、静默吞错。异常要带上下文，或明确转成业务错误。
- **PY-06**：循环嵌套 ≤ 2 层；3 层必须重构或写豁免。**不按“函数里有几个 for”做硬指标**——4 个顺序循环但逻辑简单是可以的；真正的问题是嵌套和复杂度。
- **PY-07**：路径用 `pathlib.Path`；新代码不得硬编码 `D:\AI\agent_my_test` 这类机器路径，统一走 `core/config`。
- **PY-08**：时间用带时区的时间；金额用 `Decimal`，不用 `float` 算钱；对外 ID 用字符串。
- **PY-09**：日志带上下文（job_id、路径、模块），禁止打印 token、密码、cookie、完整手机号。
- **PY-10**：注释写“为什么”，不写“这行在做 what”；复杂分支必须有一句原因。
- **PY-11**：不新增全局可变单例；需要共享状态时走 app config 或显式注入。
- **PY-12**：请求线程里不 `time.sleep` 等长任务；生图/漫画/抓取放 worker 或后台线程。
- **PY-13**：新依赖先问。标准库能解决的不加包；一个包只为一个函数加的，不批准。
- **PY-14**：命名：文件/函数 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE`；测试名写行为，不写 `test1`。
- **PY-15**：简单可读优先。能用早返回就少套 `else`；能用 1 个函数就不建 3 层基类；一个抽象只有一个调用方时，先不抽。

---

## 7. 测试规则

- **TEST-01**：新行为先写失败测试（RED），再写最小实现（GREEN）；没看到失败，不算 TDD。
- **TEST-02**：修 bug 必须先写复现测试；修完测试通过，再提交。
- **TEST-03**：测试必须确定性：不依赖执行顺序、不共享真实 `state/`、不写真实 `data/`。
- **TEST-04**：单测不连真实网络、LLM、ComfyUI、QQ；这些放 `integration/`，用 fake 或临时目录。
- **TEST-05**：每个测试自己准备临时目录，结束清理；不依赖上一个测试留下的文件。
- **TEST-06**：页面/接口改动的契约测试要保留守卫字符串（如 `friendNote`、`id="lsScript"`、`<title>`）。
- **TEST-07**：全量测试不追求 0 红；以“改前红项清单”为基线，**新增红项为 0** 才算过。
- **TEST-08**：新增/修改代码的行覆盖率目标 ≥ 80%；不要求全仓覆盖率，不为覆盖率写空断言。
- **TEST-09**：测试命名：`test_<行为>_<条件>_<预期>`；一个测试只断言一个行为。
- **TEST-10**：UI 改动至少测一个手机宽度；最终真机验收由用户做，AI 不能代替。

---

## 8. 审查规则（Review）

### 严重级别

| 级别 | 含义 | 处理 |
|---|---|---|
| Blocker | 安全、数据丢失、接口破坏、新增红项、跨层依赖、无测试的新行为 | 必须改，不能合并 |
| Should fix | 重复代码、复杂度过高、错误处理缺失、God 文件继续变大 | 本次或下一个 commit 改 |
| Nit | 命名、格式、注释 | 可后置，不阻塞 |

### 必查清单

- **REV-01**：diff 是否只包含本任务？有没有顺手改无关文件？
- **REV-02**：新增行为有没有测试？测试是否先失败过？
- **REV-03**：`routes → service → repository` 方向有没有被打破？
- **REV-04**：有没有新增全局单例、循环 import、硬编码路径？
- **REV-05**：错误路径是否明确？失败会不会被静默吞掉？
- **REV-06**：鉴权/输入校验/路径穿越/SSRF 有没有新增口子？
- **REV-07**：长任务是否阻塞请求线程？有没有无界队列/内存增长？
- **REV-08**：日志/异常里有没有 secret、token、cookie、手机号？
- **REV-09**：旧 URL、旧 import、旧返回结构是否仍兼容？
- **REV-10**：文档/AGENTS/README 是否同步？没同步要写清原因。

### diff 预算

- 目标：单次审查 ≤ 300 行有效改动；超过 500 行必须拆 commit 或说明为什么不能拆。
- 纯移动/重命名/生成文件不计入有效行数。
- 一个 commit 只做一件事；重构和行为修改不混在一起。

---

## 9. 安全规则

- **SEC-01**：仓库是 public。绝不提交 `.env`、token、cookie、密码、`state/`、`agents/`、`skills/`、聊天记录。
- **SEC-02**：所有外部输入（query/body/header/文件名）必须校验；文件名防路径穿越。
- **SEC-03**：管理接口、删除接口、公网接口必须有鉴权或令牌；新增接口要登记限流桶。
- **SEC-04**：服务端请求外部 URL 要防 SSRF；不允许用户直接指定任意内网地址。
- **SEC-05**：SQL 必须参数化；不上字符串拼接。数据库迁移必须可回滚。
- **SEC-06**：错误响应不返回堆栈、内部路径、密钥、上游原始响应。

---

## 10. 前端/UI 规则

- **UI-01**：手机优先；默认按 360–430px 宽度先能用，再考虑桌面。
- **UI-02**：一个页面一个主操作；不要一排按钮堆给用户。
- **UI-03**：首屏不放说明书。用户点开就知道下一步；长说明移到折叠区或删掉。
- **UI-04**：不新引 CSS 框架/组件库；继续原生 HTML/CSS/JS，保持无构建。
- **UI-05**：改 HTML/CSS/客户端 JS 前先查 `modern-web-guidance`，用平台原生能力，不写上一代 polyfill。
- **UI-06**：不用紫色光晕、玻璃拟态堆叠、emoji 导航、全大写标签；颜色和层级要克制。
- **UI-07**：每个页面只保留一个返回路径；不要让用户猜“回主页还是回上一级”。

---

## 11. Git 与提交

- **GIT-01**：分支：`refactor/<阶段>-<主题>`；不直接在 `main` 改。
- **GIT-02**：commit 类型：`feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:`。
- **GIT-03**：一个 commit 一个意图；移动文件和改行为分开。
- **GIT-04**：提交前看 `git status` 和 `git diff --check`；不 `git add -A` 把未跟踪垃圾一起带走。
- **GIT-05**：不 `push --force`、不 `reset --hard`、不 `clean` 清场；需要时先说明再等确认。
- **GIT-06**：提交信息不写数据组织方式、账号、内网地址；public 仓库按最小暴露原则。
- **GIT-07**：重构分支只在 `D:\AI\my_agent_restructure` 做；旧目录和 `main` 不动。

---

## 12. 文档与决策

- **DOC-01**：根 `AGENTS.md` ≤ 100 行，只放硬规则、命令、边界、链接；详细内容放 `docs/`。
- **DOC-02**：结构性改动写 `docs/plans/<日期>-<主题>.md`；完成后保留，作为决策痕迹。
- **DOC-03**：框架/数据库/目录边界这类决策写 ADR，一篇只记一个决定，包含被否方案。
- **DOC-04**：测试基线写 `docs/tests/baseline-<日期>.md`：命令、红项数、红项清单。
- **DOC-05**：feature README 只写“这个功能边界、外部依赖、状态文件、怎么测”，不复制代码。
- **DOC-06**：文档和代码同一个 commit 更新；不同步就在交付报告里说明。

---

## 13. Definition of Done

一个任务只有同时满足以下条件，才能说“完成”：

- [ ] 改动范围与任务一致，没有无关重构。
- [ ] 新增/修改行为有测试，且测试先失败过。
- [ ] 定向测试通过；全量测试无新增红项。
- [ ] `compileall` / import smoke 通过。
- [ ] 旧 URL、旧 import、旧返回结构仍兼容（或已明确记录破坏）。
- [ ] 安全清单过：鉴权、输入、路径、secret、日志。
- [ ] 文档/README/AGENTS 同步，或记录未同步原因。
- [ ] diff 已自审，没有调试代码、临时打印、注释掉的死代码。
- [ ] 交付报告包含：做了什么 / 遗留什么 / 设计偏差。

---

## 14. 例外机制

规则不是铁板，但不能 silent 违反。

- 必须破例时，在代码处写一行：
  ```python
  # standard-exception: <规则编号> <原因> <到期/条件>
  ```
- 在 commit message 或方案卡里记录，review 时明确同意。
- `tools/legacy/`、迁移文件、生成代码、第三方 vendor 不受代码行数/复杂度规则约束，但不得被业务代码 import。

---

## 15. 已确认的决策（2026-11-17）

1. **框架**：确认继续 Flask + 模块化单体；Django 暂不接，未来只考虑独立管理面。
2. **硬闸门**：确认跨层 import、无测试的新行为、新增红项、secret、破坏旧 URL 都是 blocker。
3. **代码上限**：确认函数 50 行、嵌套 4 层、新文件 600 行开始规划拆分；
   1,000 行硬警戒、1,000–1,200 行可容忍、1,500 行硬 blocker；按职能拆，不按行数切。
4. **循环规则**：确认不按“3 个 for”做硬指标，改为“嵌套 ≤ 2 层、圈复杂度 ≤ 10”。
5. **覆盖率**：确认新增/修改行 ≥ 80%，不追求全仓覆盖率。
6. **diff 预算**：确认单次审查 ≤ 300 行；超过 500 行必须拆分或说明。
7. **文档形态**：确认 `AGENTS.md`（短）+ `docs/development-standard.md`（全）+ `docs/adr/`（决策）。
8. **验收方式**：确认 AI 只做静态/自动验证，页面最终由用户在手机上点一遍；AI 不代替真机验收。

后续规则变更走 ADR 或在本文件追加“变更记录”，不静默降低闸门。

---

## 16. 参考来源

这些是规则来源，不是运行依赖；不把整仓 clone 进项目，只把适用条款内化到本文件和 `AGENTS.md`。

- GitHub Blog, *How to write a great AGENTS.md*：边界、命令、Never/Ask/Always 三层
  <https://github.blog/ai-and-ml/github-copilot/how-to-write-a-great-agents-md-lessons-from-over-2500-repositories/>
- GitHub `awesome-copilot`：通用 code review checklist（安全/测试/性能/架构）
  <https://github.com/github/awesome-copilot>
- Microsoft `skills`：`AGENTS.md` 生成规范（build/test/structure/boundaries）
  <https://github.com/microsoft/skills>
- `cskwork/coding-agent-rules`：函数 ≤50 行、嵌套 ≤4、按 feature 拆、外科手术式改动
  <https://github.com/cskwork/coding-agent-rules>
- Google Chrome `modern-web-guidance`：前端平台能力与 fallback
  <https://github.com/GoogleChrome/modern-web-guidance>
