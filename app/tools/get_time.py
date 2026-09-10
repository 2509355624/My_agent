"""获取当前时间工具"""
from datetime import datetime


def _get_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


tool = {
    "name": "get_time",
    "description": "获取当前日期和时间",
    "function": _get_time,
    "parameters": {"type": "object", "properties": {}}
}
