"""
Skill 管理
加载、列出 Skills

兼容两种 skill 目录结构：
1. 本地生图 skill（image_gen_v1 风格）：
   skills/<name>/
       skill.md          # 小写规则
       workflow.json     # 生图工作流
       character.txt     # 角色底模

2. 标准/脚手架 skill（GitHub 下载风格）：
   skills/<name>/              # 可能多一层同名嵌套
       SKILL.md                # 大写主规范
       VERSION
       references/*.md         # 拆招手册/参考
       assets/
"""

import os
import json
from app.config import SKILLS_DIR


def _resolve_skill_dir(skill_name):
    """定位 skill 真实目录，处理**同名**嵌套层。
    例如 skills/foo/foo/SKILL.md 会下探到真正含 SKILL.md / skill.md 的那一层。

    注意：只认「子目录名 == skill 名」这一种嵌套。子目录名不同（如
    skills/foo-main/foo/）**不会**下探，此时返回 base，上层会读到空。
    2026-09-15 迁移后 skills/ 下已无同名嵌套形态，逻辑保留以防再次遇到。
    """
    base = os.path.join(SKILLS_DIR, skill_name)
    if not os.path.isdir(base):
        return None
    # 若内部还有同名子目录，且当前层没有规范文件，则下探
    nested = os.path.join(base, skill_name)
    if os.path.isdir(nested) and not _contains_spec(base):
        return nested
    return base


def _contains_spec(d):
    """该目录是否含 skill 主规范文件（SKILL.md / skill.md）"""
    for fname in ("SKILL.md", "skill.md"):
        if os.path.exists(os.path.join(d, fname)):
            return True
    return False


def _read_md(d):
    """读取目录下的 SKILL.md（大写优先）或 skill.md（小写回退）

    剥掉开头的 UTF-8 BOM：带 BOM 时首行会变成 `\ufeff---`，frontmatter /
    一行简介的「首行是否分隔线」判断会静默失效（Windows 工具另存为很常见）。
    """
    for fname in ("SKILL.md", "skill.md"):
        p = os.path.join(d, fname)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().lstrip("\ufeff")
    return ""


def _read_references(d):
    """读取 references/ 目录下所有 .md，拼成可附带的规范内容"""
    ref_dir = os.path.join(d, "references")
    if not os.path.isdir(ref_dir):
        return ""
    parts = []
    for f in sorted(os.listdir(ref_dir)):
        if f.endswith(".md"):
            p = os.path.join(ref_dir, f)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    parts.append("<!-- 参考: %s -->\n%s" % (f, fh.read()))
            except OSError:
                continue
    return "\n\n".join(parts)


def _parse_frontmatter(text):
    """解析规范顶部的 YAML frontmatter，只取简单的单行 `key: value`。

    够用即可——本项目只用 kind 一个字段，不为此引入 YAML 依赖。
    无 frontmatter 或未闭合时返回空 dict。
    """
    if not text:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm = {}
    for line in lines[1:]:
        s = line.strip()
        if s in ("---", "..."):
            break
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, _, v = s.partition(":")
        fm[k.strip().lower()] = v.strip().strip('"').strip("'")
    return fm


def load_workflow(path):
    """读一份 ComfyUI API 格式的工作流，失败返回 None。

    `: __SEED__` 是裸占位符、不是合法 JSON，先补引号再解析——工作流里写
    `"seed": __SEED__` 表示每张图换种子，调用侧再把引号连内容一起换成真值。
    一个 skill 可能有多份（`workflow.json` 文生图 / `workflow_i2i.json` 图生图），
    所以这里单独暴露出来给调用方按路径取，不只跟着 load_skill 走。
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().replace(": __SEED__", ': "__SEED__"')
        return json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        print("[警告] 工作流解析失败 " + str(path) + ": " + str(e))
        return None


def load_skill(skill_name):
    """加载 Skill: 返回
    {name, workflow, skill_md, character, version, kind, references}
    - skill_md: 主规范（SKILL.md 优先生成大写）
    - version: VERSION 文件内容（若有）
    - kind: 规范 frontmatter 声明的类型（生图/写作）；未声明时为空串
    - references: references/ 目录下所有 md 原文（若有）
    """
    skill_dir = _resolve_skill_dir(skill_name)
    if not skill_dir:
        return None

    # 生图工作流（可选，只有生图 skill 有）
    workflow_path = os.path.join(skill_dir, "workflow.json")
    workflow = load_workflow(workflow_path)
    if os.path.exists(workflow_path) and workflow is None:
        # 文件在却读不出来：整体判失败，别让上层拿到一个没有工作流的 skill
        return None

    skill_md = _read_md(skill_dir)
    references = _read_references(skill_dir)

    # 类型声明（可选）：给「不出图但属于生图链路」的 skill（资料库、参数存档）用
    kind = _parse_frontmatter(skill_md).get("kind", "")

    

    # 角色底模
    character = ""
    char_path = os.path.join(skill_dir, "character.txt")
    if os.path.exists(char_path):
        with open(char_path, "r", encoding="utf-8") as f:
            character = f.read().strip()

    version = ""
    ver_path = os.path.join(skill_dir, "VERSION")
    if os.path.exists(ver_path):
        with open(ver_path, "r", encoding="utf-8") as f:
            version = f.read().strip()

    return {
        "name": skill_name,
        "path": skill_dir,
        "workflow": workflow,
        "skill_md": skill_md,
        "character": character,
        "version": version,
        "kind": kind,
        "references": references,
    }


def list_skills():
    """列出所有可用 Skill（保留顶层目录名）"""
    if not os.path.isdir(SKILLS_DIR):
        return []
    return [d for d in os.listdir(SKILLS_DIR)
            if os.path.isdir(os.path.join(SKILLS_DIR, d))]


def skill_summary(skill_md):
    """从规范全文里抽一行简介，供 list_skills 展示。

    踩过的坑：带 YAML frontmatter 的规范首行是 `---`，直接取首行只能得到
    一个 `---`，对模型选 skill 毫无信息量。这里按优先级取：
    首个 markdown 标题 > 首个非空且非分隔线的正文行。
    """
    if not skill_md:
        return ""

    lines = skill_md.splitlines()
    start = 0

    # 跳过 YAML frontmatter（首行 --- 到下一个 --- / ... 为止）
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                start = i + 1
                break
        else:
            start = len(lines)  # 没有闭合的 frontmatter，视为无正文

    body = lines[start:]

    for line in body:
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()

    for line in body:
        s = line.strip()
        if s and set(s) != {"-"}:
            return s

    return ""