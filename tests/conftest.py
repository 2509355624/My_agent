# -*- coding: utf-8 -*-
"""测试公共配置。

测试用例用标准库 unittest 编写，因此零额外依赖即可运行：

    python -m unittest discover -s tests -v

若已安装 pytest，也可以：

    python -m pytest tests -v

这里只做一件事：把项目根目录加入 sys.path，保证无论从哪里被收集，
测试里的 `import app.xxx` 都能解析到本项目的代码。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
