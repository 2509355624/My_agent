# Linux 真机验收记录（2026-11-17）

环境：云主机 `vm`，Linux，Python 3.12.11，git 2.34.1。

## 已验证

1. 从 GitHub 拉取 `my_agent_restructure` 分支（`92ec517`）。
2. `python3 -m compileall -q app` 通过。
3. 建立 `.venv`，安装 `requirements.txt` 通过。
4. `import app.main` 通过，路由数 292。
5. 用 Flask `test_client` 冒烟以下入口，全部 200：
   `/`、`/admin`、`/status`、`/comic`、`/moss`、`/novel`、`/persona`、
   `/review`、`/migrate`、`/api/status`、`/api/agents`、`/api/documents`、
   `/api/characters`。
6. 关键测试：
   - `test_admin_app.py` 11/11
   - `test_migrate_api.py` 20/20
   - `test_image_migrate.py` 40/40
   - `test_image_review.py` 44/44（修复后）
   - `test_sessions_admin.py` 103 条中 2 failures，与 Windows 基线一致

## 真机发现并修复的问题

Linux 上同一毫秒连续写两条二审记录会生成相同的 `RV-...` 编号，
JSONL 后写覆盖先写，导致全局列表少一条。已在
`app/features/image/review.py` 改为“已用编号集合 + 锁”去重，
保持对外编号格式不变。提交 `5483786`。

## 尚未做

- 没有在虚拟机里启动常驻 Flask/QQ/ComfyUI 进程；启动仍由站主手动执行。
- 没有做公网隧道、NapCat 扫码、ComfyUI 真实出图验收。
- 没有写 Linux 一键启动/更新脚本；下一步按实际进程拓扑补 `scripts/linux/`。
