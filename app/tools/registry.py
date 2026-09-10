"""
工具注册表
所有工具在这里注册，Agent 通过 execute_tool 统一调用
"""

TOOLS = []


def register_tool(name, description, function, parameters):
    """注册一个工具"""
    TOOLS.append({
        "name": name,
        "description": description,
        "function": function,
        "parameters": parameters,
    })


def execute_tool(name, args):
    """执行工具，返回结果字符串"""
    for tool in TOOLS:
        if tool["name"] == name:
            try:
                return tool["function"](**args)
            except Exception as e:
                return "工具执行失败: " + str(e)
    return "未知工具: " + name


# 导入并注册所有工具
from app.tools.get_time import tool as _get_time_tool
register_tool(**_get_time_tool)

from app.tools.web_search import tool as _web_search_tool
register_tool(**_web_search_tool)

from app.tools.load_skill import tool as _load_skill_tool
register_tool(**_load_skill_tool)

from app.tools.generate_image import tool as _generate_image_tool
register_tool(**_generate_image_tool)
