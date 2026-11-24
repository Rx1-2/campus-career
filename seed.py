"""把 seed_data.py 中的数据写入 SQLite（幂等：已有数据则跳过）。"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import auth
import db
import seed_data


def seed(force=False):
    conn = db.get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if count and not force:
            return False

        if force:
            for t in ("applications", "jobs", "companies", "quiz_options",
                      "quiz_questions", "interview_questions", "salary_stats"):
                conn.execute(f"DELETE FROM {t}")
            conn.execute("DELETE FROM sqlite_sequence")

        for c in seed_data.COMPANIES:
            conn.execute(
                "INSERT INTO companies (name, industry, size, city, logo_text, intro, tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", c)

        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM companies")}
        for j in seed_data.JOBS:
            name = j[1]
            if name not in ids:
                raise ValueError(f"岗位引用了不存在的企业：{name}")
            conn.execute(
                "INSERT INTO jobs (title, company_id, type, city, degree, salary_min, salary_max, "
                "days_per_week, convertible, description, requirement, publish_date, heat) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (j[0], ids[name], j[2], j[3], j[4], j[5], j[6], j[7], j[8], j[9], j[10], j[11], j[12]))

        conn.executemany(
            "INSERT INTO interview_questions (category, question, answer, difficulty) VALUES (?, ?, ?, ?)",
            seed_data.INTERVIEW_QUESTIONS)

        conn.executemany(
            "INSERT INTO salary_stats (direction, city, degree, p25, p50, p75) VALUES (?, ?, ?, ?, ?, ?)",
            seed_data.SALARY_STATS)

        for seq, (content, options) in enumerate(seed_data.QUIZ_QUESTIONS, start=1):
            cur = conn.execute("INSERT INTO quiz_questions (seq, content) VALUES (?, ?)", (seq, content))
            qid = cur.lastrowid
            for label, dim, weight in options:
                conn.execute(
                    "INSERT INTO quiz_options (question_id, label, dim, weight) VALUES (?, ?, ?, ?)",
                    (qid, label, dim, weight))

        conn.commit()
        return True
    finally:
        conn.close()


def ensure_ready():
    db.init_db()
    created = seed()
    return created


if __name__ == "__main__":
    created = ensure_ready()
    conn = db.get_conn()
    for t in ("companies", "jobs", "interview_questions", "salary_stats",
              "quiz_questions", "quiz_options", "users"):
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n}")
    conn.close()
    print("首次灌库" if created else "数据已存在，跳过灌库")
