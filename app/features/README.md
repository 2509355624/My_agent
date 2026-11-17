# app/features

按业务分 feature，不按 `views/services/utils` 三个大口袋分。

| feature | 负责 | 不负责 |
|---|---|---|
| `chat` | 对话循环、提示词、记忆、模型路由 | 网关、静态页、图片落盘 |
| `image` | 生图、渠道、队列、审核、LoRA | 漫画分镜、QQ 消息 |
| `comic` | 漫画、编剧、渲染、额度、作品 | 单张生图渠道 |
| `persona` | 角色扮演、计费、记忆、查看台 | 通用对话循环 |
| `rag` | 文档解析、切片、检索、重排 | 页面样式、QQ 推送 |
| `novel` | 小说模式、世界书、裁定链 | 通用对话循环 |
| `card` | 角色卡生成、封面、五框 | 生图渠道实现 |
| `qq` | QQ 事件、账单、群友页内核 | 5174 页面路由 |
| `admin` | 用量、账本、预设、配置、后台 | 进程启动、网关反代 |

## 一个 feature 的默认文件

```text
routes.py       HTTP：解析参数、鉴权、调 service、返回 JSON
service.py      业务：不 import Flask，不碰 request/jsonify
repository.py   存储：只读写，不判断业务
jobs.py         后台：轮询、落盘、通知
models.py       DTO/dataclass
README.md       边界、状态文件、外部依赖、怎么测
```

不是每个都要建。先有 `routes + service`，需要落盘再抽 `repository`，需要后台再抽 `jobs`。

## 依赖规则

- `routes → service → repository/integrations`，反向 import 是 blocker。
- feature 之间不直接 import 内部文件；跨 feature 只走对方 `service.py` 的公开函数。
- 每个 feature 建好后补自己的 README，AI 只读这十几行，不读几万行实现。
