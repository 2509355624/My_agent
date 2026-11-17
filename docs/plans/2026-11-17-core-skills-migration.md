# 2026-11-17 core：技能加载器迁移

## 目标

`app/skills.py` → `app/core/skills.py`

`skills.py` 只做 `skills/<name>/` 的目录定位、`SKILL.md`/`skill.md` 读取、清单与
优先级，不判断任何业务，属于跨域共享基础设施。

## 调用方

`agents.py`、`comic.py`、`direct_gen.py`、`friend_api.py`、`image_jobs.py`、
`main.py`、`tools/normal/*`、`features/chat/agent_prompt.py`、
`features/comic/{moss_api,portrait,render_opts,routes,story}.py`、
`features/image/{lora_slots,outfit_api,routes}.py`；测试约 12 个文件。

## 验证

`compileall`（app + tests）；`test_skills.py` 25/25（4 skip）、
`test_gen_params.py` 40/40（4 skip）、`test_comic.py` 112 条中 3 failures
（缺 `skills/` 渠道数据，基线）、`test_lora_slots.py` 28 条中 13 failures + 6 errors
（同基线）。
