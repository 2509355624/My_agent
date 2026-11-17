"""管理接口里反复出现的 agent id 校验。"""

from flask import jsonify

from app.core import agents as agent_store


def agent_or_400(agent_id):
    """校验路径里的 agent id。返回 (id, None) 或 (None, 错误响应)。"""
    aid = agent_store.safe_agent_id(agent_id)
    if aid is None:
        return None, (jsonify({"error": "非法的 agent id：%s" % agent_id}), 400)
    return aid, None
