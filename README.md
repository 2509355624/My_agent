# 个人 Agent

最小化的本地 AI Agent，基于 Ollama + Python。

## 快速开始

```bash
# 1. 确认 Ollama 在运行
ollama serve

# 2. 拉模型（如果还没有）
ollama pull qwen2.5:7b

# 3. 运行 Agent
python agent.py
```

## 使用

```
你 > 现在几点了？
你 > 帮我画一个海边少女
你 > /clear    # 清空会话
你 > /quit     # 退出（自动保存）
```

## 添加新工具（Skill）

1. 在 `tools.py` 里写函数
2. 在 `tools.py` 的 TOOLS 列表里注册
3. 在 `agent.py` 的 SYSTEM_PROMPT 里更新工具说明

示例：添加一个天气查询工具

```python
# tools.py 里加函数
def get_weather(city):
    import requests
    resp = requests.get(f"https://wttr.in/{city}?format=3")
    return resp.text

# TOOLS 列表里加注册
{
    "name": "get_weather",
    "description": "查询天气",
    "function": get_weather,
    "parameters": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名"}
        },
        "required": ["city"]
    }
}
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `agent.py` | Agent Loop + CLI 交互 + 会话持久化 |
| `tools.py` | 工具注册表（在这里加新工具）|
| `session.jsonl` | 自动生成的会话记录 |

## 后续可以加什么

- [ ] 接入 ComfyUI API → 真正生图
- [ ] 接入 RAG 知识库 → 记忆检索
- [ ] 接入 GPT-SoVITS → 语音输出
- [ ] 多模态（LLaMA 看图）→ 图片质量判断
- [ ] 上下文窗口管理 → 长对话不爆
- [ ] Web UI → 不用终端操作
