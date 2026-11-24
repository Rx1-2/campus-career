"""大学生职业网 CampusCareer —— 零依赖 HTTP 服务（标准库实现）。

启动：python server.py [--port 8080]
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import traceback
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import auth
from db import get_conn
from seed import ensure_ready
import seed_data

STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/jobs": "jobs.html",
    "/jobs.html": "jobs.html",
    "/job": "job-detail.html",
    "/job.html": "job-detail.html",
    "/plan": "plan.html",
    "/plan.html": "plan.html",
    "/resume": "resume.html",
    "/resume.html": "resume.html",
    "/interns": "interns.html",
    "/interns.html": "interns.html",
    "/toolbox": "toolbox.html",
    "/toolbox.html": "toolbox.html",
    "/roadmap": "roadmap.html",
    "/roadmap.html": "roadmap.html",
    "/about": "about.html",
    "/about.html": "about.html",
    "/login": "login.html",
    "/login.html": "login.html",
    "/register": "register.html",
    "/register.html": "register.html",
}

DEGREES = ["大专", "本科", "硕士", "博士"]
JOB_TYPES = ["校招", "实习", "社招"]
APP_STATUSES = ["已投递", "已查看", "面试中", "已录用", "未通过"]


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg = msg
        self.code = code


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 数据访问

JOB_SELECT = """
SELECT j.id, j.title, j.type, j.city, j.degree, j.salary_min, j.salary_max,
       j.days_per_week, j.convertible, j.publish_date, j.heat,
       CASE WHEN j.type = '实习' THEN '日薪' ELSE '月薪' END AS pay_unit,
       c.id AS company_id, c.name AS company_name, c.industry, c.size AS company_size,
       c.logo_text, c.tags
