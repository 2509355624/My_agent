# app/integrations

只包外部系统，不写业务策略。

```text
comfy/    ComfyUI 提交、轮询、输出、状态
llm/      模型 provider、降级链、流式
qq/       OneBot 协议、事件字段
volc/     火山/豆包适配
```

约束：

- `integrations` 不判断“什么时候该出图/该回复”；那是 `service` 的事。
- 外部错误转成明确的领域错误，不静默吞掉。
- 不把上游原始响应直接透给页面；错误响应不带堆栈、内部路径、密钥。
- 网络调用可注入超时/重试；测试用 fake，不连真实服务。

状态：空包，未接线。现有 `comfy_*`、`llm.py`、`vision.py`、`nai.py`、`qq_api.py` 后续按调用方拆入。
