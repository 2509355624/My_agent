"""删除 Skill 目录内的文件或子目录（强沙箱约束）

安全设计（代码层面，不依赖 AI 自觉）：
1. 仅允许操作 skills/ 目录内的相对路径
2. 用 pathlib.resolve + startswith 双重校验，确保最终路径绝不跳出 SKILLS_DIR
3. 绝对禁止解析结果等于 SKILLS_DIR 本身（skills 主目录不可删）
4. 过滤路径穿越（..、/、\\ 前缀）
"""

import os
import shutil
from app.config import SKILLS_DIR


def _resolve_within_sandbox(rel_path):
    """把相对路径解析到 SKILLS_DIR 内，返回 (最终绝对路径, 错误信息)。

    - rel_path 形如 "cyberpunk_style"，或 "cyberpunk_style/skill.md"
    - 强制相对定位到 SKILLS_DIR；使用 str: 后缀 + resolve 防穿越
    - 返回 None+err 表示非法
    """
    if not rel_path or not rel_path.strip():
        return None, "路径不能为空"

    # 去掉首尾空白和路径分隔符前缀，防止 "/etc" 或 "\\etc" 逃逸
    rel_path = rel_path.strip()
    rel_path = rel_path.replace("\\", "/").lstrip("/").strip()
    if not rel_path:
        return None, "路径非法"

    # 禁止绝对路径与父级跳转
    if rel_path.startswith(("/", "~")):
        return None, "路径非法：禁止绝对路径"
    if ".." in rel_path.split("/"):
        return None, "路径非法：禁止父级跳转 (..)"

    # 拼到沙箱根下，再用 resolve 规范化（吃符号链接 + 相对段）
    candidate = os.path.join(SKILLS_DIR, rel_path)
    real_candidate = os.path.realpath(candidate)
    real_skills = os.path.realpath(SKILLS_DIR)

    # 关键判定 1：必须严格在 SKILLS_DIR 之内
    # 注意用分隔符收尾，避免 skills_other 这种前缀干扰
    if not (real_candidate == real_skills
            or real_candidate.startswith(real_skills + os.path.sep)):
        return None, "路径非法：超出沙箱范围"

    return real_candidate, None


def delete_file(rel_path, recursive=False):
    """
    删除 skills/ 目录内的一个文件或 Skill 子目录。

    参数:
      - rel_path: skills 目录下的相对路径。例如 "old_skill/skill.md"
                  或删整个目录 "old_skill"（需 recursive=true）
      - recursive: 删除整个子目录时设为 true

    安全约束（代码强制）:
      - 只能删 skills/ 内部，绝不可能删到项目外
      - 不能删除 skills 主目录本身
      - recursive=false 时只删文件；目标若是目录会报错
    """
    target, err = _resolve_within_sandbox(rel_path)
    if err:
        return "错误: " + err

    real_skills = os.path.realpath(SKILLS_DIR)

    # 关键判定 2：禁止删除 skills 主目录本身（即使 rel_path 传了空字符串或其他技巧）
    if target == real_skills:
        return "错误: 不允许删除 skills 主目录"

    if not os.path.exists(target):
        return "错误: 路径不存在: " + rel_path

    # 目录删除必须显式 recursive
    if os.path.isdir(target) and not recursive:
        return ("错误: '" + rel_path + "' 是目录，若要删除整个目录请设 recursive=true")

    # 文件删除：明确只删普通文件，避免误删软链目标
    if os.path.islink(target):
        return "错误: 不允许删除符号链接"

    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        return "已删除 skills/" + rel_path
    except Exception as e:
        return "删除失败: " + str(e)


tool = {
    "name": "delete_file",
    "description": "删除 skills 目录内的文件或无效的 Skill 子目录。"
                  "安全限制：只能删除 skills/ 内部，绝不能删到项目其他位置，也无法删除 skills 主目录。"
                  "删除文件传相对路径如 'bad_skill/skill.md'；删除整个 skill 目录需设 recursive=true 如 'bad_skill'",
    "function": delete_file,
    "parameters": {
        "type": "object",
        "properties": {
            "rel_path": {"type": "string", "description": "skills 目录下的相对路径。文件如 'old_skill/skill.md'；整个目录如 'old_skill'"},
            "recursive": {"type": "boolean", "description": "目标为目录且要删除整个目录时设为 true（默认 false）"}
        },
        "required": ["rel_path"]
    }
}