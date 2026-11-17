# my_agent 重构基线

> 2026-11-17 建立。分支 `my_agent_restructure`，工作树 `D:\AI\my_agent_restructure`。
> 目标是先把结构变清楚，不改变现有行为；当前 `main` 分支继续可用。

## 一、现状数据

| 项目 | 数量 |
|---|---|
| 仓库内已跟踪文件 | 631 |
| `app/` Python 模块 | 116 个 / 70,159 行 |
| 测试文件 | 165 个 / 65,761 行 |
| 仓库根目录已跟踪 Python | 70 个，绝大多数是 `_probe_*`、`_verify_*`、`_tmp_*` 诊断脚本 |
| 当前工作树体积 | 旧目录约 18.7 GB（含模型、状态、日志）；新工作树约 107 MB（只含 git 跟踪文件） |
| 测试基线 | 全量 `unittest discover` 约 155 条既有红项，不能把“0 红”当目标 |

## 二、耦合热点

按 `app/` 内部 import 统计：

| 模块 | 被多少个模块 import |
|---|---|
| `config` | 83 |
| `qq_api` | 24 |
| `llm` | 22 |
| `image_jobs` | 21 |
| `tools` | 21 |
| `agents` | 15 |
| `skills` | 15 |

单个模块的依赖数量：

| 模块 | 内部 import 数 |
|---|---|
| `main` | 43 |
| `qq_bot` | 32 |
| `direct_gen` | 22 |
| `image_jobs` | 19 |
| `persona` | 14 |
| `comic_api` | 13 |
| `persona_api` | 13 |
| `comic` | 12 |
| `gen_api` | 12 |

结论：不是“文件放得乱”这么简单，`config`、`qq_api`、`llm`、`image_jobs` 是事实上的全局单例，直接搬文件会把半个系统一起拽动。

## 三、难度判断

| 做法 | 难度 | 预计工作量 | 风险 |
|---|---|---|---|
| 只整理目录、临时脚本、测试命名 | 低 | 2–4 天 | 低 |
| 建 `app/core` / `app/infra` 骨架，旧路径做兼容层 | 中 | 1–2 周 | 中 |
| 拆 `config` / `qq_api` / `llm` / `image_jobs` 的全局耦合 | 高 | 3–5 周 | 高，最容易出隐性回归 |
| 拆 `main` / `qq_bot` / `direct_gen` / `persona` | 高 | 4–8 周 | 高，入口和路由多 |
| 测试按模块重整、补契约测试 | 中高 | 3–4 周 | 中，165 个文件互相依赖共享临时目录 |
| 推倒重写整个系统 | 很高 | 3–6 个月 | 极高，不建议 |

更现实的总量：**分阶段重构约 2–4 个月（按每天能投入的时长浮动）**，不是一两周能“清爽”的工程。先把边界和兼容层做出来，收益最快。

## 四、推荐方案：绞杀者式重构，不推倒重写

原则：

1. 每次只搬一个模块，旧 import 路径保留兼容层。
2. 行为不变，URL 不变，`web/` 不动。
3. 每个模块单独提交，提交前跑同一组测试并对比红项清单。
4. 不在重构分支里顺手改业务逻辑。

目标结构（先骨架，后搬家）：

```text
app/
  core/          # config、logsetup、cancel、usage、路径
  infra/         # comfy、llm、qq_api、image_jobs、存储
  domain/        # persona、comic、novel、gallery、card
  features/      # 各功能的 service / api
  main.py        # 只做装配，不写业务
tests/
  unit/
  integration/
  web/
  legacy/        # 暂时保留旧测试，跑通后再迁
tools/
  archive/       # 根目录诊断脚本、一次性 probe
```

### 阶段

**阶段 0：基线（已完成）**
- 新分支 + 独立工作树，旧分支不受影响。
- 新工作树冒烟：84 条手机端测试通过。
- 记录全量测试红项，后续只对比差异。

**阶段 1：卫生清理（低风险）**
- 根目录 70 个诊断脚本先分类：仍在用的留在 `tools/`，一次性的进 `tools/archive/`。
- `tests/` 先不物理搬家，只加分组说明和运行脚本。
- 清理重复备份 `.bak-*`、`*_before_*`、`web/showcase_*` 样张，确认无引用再处理。

**阶段 2：核心骨架 + 兼容层（中风险）**
- 先建 `app/core/config.py`，原 `app/config.py` 改成一行 re-export。
- 同样处理 `logsetup`、`cancel`、`usage`。
- 83 个 import `config` 的模块一个都不改，先让兼容层生效。

**阶段 3：拆基础设施（高风险）**
- 顺序：`llm` → `qq_api` → `image_jobs` → `tools/skills`。
- 每个模块先抽接口，再搬家，旧模块保留薄壳。
- 每步跑对应测试 + 一组跨模块冒烟（生成、对话、漫画、画廊）。

**阶段 4：拆入口与业务大模块**
- `main.py` 按路由/功能拆 blueprint 或子应用，`main` 只装配。
- `qq_bot` 按消息类型拆 handler。
- `direct_gen`、`persona` 按“生成/记忆/计费/出图”拆服务。
- 这一步才动业务文件，前面阶段不要提前做。

**阶段 5：测试与清理**
- 旧测试保持可运行，新增测试按模块放。
- 兼容层没有调用者后删除。
- 最后再统一命名、目录和文档。

## 五、运行注意事项

新工作树不含被 `.gitignore` 忽略的运行数据。需要联调时，从旧目录复制这些**本机私有**内容，但不要提交：

- `.env`
- `state/`
- `agents/`
- `skills/`
- `data/`
- `config/` 里被忽略的私有配置
- `models/`（体积大，按需软链接或复制）

## 六、对应 Skill

- 代码重构流程用 `coding-playbook`：走“重构 → TDD → verification”这条，不靠即兴改。
- 架构分析用 `coding-playbook/references/repo-qa-evidence.md`，本次数据就是按它的证据口径统计的。
- 目前没有专门的“Python 单体架构重构”Skill；`impeccable` / `unslop` / `modern-web-guidance` 都只管前端视觉，不管后端架构。若以后要固化本项目重构规范，可用 `skill-creator-for-work` 写一个仓库专用 Skill。

## 七、下一步（建议按顺序）

1. 在新分支跑一次全量测试，保存红项清单作为基线。
2. 只做阶段 1：根目录脚本分类，不动 `app/`。
3. 建 `app/core` 和 `config` 兼容层，先验证 83 个调用点不受影响。
4. 再决定是否继续拆 `llm` / `image_jobs`。
