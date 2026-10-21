"""临时预览服务：只挂 moss 漫画台的 blueprint + 页面，不碰 5174 上正在跑的服务。

用来自测页面（截图 / 交互），验证完就关。
    python tools/preview_moss.py [端口]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from flask import Flask, send_from_directory            # noqa: E402

from app import moss_api                                # noqa: E402

WEB = os.path.join(ROOT, "web")
app = Flask(__name__, static_folder=WEB, static_url_path="")
app.register_blueprint(moss_api.bp)


@app.route("/moss")
def page():
    return send_from_directory(WEB, "moss_studio.html")


@app.route("/")
def home():
    return '<a href="/moss">moss 漫画台</a>'


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5199
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
