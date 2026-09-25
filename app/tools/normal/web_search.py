"""网页搜索工具：优先豆包搜索，未配置/失败时回退 DuckDuckGo"""
import requests
from app.config import (SEARCH_API_KEY, DOUBAO_SEARCH_ENDPOINT,
                        WEB_SEARCH_MAX_CHARS)


def _truncate(text):
    """搜索结果截断。结果会作为 tool_result **永久留在历史里**，不截断的话
    用过一次之后每轮请求都要重发这几千 token（实测一次搜索塞过 3200 token）。"""
    if text and WEB_SEARCH_MAX_CHARS > 0 and len(text) > WEB_SEARCH_MAX_CHARS:
        return text[:WEB_SEARCH_MAX_CHARS] + "\n…（结果已截断，需要细节可换个更具体的词再搜）"
    return text


def _format_results(query, items, limit):
    out = []
    count = 0
    for r in items:
        if count >= limit:
            break
        title = r.get("Title") or r.get("title") or ""
        url = r.get("Url") or r.get("url") or ""
        # Summary（500~1000字，适合 LLM）优先，其次 Snippet
        body = r.get("Summary") or r.get("Snippet") or r.get("body") or ""
        count += 1
        out.append("[" + str(count) + "] " + title + "\n    " + body + "\n    来源: " + url)
    return "搜索 '" + query + "' 找到 " + str(len(items)) + " 条结果：\n\n" + "\n\n".join(out)


def _doubao_search(query, max_results=5):
    """豆包搜索（火山引擎 联网搜索接口）"""
    if not SEARCH_API_KEY:
        return None
    body = {
        "Query": query[:100],          # 接口仅支持 1~100 字符
        "SearchType": "web",
        "Count": min(max_results, 50),
        "NeedSummary": True,
        "Filter": {"NeedUrl": True},
    }
    headers = {"Authorization": "Bearer " + SEARCH_API_KEY, "Content-Type": "application/json"}
    resp = requests.post(DOUBAO_SEARCH_ENDPOINT, json=body, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    meta = data.get("ResponseMetadata") or {}
    if meta.get("Error"):
        raise RuntimeError(str(meta["Error"]))
    items = (data.get("Result") or {}).get("WebResults") or []
    if not items:
        return "搜索 '" + query + "' 没有找到结果"
    return _format_results(query, items, max_results)


def _web_search(query, max_results=5):
    try:
        result = _doubao_search(query, max_results)
        if result:
            return _truncate(result)
        return _truncate(_ddgs_search(query, max_results))
    except Exception as e:
        return "搜索失败: " + str(e)


def _ddgs_search(query, max_results):
    """DuckDuckGo 回退实现"""
    from duckduckgo_search import DDGS
    results = DDGS().text(keywords=query, max_results=max_results)
    if not results:
        return "搜索 '" + query + "' 没有找到结果"
    return _format_results(query, results, max_results)


tool = {
    "name": "web_search",
    "description": "网页搜索，获取最新信息或参考资料",
    "function": _web_search,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "返回结果数量，默认5"}
        },
        "required": ["query"]
    }
}