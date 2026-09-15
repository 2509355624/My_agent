"""删除 Skill 目录内的文件或子目录（强沙箱约束）

安全设计（代码层面，不依赖 AI 自觉）：
1. 仅允许操作 skills/ 目录内的相对路径（删除面比读取面更严，不放行绝对路径）
2. 路径解析 + startswith 双重校验，确保最终路径绝不跳出 SKILLS_DIR
3. 绝对禁止删除 SKILLS_DIR 本身（skills 主目录不可删）

路径规则统一在 app/tools/sandbox.py，与 read_file / write_file / list_files 共用一份实现。
"""

import os
import shutil

from app.tools.sandbox import resolve_in_skills, skills_root, to_rel


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
    target, err = resolve_in_skills(rel_path, allow_absolute=False)
    if err:
        return "错误: " + err

    # 关键判定：禁止删除 skills 主目录本身（即使 rel_path 传了空字符串或其他技巧）
    if target == skills_root():
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
        return "已删除 skills/" + to_rel(target)
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