"""账号与登录：密码哈希、登录会话、登录限流。

不依赖第三方库：密码用 pbkdf2_hmac(sha256) 加盐存储；
会话用 HMAC 签名的 Cookie，服务端不存 session 表（密钥持久化在 .secret_key）。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from db import get_conn

COOKIE_NAME = "cc_session"
SESSION_HOURS = 12
PBKDF2_ROUNDS = 120_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


def _secret_key():
    """优先用环境变量 CC_SECRET_KEY（云平台重启后会话不失效），否则落到本地 .secret_key。"""
    env_key = (os.environ.get("CC_SECRET_KEY") or "").strip()
    if len(env_key) >= 32:
        return env_key.encode("utf-8")
    path = os.path.join(BASE_DIR, ".secret_key")
    try:
        with open(path, "r", encoding="ascii") as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key.encode("ascii")
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(path, "w", encoding="ascii") as f:
            f.write(key)
    except OSError:
        pass
    return key.encode("ascii")


_SECRET = _secret_key()


# ---------------------------------------------------------------- 密码

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"))


def verify_password(password, stored):
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- 会话签名

def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(user_id, username):
    payload = {"uid": user_id, "u": username, "exp": int(time.time()) + SESSION_HOURS * 3600}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_SECRET, body.encode("ascii"), hashlib.sha256).digest())
    return body + "." + sig


def read_session(token):
    """校验签名与有效期，合法返回 {uid, u, exp}，否则 None。"""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expect = _b64(hmac.new(_SECRET, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------------------------------------------------------------- 用户

def find_user(username):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, display_name, role FROM users WHERE username = ?",
            (username,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user(user_id):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, display_name, role FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_user(username, password, display_name, role="student"):
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, role, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(password), display_name, role,
             time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def touch_login(user_id):
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                     (time.strftime("%Y-%m-%d %H:%M:%S"), user_id))
        conn.commit()
    finally:
        conn.close()


def count_users():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


DISPLAY_NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5·\-\s]{1,20}$")


def validate_register(username, password, display_name):
    """注册参数校验，返回规范化后的 (username, password, display_name)，不合法抛 ValueError。"""
    username = (username or "").strip()
    display_name = (display_name or "").strip()
    password = password or ""
    if not username:
        raise ValueError("请填写账号")
    if not USERNAME_RE.match(username):
        raise ValueError("账号需为 3-20 位字母、数字或下划线")
    if not display_name:
        display_name = username
    if not DISPLAY_NAME_RE.match(display_name):
        raise ValueError("昵称最多 20 个字，不能包含特殊符号")
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    if len(password) > 64:
        raise ValueError("密码不能超过 64 位")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ValueError("密码需同时包含字母和数字")
    return username, password, display_name


def register_user(username, password, display_name):
    """创建学生账号；账号已存在抛 ValueError。"""
    conn = get_conn()
    try:
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            raise ValueError("该账号已被注册，请换一个")
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, role, created_at) "
            "VALUES (?, ?, ?, 'student', ?)",
            (username, hash_password(password), display_name,
             time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def ensure_admin():
    """首次启动内置一个管理员账号。

    密码来源优先级：环境变量 ADMIN_PASSWORD > 随机生成（仅首次打印到日志）。
    """
    env_password = (os.environ.get("ADMIN_PASSWORD") or "").strip()
    if count_users():
        if env_password and len(env_password) >= 8:
            reset_password("admin", env_password)
            return None
        return None
    if env_password and len(env_password) >= 8:
        create_user("admin", env_password, "管理员", "admin")
        return {"username": "admin", "password": env_password, "from_env": True}
    password = secrets.token_urlsafe(9)
    create_user("admin", password, "管理员", "admin")
    return {"username": "admin", "password": password, "from_env": False}


def reset_password(username, password):
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(password), username))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 登录限流

_ATTEMPTS = {}          # key -> [窗口内失败次数, 窗口起点]
MAX_ATTEMPTS = 8        # 每个窗口允许的失败次数
WINDOW_SECONDS = 60     # 固定窗口长度：超过窗口自动解锁


def check_rate(key):
    """返回还需等待的秒数，0 表示可以尝试。"""
    rec = _ATTEMPTS.get(key)
    if not rec:
        return 0
    count, start = rec
    elapsed = time.time() - start
    if elapsed > WINDOW_SECONDS:
        _ATTEMPTS.pop(key, None)
        return 0
    if count >= MAX_ATTEMPTS:
        return int(WINDOW_SECONDS - elapsed) + 1
    return 0


def note_failure(key):
    rec = _ATTEMPTS.get(key)
    if rec and time.time() - rec[1] <= WINDOW_SECONDS:
        rec[0] += 1
    else:
        _ATTEMPTS[key] = [1, time.time()]


def clear_failures(key):
    _ATTEMPTS.pop(key, None)