FROM jobs j JOIN companies c ON c.id = j.company_id
"""


def _init_cjk_table():
    """用 GB2312 常用字表建立「正常汉字」集合。"""
    table = set()
    for high in range(0xB0, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                table.add(bytes([high, low]).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return table


_COMMON_CJK = _init_cjk_table()


def _looks_clean(text):
    """判断字符串像不像正常中文：大部分字符是常用汉字/ASCII/全角标点才算正常。"""
    common = sum(1 for ch in text if ch in _COMMON_CJK or 0x20 <= ord(ch) <= 0x7E
                 or 0x3000 <= ord(ch) <= 0x303F or 0xFF00 <= ord(ch) <= 0xFF60)
    return common / len(text) >= 0.8


def _fix_mojibake(text):
    """把「UTF-8 字节被当成 GBK/CP1252 解码」产生的乱码还原回中文，还原失败原样返回。"""
    if not text or all(ord(ch) < 128 for ch in text) or _looks_clean(text):
        return text
    for chain in (("latin-1", "utf-8"), ("cp1252", "utf-8"), ("gbk", "utf-8"), ("gb18030", "utf-8")):
        try:
            fixed = text.encode(chain[0]).decode(chain[1])
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
            continue
        if fixed != text:
            return fixed
    return text


def _q(query, key):
    """取查询参数：标准百分号编码按 UTF-8 解码，失败再按 GB18030（兼容旧终端），最后修复乱码。"""
    return _fix_mojibake((query.get(key, [""])[0] or "").strip())


def api_stats():
    conn = get_conn()
    try:
        total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        total_companies = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        intern_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE type='实习'").fetchone()[0]
        campus_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE type='校招'").fetchone()[0]
        convertible = conn.execute("SELECT COUNT(*) FROM jobs WHERE convertible=1").fetchone()[0]
        applications = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        cities = conn.execute("SELECT COUNT(DISTINCT city) FROM jobs").fetchone()[0]
        industries = conn.execute(
            "SELECT c.industry, COUNT(j.id) AS n FROM jobs j JOIN companies c ON c.id=j.company_id "
            "GROUP BY c.industry ORDER BY n DESC").fetchall()
        latest = conn.execute(JOB_SELECT + " ORDER BY j.publish_date DESC, j.id DESC LIMIT 6").fetchall()
        hot = conn.execute(JOB_SELECT + " ORDER BY j.heat DESC LIMIT 5").fetchall()
        return {
            "total_jobs": total_jobs, "total_companies": total_companies,
            "intern_jobs": intern_jobs, "campus_jobs": campus_jobs,
            "convertible_jobs": convertible, "applications": applications,
            "cities": cities,
            "industries": rows_to_dicts(industries),
            "latest_jobs": rows_to_dicts(latest),
            "hot_jobs": rows_to_dicts(hot),
        }
    finally:
        conn.close()


def api_filters():
    conn = get_conn()
    try:
        cities = [r[0] for r in conn.execute("SELECT DISTINCT city FROM jobs ORDER BY city")]
        industries = [r[0] for r in conn.execute("SELECT DISTINCT industry FROM companies ORDER BY industry")]
        return {"cities": cities, "industries": industries,
                "degrees": DEGREES, "types": JOB_TYPES}
    finally:
        conn.close()


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num(value, default=0):
    """把筛选参数转成整数；实习岗位的日薪（如 "200"）换算成月薪口径（×22）。"""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    if 0 < n < 1000:
        n *= 22
    return n


def api_jobs(query):
    keyword = _q(query, "keyword")[:50]
    city = _q(query, "city")
    industry = _q(query, "industry")
    degree = _q(query, "degree")
    jtype = _q(query, "type")
    salary_floor = _num(query.get("salary", [""])[0], 0)
    days = _int(query.get("days", [""])[0], 0) or 0
    convertible = _q(query, "convertible")
    sort = _q(query, "sort") or "latest"
    page = max(1, _int(query.get("page", ["1"])[0], 1) or 1)
    size = min(50, max(4, _int(query.get("page_size", ["9"])[0], 9) or 9))

    where, params = [], []
    if keyword:
        where.append("(j.title LIKE ? OR c.name LIKE ? OR j.description LIKE ? OR c.tags LIKE ?)")
        like = f"%{keyword}%"
        params += [like, like, like, like]
    if city:
        where.append("j.city = ?")
        params.append(city)
    if industry:
        where.append("c.industry = ?")
        params.append(industry)
    if degree:
        where.append("j.degree = ?")
        params.append(degree)
    if jtype:
        where.append("j.type = ?")
        params.append(jtype)
    if salary_floor > 0:
        where.append("CASE WHEN j.type = '实习' THEN j.salary_max * 22 ELSE j.salary_max END >= ?")
        params.append(salary_floor)
    if days > 0:
        where.append("j.days_per_week >= ?")
        params.append(min(days, 7))
    if convertible in ("1", "true"):
        where.append("j.convertible = 1")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = {
        "latest": "ORDER BY j.publish_date DESC, j.id DESC",
        "salary": "ORDER BY (CASE WHEN j.type = '实习' THEN j.salary_max * 22 ELSE j.salary_max END) DESC, j.id DESC",
        "salary_asc": "ORDER BY (CASE WHEN j.type = '实习' THEN j.salary_min * 22 ELSE j.salary_min END) ASC, j.id DESC",
        "heat": "ORDER BY j.heat DESC, j.id DESC",
    }.get(sort, "ORDER BY j.publish_date DESC, j.id DESC")

    conn = get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id=j.company_id " + where_sql,
            params).fetchone()[0]
        rows = conn.execute(
            JOB_SELECT + f" {where_sql} {order_sql} LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size]).fetchall()
        pages = max(1, (total + size - 1) // size)
        return {"total": total, "page": page, "page_size": size, "pages": pages,
                "items": rows_to_dicts(rows), "applied_filters": {
                    "keyword": keyword, "city": city, "industry": industry, "degree": degree,
                    "type": jtype, "salary": salary_floor, "days": days,
                    "convertible": convertible == "1", "sort": sort}}
    finally:
        conn.close()


def api_job_detail(job_id):
    conn = get_conn()
    try:
        row = conn.execute(JOB_SELECT + " WHERE j.id = ?", (job_id,)).fetchone()
        if not row:
            raise ApiError("岗位不存在或已下线", 404)
        job = dict(row)
        detail = conn.execute("SELECT description, requirement FROM jobs WHERE id=?", (job_id,)).fetchone()
        job["description"], job["requirement"] = detail[0], detail[1]
        job["company"] = dict(conn.execute(
            "SELECT id, name, industry, size, city, logo_text, intro, tags FROM companies WHERE id = ?",
            (job["company_id"],)).fetchone())
        job["related"] = rows_to_dicts(conn.execute(
            JOB_SELECT + " WHERE j.company_id = ? AND j.id != ? ORDER BY j.publish_date DESC LIMIT 4",
            (job["company_id"], job_id)).fetchall())
        similar = conn.execute(
            JOB_SELECT + " WHERE j.id != ? AND (j.title LIKE ? OR c.industry = ?) "
                         "ORDER BY j.heat DESC LIMIT 5",
            (job_id, "%" + job["title"][:4] + "%", job["company"]["industry"])).fetchall()
        job["similar"] = rows_to_dicts(similar)
        return job
    finally:
        conn.close()


def api_companies(query):
    industry = _q(query, "industry")
    keyword = _q(query, "keyword")[:50]
    where, params = [], []
    if industry:
        where.append("industry = ?")
        params.append(industry)
    if keyword:
        where.append("(name LIKE ? OR tags LIKE ? OR intro LIKE ?)")
        params += [f"%{keyword}%"] * 3
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM jobs j WHERE j.company_id=c.id) AS job_count "
            "FROM companies c " + where_sql + " ORDER BY job_count DESC, c.id", params).fetchall()
        return {"total": len(rows), "items": rows_to_dicts(rows)}
    finally:
        conn.close()


def api_company_detail(cid):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
        if not row:
            raise ApiError("企业不存在", 404)
        data = dict(row)
        data["jobs"] = rows_to_dicts(conn.execute(
            JOB_SELECT + " WHERE j.company_id = ? ORDER BY j.publish_date DESC", (cid,)).fetchall())
        return data
    finally:
        conn.close()


def api_quiz():
    conn = get_conn()
    try:
        qs = conn.execute("SELECT id, seq, content FROM quiz_questions ORDER BY seq").fetchall()
        opts = conn.execute("SELECT question_id, label FROM quiz_options ORDER BY id").fetchall()
        grouped = {}
        for o in opts:
            grouped.setdefault(o["question_id"], []).append(o["label"])
        return {"total": len(qs), "questions": [
            {"id": q["id"], "seq": q["seq"], "content": q["content"],
             "options": grouped.get(q["id"], [])} for q in qs]}
    finally:
        conn.close()


def api_quiz_submit(payload):
    answers = payload.get("answers") or {}
    if not isinstance(answers, dict):
        raise ApiError("答案格式不正确")
    conn = get_conn()
    try:
        questions = conn.execute("SELECT id FROM quiz_questions ORDER BY seq").fetchall()
        if not questions:
            raise ApiError("题库为空", 500)
        scores = {d: 0 for d in seed_data.DIMENSION_INFO}
        answered = 0
        for q in questions:
            raw = answers.get(str(q["id"]))
            idx = _int(raw, -1)
            if idx is None or idx < 0:
                continue
            options = conn.execute(
                "SELECT dim, weight FROM quiz_options WHERE question_id = ? ORDER BY id",
                (q["id"],)).fetchall()
            if idx >= len(options):
                continue
            scores[options[idx]["dim"]] += options[idx]["weight"]
            answered += 1
        if answered < len(questions):
            raise ApiError(f"还有 {len(questions) - answered} 题未作答，请完成全部题目")

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        top = ranked[:3]
        primary = top[0][0]
        info = seed_data.DIMENSION_INFO
        max_score = len(questions) * 5
        result = {
            "scores": [{"dim": d, "name": info[d]["name"], "score": s,
                        "percent": round(s / max_score * 100, 1)} for d, s in ranked],
            "code": "".join(d for d, _ in top),
            "primary": {"dim": primary, **info[primary]},
            "secondary": [{"dim": d, **info[d]} for d, _ in top[1:]],
            "paths": build_paths(primary),
        }
        return result
    finally:
        conn.close()


def build_paths(dim):
    paths = {
        "R": [("大一", "学好机械制图、电路与编程基础，加入机器人或电子社团"),
              ("大二", "参加工程实训与学科竞赛（如电子设计、机械创新设计大赛）"),
              ("大三", "进入制造类企业实习，熟悉设备调试与产线流程"),
              ("大四", "锁定自动化/嵌入式/机械设计方向，完成毕业设计与企业实训")],
        "I": [("大一", "打牢数学与编程基础，刷算法题起步"),
              ("大二", "参与实验室项目或开源项目，掌握数据处理工具链"),
              ("大三", "参加数据/算法类竞赛或科研，产出可展示成果"),
              ("大四", "投递算法、数据分析岗位，准备技术面与论文/项目答辩")],
        "A": [("大一", "系统学习设计基础与软件工具，持续积累作品"),
              ("大二", "参与校园活动物料与设计比赛，建立作品集"),
              ("大三", "争取设计类实习，参与真实商业项目"),
              ("大四", "以作品集为核心投递 UI/视觉/内容岗位")],
        "S": [("大一", "参与志愿服务与社团，锻炼沟通与共情能力"),
              ("大二", "担任社团骨干，尝试活动组织与新人带教"),
              ("大三", "进入教育、人力、客户服务类岗位实习"),
              ("大四", "投递 HR、课程运营、客户成功等岗位，准备结构化面试")],
        "E": [("大一", "参与学生组织与创业社团，锻炼表达与组织"),
              ("大二", "尝试校园创业项目或商业比赛（如商业计划书大赛）"),
              ("大三", "进入市场、销售、产品运营实习，积累业务结果"),
              ("大四", "投递产品、市场、咨询与管培生岗位，突出成果与影响力")],
        "C": [("大一", "扎实学好财务、统计或信息管理类专业课"),
              ("大二", "考取初级会计等证书，熟练 Excel 与数据处理"),
              ("大三", "进入财务、运营或审计类岗位实习"),
              ("大四", "投递财务分析、运营、数据运营类岗位，准备专业笔试")],
    }
    return [{"stage": s, "action": a} for s, a in paths.get(dim, [])]


def api_interview(query):
    category = _q(query, "category")
    keyword = _q(query, "keyword")[:50]
    where, params = [], []
    if category:
        where.append("category = ?")
        params.append(category)
    if keyword:
        where.append("(question LIKE ? OR answer LIKE ?)")
        params += [f"%{keyword}%"] * 2
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_conn()
    try:
        cats = [r[0] for r in conn.execute(
            "SELECT category, COUNT(*) n FROM interview_questions GROUP BY category ORDER BY n DESC")]
        rows = conn.execute(
            "SELECT * FROM interview_questions " + where_sql + " ORDER BY category, id",
            params).fetchall()
        return {"categories": cats, "total": len(rows), "items": rows_to_dicts(rows)}
    finally:
        conn.close()


def api_salary(query):
    direction = _q(query, "direction")
    city = _q(query, "city")
    where, params = [], []
    if direction:
        where.append("direction = ?")
        params.append(direction)
    if city:
        where.append("city = ?")
        params.append(city)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_conn()
    try:
        directions = [r[0] for r in conn.execute("SELECT DISTINCT direction FROM salary_stats ORDER BY direction")]
        cities = [r[0] for r in conn.execute("SELECT DISTINCT city FROM salary_stats ORDER BY city")]
        rows = conn.execute("SELECT * FROM salary_stats " + where_sql +
                            " ORDER BY p50 DESC", params).fetchall()
        return {"directions": directions, "cities": cities, "items": rows_to_dicts(rows)}
    finally:
        conn.close()


def api_create_application(payload):
    name = str(payload.get("name", "")).strip()
    phone = str(payload.get("phone", "")).strip()
    email = str(payload.get("email", "")).strip()
    job_id = _int(payload.get("job_id"), None)
    if not (2 <= len(name) <= 20):
        raise ApiError("请填写 2-20 个字的姓名")
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise ApiError("请填写正确的 11 位手机号")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email):
        raise ApiError("请填写正确的邮箱地址")
    if not job_id:
        raise ApiError("请选择要投递的岗位")
    conn = get_conn()
    try:
        job = conn.execute("SELECT id, title FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise ApiError("岗位不存在或已下线", 404)
        dup = conn.execute(
            "SELECT id FROM applications WHERE phone = ? AND job_id = ?", (phone, job_id)).fetchone()
        if dup:
            raise ApiError("该手机号已投递过此岗位，请勿重复投递", 409)
        cur = conn.execute(
            "INSERT INTO applications (name, phone, email, job_id, status, created_at) "
            "VALUES (?, ?, ?, ?, '已投递', ?)",
            (name, phone, email, job_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return {"id": cur.lastrowid, "job_id": job_id, "job_title": job["title"],
                "status": "已投递"}
    finally:
        conn.close()


def api_applications(query):
    phone = (query.get("phone", [""])[0] or "").strip()
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise ApiError("请填写正确的 11 位手机号用于查询投递记录")
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT a.id, a.name, a.status, a.created_at, j.id AS job_id, j.title, j.type, "
            "j.city, j.salary_min, j.salary_max, "
            "CASE WHEN j.type = '实习' THEN '日薪' ELSE '月薪' END AS pay_unit, c.name AS company_name, c.logo_text "
            "FROM applications a JOIN jobs j ON j.id=a.job_id JOIN companies c ON c.id=j.company_id "
            "WHERE a.phone = ? ORDER BY a.created_at DESC, a.id DESC", (phone,)).fetchall()
        status_order = APP_STATUSES
        stats = {s: 0 for s in status_order}
        for r in rows:
            if r["status"] in stats:
                stats[r["status"]] += 1
        return {"total": len(rows), "items": rows_to_dicts(rows), "status_stats": stats}
    finally:
        conn.close()


# ---------------------------------------------------------------- HTTP 层

class Handler(BaseHTTPRequestHandler):
    server_version = "CampusCareer/3.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # --- helpers
    def _current_user(self):
        """从 Cookie 里解出登录用户；签名错误/过期/用户已删除都返回 None。"""
        if getattr(self, "_user_cache", 0) != 0:
            return self._user_cache
        self._user_cache = None
        raw = self.headers.get("Cookie")
        if raw:
            jar = SimpleCookie()
            try:
                jar.load(raw)
            except Exception:
                jar = {}
            morsel = jar.get(auth.COOKIE_NAME)
            payload = auth.read_session(morsel.value if morsel else None)
            if payload:
                user = auth.get_user(payload.get("uid"))
                if user:
                    self._user_cache = user
        return self._user_cache

    def _session_cookie(self, token, max_age):
        attrs = [f"{auth.COOKIE_NAME}={token}", "Path=/", f"Max-Age={max_age}",
                 "HttpOnly", "SameSite=Lax"]
        return "; ".join(attrs)

    def _client_ip(self):
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _is_https(self):
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _redirect_to_https(self):
        """反向代理（隧道/网关）以 https 对外时把 http 跳过去，避免混合内容或去端口。"""
        if os.environ.get("CC_HTTPS_REDIRECT") != "1":
            return False
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        if proto != "http" or not host:
            return False
        self.send_response(301)
        self.send_header("Location", "https://" + host.split(",")[0].strip() + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _send_bytes(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, data, status=200):
        payload = json.dumps({"code": 0, "msg": "ok", "data": data}, ensure_ascii=False)
        self._send_bytes(status, payload.encode("utf-8"), "application/json; charset=utf-8",
                         {"Vary": "Accept-Encoding", "Cache-Control": "no-store"})

    def _error(self, msg, status=400):
        payload = json.dumps({"code": status, "msg": msg, "data": None}, ensure_ascii=False)
        self._send_bytes(status, payload.encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self):
        length = _int(self.headers.get("Content-Length"), 0) or 0
        if length > 64 * 1024:
            raise ApiError("请求体过大", 413)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            try:
                data = json.loads(raw.decode("gb18030"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError("请求数据不是合法的 JSON（请使用 UTF-8 编码）")
        except json.JSONDecodeError:
            raise ApiError("请求数据不是合法的 JSON")
        if not isinstance(data, dict):
            raise ApiError("请求数据格式不正确")
        return data

    def _serve_file(self, base, rel_path, no_store=False):
        safe = os.path.normpath(rel_path).replace("\\", "/")
        if safe.startswith("..") or os.path.isabs(safe):
            self._error("非法路径", 403)
            return
        full = os.path.join(base, safe)
        if not os.path.isfile(full):
            self._error("文件不存在", 404)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            body = f.read()
        if no_store or full.endswith(".html"):
            cache = "no-store"
        elif full.endswith((".js", ".css")):
            cache = "no-cache"
        else:
            cache = "public, max-age=3600"
        self._send_bytes(200, body, ctype, {"Cache-Control": cache})

    # --- routing
    def do_GET(self):
        if self._redirect_to_https():
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, encoding="utf-8", errors="replace")
        try:
            user = self._current_user()
            if path.startswith("/api/"):
                if path != "/api/auth/login" and not user:
                    raise ApiError("请先登录后再操作", 401)
                return self._route_api_get(path, query)
            if path == "/health":
                return self._json({"status": "up", "auth": bool(user),
                                   "time": datetime.now().isoformat(timespec="seconds")})
            if not user:
                if path in ("/login", "/register"):
                    return self._serve_file(TEMPLATE_DIR, PAGES[path], no_store=True)
                if path.startswith("/static/"):
                    return self._serve_file(STATIC_DIR, path[len("/static/"):])
                nxt = path + (("?" + parsed.query) if parsed.query else "")
                target = "/login" if path in ("/", "") else "/login?next=" + quote(nxt, safe="")
                return self._redirect(target)
            if path == "/logout":
                return self._finish_logout()
            if path in ("/login", "/register"):
                return self._redirect("/")
            if path.startswith("/static/"):
                return self._serve_file(STATIC_DIR, path[len("/static/"):])
            if path in PAGES:
                return self._serve_file(TEMPLATE_DIR, PAGES[path])
            if path in ("/favicon.ico",):
                return self._send_bytes(204, b"", "image/x-icon")
            self._error("页面不存在", 404)
        except ApiError as e:
            self._error(e.msg, e.code)
        except Exception:
            traceback.print_exc()
            self._error("服务器内部错误", 500)

    do_HEAD = do_GET

    def do_POST(self):
        if self._redirect_to_https():
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/auth/login":
                return self._login()
            if path == "/api/auth/register":
                return self._register()
            if path == "/api/auth/logout":
                return self._finish_logout()
            if not self._current_user():
                raise ApiError("请先登录后再操作", 401)
            if path == "/api/quiz/submit":
                return self._json(api_quiz_submit(self._read_json()))
            if path == "/api/applications":
                return self._json(api_create_application(self._read_json()), 201)
            self._error("接口不存在", 404)
        except ApiError as e:
            self._error(e.msg, e.code)
        except Exception:
            traceback.print_exc()
            self._error("服务器内部错误", 500)

    # --- 登录 / 退出
    def _login(self):
        payload = self._read_json()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        rate_key = self._client_ip() + "|" + username.lower()
        wait = auth.check_rate(rate_key)
        if wait:
            raise ApiError(f"登录失败次数过多，请 {wait} 秒后再试", 429)
        if not username or not password:
            raise ApiError("请输入账号和密码")
        user = auth.find_user(username)
        if not user or not auth.verify_password(password, user["password_hash"]):
            auth.note_failure(rate_key)
            raise ApiError("账号或密码不正确", 401)
        auth.clear_failures(rate_key)
        auth.touch_login(user["id"])
        self._send_user(user, "登录成功")

    def _register(self):
        payload = self._read_json()
        rate_key = "register|" + self._client_ip()
        wait = auth.check_rate(rate_key)
        if wait:
            raise ApiError(f"注册过于频繁，请 {wait} 秒后再试", 429)
        try:
            username, password, display_name = auth.validate_register(
                payload.get("username"), payload.get("password"), payload.get("display_name"))
        except ValueError as e:
            raise ApiError(str(e))
        try:
            user_id = auth.register_user(username, password, display_name)
        except ValueError as e:
            auth.note_failure(rate_key)
            raise ApiError(str(e), 409)
        user = auth.get_user(user_id)
        self._send_user(user, "注册成功，已自动登录", status=201)

    def _send_user(self, user, msg, status=200):
        token = auth.make_session(user["id"], user["username"])
        body = json.dumps({"code": 0, "msg": msg, "data": {
            "id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "role": user["role"]}}, ensure_ascii=False)
        cookie = self._session_cookie(token, auth.SESSION_HOURS * 3600)
        if self._is_https():
            cookie += "; Secure"
        self._send_bytes(status, body.encode("utf-8"), "application/json; charset=utf-8",
                         {"Set-Cookie": cookie, "Cache-Control": "no-store"})

    def _finish_logout(self):
        if self.command == "POST":
            self._send_bytes(200, json.dumps({"code": 0, "msg": "已退出登录", "data": None},
                                             ensure_ascii=False).encode("utf-8"),
                             "application/json; charset=utf-8",
                             {"Set-Cookie": self._session_cookie("", 0)})
            return
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", self._session_cookie("", 0))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _route_api_get(self, path, query):
        if path == "/api/auth/me":
            return self._json(self._current_user())
        if path == "/api/stats":
            return self._json(api_stats())
        if path == "/api/filters":
            return self._json(api_filters())
        if path == "/api/jobs":
            return self._json(api_jobs(query))
        m = re.fullmatch(r"/api/jobs/(\d+)", path)
        if m:
            return self._json(api_job_detail(int(m.group(1))))
        if path == "/api/companies":
            return self._json(api_companies(query))
        m = re.fullmatch(r"/api/companies/(\d+)", path)
        if m:
            return self._json(api_company_detail(int(m.group(1))))
        if path == "/api/quiz":
            return self._json(api_quiz())
        if path == "/api/interview-questions":
            return self._json(api_interview(query))
        if path == "/api/salary-stats":
            return self._json(api_salary(query))
        if path == "/api/applications":
            return self._json(api_applications(query))
        raise ApiError("接口不存在", 404)


def main():
    parser = argparse.ArgumentParser(description="大学生职业网 CampusCareer")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--host", default=os.environ.get("CC_HOST", "0.0.0.0"))
    args = parser.parse_args()

    created = ensure_ready()
    print("数据库初始化完成" + ("（已灌入初始数据）" if created else "（沿用已有数据）"), flush=True)
    admin = auth.ensure_admin()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print("=" * 52, flush=True)
    print("  大学生职业网 CampusCareer 已启动", flush=True)
    print(f"  监听：{url}", flush=True)
    print("  --------------------------------------------------", flush=True)
    print("  登录方式：浏览器打开后用账号 + 密码登录，也可自助注册", flush=True)
    if admin:
        print(f"  首次启动已创建管理员账号：admin  密码：{admin['password']}", flush=True)
        if admin.get("from_env"):
            print("  （密码来自环境变量 ADMIN_PASSWORD）", flush=True)
        else:
            print("  （该密码仅本次显示，请立即记录；账号存于 career.db 的 users 表）", flush=True)
    else:
        print("  管理员账号已存在，密码沿用首次创建时设定的值", flush=True)
        print("  （忘记密码：设置环境变量 ADMIN_PASSWORD 后重启即可重置）", flush=True)
    print("=" * 52, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
