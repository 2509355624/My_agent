"""管理设置接口共用的小工具。"""

from app.core import agents as agent_store


def put_override(aid, group_id, field, value):
    """写 settings.json 的 <field>_overrides[群号]；value=None 表示删覆盖。"""
    settings = agent_store.load_settings(aid)
    name = field + "_overrides"
    overrides = settings.get(name)
    if not isinstance(overrides, dict):
        overrides = {}
    if value is None:
        overrides.pop(str(group_id), None)
    else:
        overrides[str(group_id)] = value
    settings[name] = overrides
    return bool(agent_store.save_settings(aid, settings))
