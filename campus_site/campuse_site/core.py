"""
Campus core: Flask app, shared helpers, DB bootstrap.
Константы — config.py, SQL-схемы — _schemas.py.
Маршруты подключаются из routes/* при импорте.
"""
import os, random, sqlite3
import io
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, Response, g, render_template, request, redirect, url_for, session, jsonify, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import re
import tempfile

from config import _ROOT, DB, UPLOADS, ALLOWED_EXT, SECRET_KEY, WD, WDF
from _schemas import SCHEMA, NEW_SCHEMA

app = Flask(__name__, template_folder=os.path.join(_ROOT, "templates"), static_folder=os.path.join(_ROOT, "static"))
app.secret_key = SECRET_KEY

@app.route("/sw.js")
def service_worker():
    from flask import send_from_directory
    resp = send_from_directory(app.static_folder, "sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.route("/manifest.webmanifest")
def web_manifest():
    from flask import send_from_directory
    return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")


# Связи: преподаватель -> назначение (группа + предмет); занятия и отметки общие для группы и предмета
def init_db():
    if os.path.exists(DB):
        return False
    c = sqlite3.connect(DB); c.execute("PRAGMA foreign_keys=ON"); c.executescript(SCHEMA)
    def add(login, pw, name, role, gid=None):
        return c.execute("INSERT INTO users(login,pw,name,role,group_id) VALUES(?,?,?,?,?)",
                         (login, generate_password_hash(pw), name, role, gid)).lastrowid
    a = add("admin", "admin123", "Администратор", "admin")
    # Только аккаунт администратора. Группы, предметы, преподаватели и студенты добавляются через панель /admin.
    for n in ("ИС-21", "ПИ-22"): c.execute("INSERT INTO groups(name) VALUES(?)", (n,))
    for n in ("Математика", "Программирование", "Физика"): c.execute("INSERT INTO subjects(name) VALUES(?)", (n,))
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    c.execute("INSERT INTO announcements(author_id,group_id,title,body,created) VALUES(?,?,?,?,?)",
              (a, None, "Добро пожаловать в Campus",
               "Система готова к работе. Через панель администратора добавьте преподавателей, студентов, назначения и расписание.", now))
    c.commit(); c.close()
    return True

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

class Bad(Exception):
    pass

# Расписание: звонки, недельный шаблон (в т.ч. чётные/нечётные недели и разовые занятия), замены и отмены
def migrate():
    """Добавляет таблицы расписания. Работает и на старой базе, данные не теряются."""
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); c.executescript(NEW_SCHEMA)
    if "head_id" not in [r[1] for r in c.execute("PRAGMA table_info(groups)")]:  # староста группы
        c.execute("ALTER TABLE groups ADD COLUMN head_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
    if not setting(c, "v3"):
        if not c.execute("SELECT 1 FROM bells").fetchone():
            c.executemany("INSERT INTO bells(num,start,end) VALUES(?,?,?)", [(1, "08:30", "10:00"), (2, "10:10", "11:40"),
                (3, "12:10", "13:40"), (4, "13:50", "15:20"), (5, "15:30", "17:00"), (6, "17:10", "18:40")])
        # Диспетчер — служебная роль для управления расписанием (не временный аккаунт)
        c.execute("INSERT OR IGNORE INTO users(login,pw,name,role) VALUES('dispatcher',?,'Диспетчер','dispatcher')",
                  (generate_password_hash("dispatcher123"),))
        c.execute("INSERT OR REPLACE INTO settings VALUES('term_start',?)", ((date.today() - timedelta(days=28)).isoformat(),))
        # Демо-расписание создаётся только при наличии назначений (после добавления преподавателей через /admin)
        bells = [b["id"] for b in c.execute("SELECT id FROM bells ORDER BY num")]
        for a in c.execute("SELECT group_id, subject_id, MIN(teacher_id) t FROM assignments GROUP BY group_id, subject_id").fetchall():
            g_, s_ = a["group_id"], a["subject_id"]
            for wd in (s_ % 5, (s_ + 2) % 5):
                c.execute("INSERT INTO schedule(group_id,subject_id,teacher_id,bell_id,weekday,room) VALUES(?,?,?,?,?,?)",
                          (g_, s_, a["t"], bells[(s_ + g_ - 2) % len(bells)], wd, str(100 + s_ * 10 + g_)))
        c.execute("INSERT OR REPLACE INTO settings VALUES('v3','1')")

    sch_cols = [r[1] for r in c.execute("PRAGMA table_info(schedule)")]
    if "teachers_extra" not in sch_cols:
        try:
            c.execute("ALTER TABLE schedule ADD COLUMN teachers_extra TEXT DEFAULT ''")
        except Exception:
            pass
    cols = [r[1] for r in c.execute("PRAGMA table_info(marks)")]
    if "excuse" not in cols:
        c.execute("ALTER TABLE marks ADD COLUMN excuse TEXT DEFAULT ''")
    ann_cols = [r[1] for r in c.execute("PRAGMA table_info(announcements)")]
    if "target_user_id" not in ann_cols:
        c.execute("ALTER TABLE announcements ADD COLUMN target_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
    c.executescript("""
CREATE TABLE IF NOT EXISTS final_grades(
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  grade TEXT,
  date_from TEXT,
  date_to TEXT,
  note TEXT DEFAULT '',
  set_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  updated TEXT,
  UNIQUE(student_id, subject_id, kind));
CREATE TABLE IF NOT EXISTS active_sessions(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS final_kinds(
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  kind_key TEXT NOT NULL,
  label TEXT NOT NULL,
  sort_order INTEGER DEFAULT 0,
  UNIQUE(group_id, subject_id, kind_key));
""")


    # --- v5: куратор, зам. старосты, замечания, расширенные данные студента ---
    if not setting(c, "v5"):
        cols_g = [r[1] for r in c.execute("PRAGMA table_info(groups)")]
        if "curator_id" not in cols_g:
            c.execute("ALTER TABLE groups ADD COLUMN curator_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
        if "deputy_head_id" not in cols_g:
            c.execute("ALTER TABLE groups ADD COLUMN deputy_head_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
        cols_u = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        for col, typ in [
            ("email", "TEXT DEFAULT ''"),
            ("phone", "TEXT DEFAULT ''"),
            ("birth_date", "TEXT DEFAULT ''"),
            ("student_id_number", "TEXT DEFAULT ''"),
            ("parent_name", "TEXT DEFAULT ''"),
            ("parent_phone", "TEXT DEFAULT ''"),
            ("address", "TEXT DEFAULT ''"),
            ("notes", "TEXT DEFAULT ''"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
        ]:
            if col not in cols_u:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
        c.execute("""CREATE TABLE IF NOT EXISTS journal_remarks(
            id INTEGER PRIMARY KEY,
            student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
            lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
            author_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            created TEXT NOT NULL
        )""")
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('v5','1')")

    try:
        cleanup_bogus_bells(c)
    except Exception:
        pass
    # убрать фантомные пары (номера кабинетов, попавшие в звонки)
    for b in c.execute("SELECT id,num,start,end FROM bells").fetchall():
        if b["num"] > 12 or b["num"] < 1:
            used = c.execute("SELECT 1 FROM schedule WHERE bell_id=? LIMIT 1", (b["id"],)).fetchone()
            if not used:
                c.execute("DELETE FROM bells WHERE id=?", (b["id"],))
        elif b["start"] == "00:00" and b["end"] == "00:00":
            defaults = {1:("09:00","10:35"),2:("10:55","12:30"),3:("13:10","14:45"),
                        4:("14:55","16:30"),5:("16:40","18:15"),6:("18:25","20:00")}
            if b["num"] in defaults:
                s, e = defaults[b["num"]]
                c.execute("UPDATE bells SET start=?, end=? WHERE id=?", (s, e, b["id"]))
            else:
                used = c.execute("SELECT 1 FROM schedule WHERE bell_id=? LIMIT 1", (b["id"],)).fetchone()
                if not used:
                    c.execute("DELETE FROM bells WHERE id=?", (b["id"],))
    c.commit()
    sync_lessons(c)
    c.close()

def seed_marks():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; random.seed(1)
    studs, cnt = {}, {}
    for u in c.execute("SELECT id, group_id FROM users WHERE role='student'"):
        studs.setdefault(u["group_id"], []).append(u["id"])
    topics = ["Введение", "Основные понятия", "Практическое занятие", "Разбор задач", "Контрольная работа"]
    for l in c.execute("SELECT id, group_id, subject_id FROM lessons ORDER BY group_id, subject_id, day").fetchall():
        k = (l["group_id"], l["subject_id"]); n = cnt[k] = cnt.get(k, -1) + 1
        c.execute("UPDATE lessons SET topic=? WHERE id=?", (topics[n % 5], l["id"]))
        for s in studs.get(l["group_id"], []):
            p = int(random.random() < .85)
            c.execute("INSERT INTO marks VALUES(?,?,?,?)", (s, l["id"], p, random.choice([2, 3, 4, 4, 5, 5]) if p and random.random() < .6 else None))
    for grp in c.execute("SELECT id FROM groups").fetchall():  # демо: староста, график дежурств
        first = c.execute("SELECT id FROM users WHERE role='student' AND group_id=? ORDER BY id LIMIT 1", (grp["id"],)).fetchone()
        if first:
            c.execute("UPDATE groups SET head_id=? WHERE id=?", (first["id"], grp["id"]))
            gen_duty(c, grp["id"], date.today(), 10, 1, "Дежурный по группе")
    for a in c.execute("SELECT group_id, subject_id, MIN(teacher_id) t FROM assignments GROUP BY group_id, subject_id").fetchall():
        c.execute("INSERT INTO homework(group_id,subject_id,author_id,title,body,due,created) VALUES(?,?,?,?,?,?,?)",
                  (a["group_id"], a["subject_id"], a["t"], "Практическая работа №1", "Выполнить задания из методички.",
                   (date.today() + timedelta(days=5)).isoformat(), date.today().isoformat()))
    c.commit(); c.close()

def setup():
    fresh = init_db()
    migrate()
    if fresh: seed_marks()

@app.context_processor
def inject():
    unread = 0
    if session.get("uid"):
        try:
            r = db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0",
                             (session["uid"],)).fetchone()
            unread = r[0] if r else 0
        except Exception:
            unread = 0
    return dict(now=datetime.now(), WDF=WDF, notif_unread=unread)

# --- re-export services (совместимость: from core import stats, role, …) ---
from services.auth import role
from services.common import setting, minutes, gap_label
from services.journal import (
    stats, grant_journal_access, has_journal_access,
    course, load_journal, fmt_period, period_for, get_final_kinds, _seed_final_kinds,
)
from services.schedule import overlap, occurrences, sync_lessons, feed, bell_rows, scope
from services.notify import notify, notify_many
from services.attendance import can_fill_attendance, head_group, gen_duty
from services.bonus import _ensure_bonus_tables
from services.announcements import announcements

