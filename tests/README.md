# 测试说明

## 运行环境

- Python：`D:\AI\confyui_env\Scripts\python.exe`
- 测试框架：标准库 `unittest`，不使用 pytest。
- 工作目录：仓库根目录。

## 全量跑

```powershell
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests
```

注意：当前 `main` 有约 **155 条既有红项**。重构分支不把“全量 0 红”作为完成标准，而是：

1. 改前保存一份红项清单。
2. 改后再跑同一命令。
3. 只对比“新增红项”；新增红项必须修掉。

## 常用局部测试

```powershell
# 页面手机端
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_*mobile.py"

# 公网网关 / 登录 / 漫画路由
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_friend_proxy.py"
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_comic_friend.py"

# 仓库结构守卫
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_repo_layout.py"
```

## 分组

| 前缀 | 内容 |
|---|---|
| `test_*mobile.py` | 各页面手机布局与首屏结构 |
| `test_friend*` | 5175 网关、登录、公网路由 |
| `test_comic*` | 漫画台、群友版、作品页、额度 |
| `test_persona*` | 角色页、角色管理、计费 |
| `test_image*` / `test_direct*` | 生图渠道与直连 |
| `test_repo_layout.py` | 根目录只允许入口/运维脚本，临时脚本必须在 `tools/` |

## 约定

- 新测试放 `tests/`，文件名 `test_<模块>.py`。
- 测试用临时目录，不写真实 `state/`、`data/`、`logs/`。
- 测试不要依赖执行顺序；每个用例自己准备数据。
- 不要为了让测试变绿而删旧测试；先确认旧测试是否真的过期。
