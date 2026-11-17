"""角色扮演提示词：按域拆开，统一从这里导出。"""

__all__ = [
    "_AUTO_CONFESS_TASK",
    "_FINISH_ACT_TASK",
    "_FINISH_CUSTOM_HINT",
    "_FINISH_END_TASK",
    "_FINISH_LAST_TASK",
    "_FINISH_MORNING_TASK",
    "_GUARD_TEXT",
    "_INTENT_PROMPT",
    "_OPENING_PROMPT",
    "_PHASE_TASK",
    "_SCENE_DUTY",
    "_SCORE_PROMPT",
    "_SCORE_PROMPT_V2",
    "_SKIP_TASK",
    "_SKIP_TASK_DOING",
    "_SKIP_TASK_MULTI",
    "_THIS_TURN_ECHO",
]

from .finish import _AUTO_CONFESS_TASK
from .finish import _FINISH_ACT_TASK
from .finish import _FINISH_CUSTOM_HINT
from .finish import _FINISH_END_TASK
from .finish import _FINISH_LAST_TASK
from .finish import _FINISH_MORNING_TASK
from .actor import _GUARD_TEXT
from .intent import _INTENT_PROMPT
from .opening import _OPENING_PROMPT
from .time_flow import _PHASE_TASK
from .actor import _SCENE_DUTY
from .scoring import _SCORE_PROMPT
from .scoring import _SCORE_PROMPT_V2
from .time_flow import _SKIP_TASK
from .time_flow import _SKIP_TASK_DOING
from .time_flow import _SKIP_TASK_MULTI
from .actor import _THIS_TURN_ECHO
