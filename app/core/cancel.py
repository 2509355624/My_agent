"""
手动中断 agent 循环的取消信号。

在跑的每条请求对应一个 threading.Event，存在进程内的注册表里。前端点
「停止」时 POST /api/stop 把对应 Event 置位，跑这一轮的线程在下一次检查点
看到就收工。

几个取舍：

- **按 request_id 建键，不按 agent**。多标签页可以同时用同一个 agent，
  按 agent 中断会把另一个标签页里正常跑的请求一起打断。
- **只存在进程内存里，不落盘**。Flask 默认多线程，跑循环的线程与处理
  /api/stop 的线程在同一进程内，共享一份内存最直接，也省掉清理成本。
- **没有事件 = 没人要求中断**。所有读取点都按"取不到就放行"处理，所以
  不带 request_id 的调用（测试、脚本、旧客户端）行为与从前完全一致。
"""

import threading

_lock = threading.Lock()
_events = {}                 # request_id -> threading.Event
_local = threading.local()   # 当前线程绑定的取消事件（供工具层读取）


class Cancelled(Exception):
    """工具主动放弃执行（用户中断），与真正的执行失败区分开——
    前者不该被描述成"工具坏了"，模型据此调整下一步是有意义的。"""


def register(request_id):
    """登记一条请求并返回它的取消事件。request_id 为空返回 None。"""
    if not request_id:
        return None
    ev = threading.Event()
    with _lock:
        _events[request_id] = ev
    return ev


def unregister(request_id):
    """请求收尾时摘除，避免注册表随会话无限增长。"""
    with _lock:
        _events.pop(request_id, None)


def cancel(request_id):
    """置位。返回是否命中了在跑的请求（false 说明它已经跑完了）。"""
    with _lock:
        ev = _events.get(request_id)
    if ev is None:
        return False
    ev.set()
    return True


def bind(ev):
    """把取消事件绑定到当前线程，供工具层读取。

    execute_tool(name, args) 的签名不便加参数——会牵动全部工具与既有测试；
    而一条请求固定在一个线程里跑完，所以用线程本地变量传递：天然隔离，
    不需要清理，取不到时按"未中断"处理。
    """
    _local.event = ev


def current():
    """当前线程绑定的取消事件；不在请求上下文中时返回 None。"""
    return getattr(_local, "event", None)


def is_cancelled(ev=None):
    """ev 为空时看当前线程绑定的那个。"""
    ev = ev if ev is not None else current()
    return ev is not None and ev.is_set()


def active_count():
    """在跑的请求数（测试用：验证注册表不泄漏）。"""
    with _lock:
        return len(_events)
