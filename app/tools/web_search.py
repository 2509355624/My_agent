"""网页搜索工具"""
from duckduckgo_search import DDGS


def _web_search(query, max_results=5):
    try:
        results = DDGS().text(keywords=query, max_results=max_results)
        if not results:
            return "搜索 '" + query + "' 没有找到结果"

        output = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            output.append("[" + str(i) + "] " + title + "\n    " + body + "\n    来源: " + href)

        return "搜索 '" + query + "' 找到 " + str(len(results)) + " 条结果：\n\n" + "\n\n".join(output)
    except Exception as e:
        return "搜索失败: " + str(e)


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
