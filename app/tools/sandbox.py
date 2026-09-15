"""skills 目录沙箱路径解析 —— read / write / list / delete 四处共用同一份规则。

为什么单独抽一个模块：
    历史上 read_file 和 write_file 各自实现了一份错误规则——把路径里的
    "/" 和 "\\" 直接删掉（见 git 历史）。结果是 skills 内部的子目录永远读不到：
    "human-writing/references/fiction.md" 会被拼成 "human-writingreferencesfiction.md"。
    而同一时期 delete_file 却写对了（相对路径 + 分隔符归一 + realpath 双重校验）。
    同一条安全规则被实现两遍、一遍对一遍错，所以收敛到这里，只留一份。

约定：
    - 一切路径都相对 SKILLS_DIR，可带任意层级子目录
    - 附带逻辑前缀的写法也接受："skills/a/b.md" 等价于 "a/b.md"
    - 拒绝父级跳转 (..)、拒绝逃出 SKILLS_DIR 的绝对路径
"""

import os
import re

from app.config import SKILLS_DIR

# 列目录时跳过的噪音目录：版本库 / 缓存 / 依赖
SKIP_DIR_NAMES = {"__pycache__", "node_modules", ".git", ".svn", ".idea", ".vscode"}

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def skills_root():
    """skills 目录的真实绝对路径（realpath，用于前缀判定与相对化）"""
    return os.path.realpath(SKILLS_DIR)


def to_rel(abs_path):
    """把沙箱内的绝对路径转回「相对 skills/ 的路径」，统一用 / 分隔。

    这个形式就是 read_file(path=...) 直接能用的形式，所以对外一律输出它。
    """
    rel = os.path.relpath(abs_path, skills_root())
    return rel.replace(os.sep, "/")


def is_noise_dir(name):
    """是否应跳过的目录：隐藏目录（.git / .idea）+ 缓存依赖目录"""
    return name.startswith(".") or name in SKIP_DIR_NAMES


def is_noise_file(name):
    """是否应跳过的文件：隐藏文件（.gitignore / .DS_Store）"""
    return name.startswith(".")


def resolve_in_skills(rel_path, allow_absolute=False):
    """把路径安全映射到 SKILLS_DIR 内，返回 (绝对路径, 错误信息)。

    参数:
      - rel_path: 相对 skills/ 的路径，如 "writing/01-structure/write-structure.md"
      - allow_absolute: 是否放行落在 skills 内的绝对路径。
                        只读类工具（read_file / list_files）设 True，
                        它们拿到的是用户或模型手抄的绝对路径，放行能少一轮试错；
                        删除类工具保持 False，维持「只认相对路径」的严格面。

    返回 (None, 错误信息) 表示非法；成功时错误信息为 None。
    """
    if rel_path is None:
        return None, "路径不能为空"

    raw = str(rel_path).strip()
    if not raw:
        return None, "路径不能为空"

    # 统一分隔符：模型和用户都可能混用 / 与 \
    norm = raw.replace("\\", "/")

    # 剥掉 "skills/" 逻辑前缀与 "./"，让 "skills/a/b.md" 等价 "a/b.md"
    while norm.startswith("./"):
        norm = norm[2:]
    if norm.lower() == "skills":
        norm = ""
    elif norm.lower().startswith("skills/"):
        norm = norm[len("skills/"):]

    root = skills_root()

    if os.path.isabs(norm) or _WIN_DRIVE_RE.match(norm):
        if not allow_absolute:
            return None, "路径非法：只接受相对 skills/ 的路径"
        candidate = os.path.normpath(norm)
    else:
        # 禁止父级跳转（绝对路径靠下面的 realpath 收敛判断兜底）
        if ".." in norm.split("/"):
            return None, "路径非法：禁止父级跳转 (..)"
        if norm == "":
            return root, None
        candidate = os.path.normpath(os.path.join(SKILLS_DIR, norm))

    real_candidate = os.path.realpath(candidate)

    # 必须严格落在 SKILLS_DIR 之内；用分隔符收尾，避免 skills_other 这类前缀干扰
    if not (real_candidate == root or real_candidate.startswith(root + os.path.sep)):
        return None, "路径非法：超出 skills 目录范围"

    return real_candidate, None
