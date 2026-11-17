# 2026-11-17 `chat` feature：联网搜索迁移

## 目标

`app/search_agent.py` → `app/features/chat/search_agent.py`

联网搜索是对话工具，不服务生图/漫画。

## 调用方

`app/direct_gen.py`；测试 `test_search_agent.py`、`test_direct_gen.py`。

## 验证

`compileall`（app + tests）；`test_search_agent.py` 51/51。
