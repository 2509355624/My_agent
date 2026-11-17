# app/workers

负责后台任务与看门狗，不处理 HTTP。

```text
image_worker.py   生图队列、ComfyUI 轮询、落盘、通知
comic_worker.py   漫画渲染、交付
watchdog.py       常驻进程守护
```

约束：

- 不在请求线程里跑长任务。
- 队列有界；任务有超时、取消、失败落账。
- 不 import Flask；需要业务用例时调用对应 `service.py`。
- AI 不重启常驻进程、不点 `.bat`、不 `taskkill`；重启由站主手动做。

状态：`watchdog.py`（原 `app/watchdog.py`，SnowLuma/ComfyUI 常驻守护）已迁入；
启动命令改为 `python -m app.workers.watchdog`。`image_worker.py`、`comic_worker.py`
仍是后续拆分目标。
