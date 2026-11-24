"""WSGI 适配层：把 server.Handler 挂到 WSGI 容器（PythonAnywhere / gunicorn / mod_wsgi）。

server.py 本身是自带的 http.server 应用，这里用一个内存桩（假 socket）
接住 Handler 的读写，把 WSGI 的 environ 翻译成 Handler 认识的样子。

PythonAnywhere 的用法：后台 Web 标签页里把 WSGI 配置文件的源码
改成这里给出的一行，见 DEPLOY_PYTHONANYWHERE.txt。
"""
import io
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import server
import auth
from seed import ensure_ready

_ready = False


def _bootstrap():
    """首次请求时建库、灌数据、建管理员账号。每进程只跑一次。"""
    global _ready
    if _ready:
        return
    ensure_ready()
    admin = auth.ensure_admin()
    if admin:
        sys.stderr.write("[campus-career] 已创建管理员账号 admin / %s\n" % admin["password"])
    else:
        sys.stderr.write("[campus-career] 管理员账号已存在，密码沿用首次创建时设定的值\n")
    _ready = True


class _Socket:
    """冒充 socket：读走请求体，收下响应字节。"""

    def __init__(self, body=b""):
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = 200
        self.headers = []

    def makefile(self, mode="rb", *args, **kwargs):
        return self.rfile if "r" in mode else self.wfile


class _Reply:
    """冒充 Handler 自身输出的 send_response / send_header / end_headers。"""

    def __init__(self):
        self.status = 200
        self.headers = []

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass


class _Request(server.Handler):
    """复用 Handler 的全部路由逻辑，只是换掉底层 socket。"""

    def __init__(self, environ, body):
        self.environ = environ
        self.command = environ.get("REQUEST_METHOD", "GET").upper()
        raw_path = environ.get("PATH_INFO", "/") or "/"
        query = environ.get("QUERY_STRING", "")
        self.path = raw_path + (("?" + query) if query else "")
        self.request_version = "HTTP/1.1"
        self.requestline = "%s %s HTTP/1.1" % (self.command, self.path)
        self.protocol_version = "HTTP/1.1"
        self.client_address = (environ.get("REMOTE_ADDR") or "127.0.0.1", 0)
        self.server = None
        self.close_connection = True
        self._headers = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                self._headers[key[5:].replace("_", "-")] = value
        if environ.get("CONTENT_TYPE"):
            self._headers["Content-Type"] = environ["CONTENT_TYPE"]
        if environ.get("CONTENT_LENGTH"):
            self._headers["Content-Length"] = environ["CONTENT_LENGTH"]
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.reply = _Reply()
        self._user_cache = 0

    # --- 用 _Reply 顶替真实写出
    def send_response(self, code, message=None):
        self.reply.send_response(code, message)

    def send_header(self, key, value):
        self.reply.send_header(key, value)

    def end_headers(self):
        self.reply.end_headers()

    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass

    def handle(self):
        if self.command in ("GET", "HEAD"):
            self.do_GET()
        elif self.command == "POST":
            self.do_POST()
        else:
            self._error("不支持的请求方法", 405)

    @property
    def _out(self):
        status = self.reply.status
        headers = []
        seen = set()
        for key, value in self.reply.headers:
            low = key.lower()
            if low in ("content-length", "transfer-encoding", "connection"):
                continue
            if low == "set-cookie":
                headers.append((key, value))
                continue
            if low in seen:
                headers = [(k, v) for (k, v) in headers if k.lower() != low]
            seen.add(low)
            headers.append((key, value))
        return status, headers, self.wfile.getvalue()

    def read_headers(self):
        return self._headers

    @property
    def headers(self):
        return _Headers(self._headers)


class _Headers(dict):
    def get(self, key, default=None):
        if key in self:
            return dict.__getitem__(self, key)
        low = key.lower()
        for k, v in self.items():
            if k.lower() == low:
                return v
        return default


def application(environ, start_response):
    _bootstrap()

    length = environ.get("CONTENT_LENGTH") or "0"
    try:
        body = environ["wsgi.input"].read(int(length)) if length.isdigit() and int(length) > 0 else b""
    except (ValueError, KeyError):
        body = b""

    req = _Request(environ, body)
    try:
        req.handle()
    except Exception:  # Handler 内部已兜底，这里再保一层
        server.traceback.print_exc()
        start_response("500 Internal Server Error",
                       [("Content-Type", "text/plain; charset=utf-8")])
        return [b"internal error"]

    status, headers, payload = req._out
    reason = {200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently",
              302: "Found", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
              404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
              413: "Payload Too Large", 429: "Too Many Requests",
              500: "Internal Server Error"}.get(status, "OK")
    headers = [(k, v) for (k, v) in headers if k.lower() != "content-length"]
    headers.append(("Content-Length", str(len(payload))))
    start_response("%d %s" % (status, reason), headers)
    return [payload]
