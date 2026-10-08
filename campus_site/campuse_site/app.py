import os, random, sqlite3
import csv, io
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, Response, g, render_template, request, redirect, url_for, session, jsonify, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import re
import tempfile

_ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(_ROOT, "campus.db")
UPLOADS = os.path.join(_ROOT, "uploads")
os.makedirs(UPLOADS, exist_ok=True)
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".rar"}
app = Flask(__name__, template_folder=os.path.join(_ROOT, "templates"), static_folder=os.path.join(_ROOT, "static"))
app.secret_key = os.environ.get("SECRET_KEY", "campus-dev-change-me-before-prod")

# Связи: преподаватель -> назначение (группа + предмет); занятия и отметки общие для группы и предмета
SCHEMA = """
CREATE TABLE groups(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE subjects(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE users(id INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL, pw TEXT NOT NULL, name TEXT NOT NULL,
  role TEXT NOT NULL, group_id INTEGER REFERENCES groups(id));
CREATE TABLE assignments(id INTEGER PRIMARY KEY,
  teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE, UNIQUE(teacher_id, group_id, subject_id));
CREATE TABLE lessons(id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  day TEXT NOT NULL, topic TEXT DEFAULT '', teacher_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  UNIQUE(group_id, subject_id, day));
CREATE TABLE marks(student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
  present INTEGER, grade INTEGER, PRIMARY KEY(student_id, lesson_id));
CREATE TABLE announcements(id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE, title TEXT NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL);
"""

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

def role(*rs):
    def deco(f):
        @wraps(f)
        def wrap(*a, **k):
            if session.get("role") not in rs:
                return redirect(url_for("login"))
            return f(*a, **k)
        return wrap
    return deco

def stats(cells):
    """present: 1=был, 0=не был, 2=уваж.
    Часы пропусков считаются ТОЛЬКО по неуважительным (present=0).
    1 пара = 95 минут."""
    PAIR_MIN = 95
    gr = [c["grade"] for c in cells if c and c["grade"]]
    rec = [c for c in cells if c and c["present"] is not None]
    came = sum(1 for c in rec if c["present"] == 1)
    excused = sum(1 for c in rec if c["present"] == 2)
    unexcused = sum(1 for c in rec if c["present"] == 0)
    missed = excused + unexcused  # все отсутствия (для колонки «пропуски»)
    # часы — только без уважительной
    missed_min = unexcused * PAIR_MIN
    missed_h = round(missed_min / 60, 1)
    return dict(avg=round(sum(gr) / len(gr), 2) if gr else None, missed=missed,
                excused=excused, unexcused=unexcused, present=came,
                missed_min=missed_min, missed_hours=missed_h,
                pct=round(came * 100 / len(rec)) if rec else None)

@app.route("/")
def index():
    r = session.get("role")
    return redirect(url_for(r) if r else url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE login=?", (request.form["login"].strip(),)).fetchone()
        if u and check_password_hash(u["pw"], request.form["password"]):
            session.clear(); session.update(uid=u["id"], role=u["role"], name=u["name"])
            return redirect(url_for("index"))
        err = "Неверный логин или пароль. Проверьте данные и попробуйте снова."
    return render_template("login.html", err=err)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
WDF = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]

class Bad(Exception):
    pass

# Расписание: звонки, недельный шаблон (в т.ч. чётные/нечётные недели и разовые занятия), замены и отмены
NEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS bells(id INTEGER PRIMARY KEY, num INTEGER UNIQUE NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedule(id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  bell_id INTEGER NOT NULL REFERENCES bells(id) ON DELETE CASCADE,
  weekday INTEGER, day TEXT, room TEXT DEFAULT '', parity INTEGER NOT NULL DEFAULT 0,
  teachers_extra TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY,
  schedule_id INTEGER NOT NULL REFERENCES schedule(id) ON DELETE CASCADE, day TEXT NOT NULL, kind TEXT NOT NULL,
  teacher_id INTEGER REFERENCES users(id) ON DELETE SET NULL, room TEXT DEFAULT '', note TEXT DEFAULT '', UNIQUE(schedule_id, day));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS duties(id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE, day TEXT NOT NULL,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, task TEXT NOT NULL DEFAULT 'Дежурный по группе', UNIQUE(group_id, day, student_id));
CREATE TABLE IF NOT EXISTS homework(id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE, author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  title TEXT NOT NULL, body TEXT DEFAULT '', due TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hw_done(hw_id INTEGER NOT NULL REFERENCES homework(id) ON DELETE CASCADE,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY(hw_id, student_id));
CREATE TABLE IF NOT EXISTS hw_attach(id INTEGER PRIMARY KEY,
  hw_id INTEGER NOT NULL REFERENCES homework(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hw_notes(hw_id INTEGER NOT NULL REFERENCES homework(id) ON DELETE CASCADE,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body TEXT DEFAULT '', updated TEXT NOT NULL, PRIMARY KEY(hw_id, student_id));
CREATE TABLE IF NOT EXISTS bonus_points(
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  balance INTEGER NOT NULL DEFAULT 0,
  conditions TEXT NOT NULL DEFAULT '',
  updated TEXT,
  PRIMARY KEY(student_id, subject_id));
CREATE TABLE IF NOT EXISTS bonus_applications(
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
  points_used INTEGER NOT NULL,
  original_grade INTEGER NOT NULL,
  new_grade INTEGER NOT NULL,
  applied_at TEXT NOT NULL,
  cancelled_at TEXT,
  cancelled_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  UNIQUE(student_id, lesson_id));
CREATE TABLE IF NOT EXISTS bonus_rules(
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  conditions TEXT NOT NULL DEFAULT '',
  updated TEXT,
  PRIMARY KEY(group_id, subject_id));
CREATE TABLE IF NOT EXISTS bonus_log(
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  teacher_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal_access(
  teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  level TEXT NOT NULL DEFAULT 'edit',
  granted_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created TEXT,
  PRIMARY KEY(teacher_id, group_id, subject_id));
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
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  body TEXT DEFAULT '',
  link TEXT DEFAULT '',
  created TEXT NOT NULL,
  read INTEGER NOT NULL DEFAULT 0);

"""

def setting(c, key):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else None

def minutes(t):
    h, m = map(int, t.split(":"))
    return h * 60 + m

def gap_label(m):
    return f"перерыв {m} мин" if m <= 40 else f"окно {m // 60} ч {m % 60} мин" if m % 60 else f"окно {m // 60} ч"

def overlap(a, b):
    """Пересекаются ли две записи расписания (по дню недели, чётности или конкретной дате)."""
    if a["day"] and b["day"]:
        return a["day"] == b["day"]
    if a["day"] or b["day"]:
        o, w = (a, b) if a["day"] else (b, a)
        dd = date.fromisoformat(o["day"])
        return w["weekday"] == dd.weekday() and w["parity"] in (0, 1 if dd.isocalendar()[1] % 2 else 2)
    return a["weekday"] == b["weekday"] and (a["parity"] == 0 or b["parity"] == 0 or a["parity"] == b["parity"])

def occurrences(c, start, end, gid=None, tid=None):
    """Реальные занятия за период: шаблон недели + разовые + замены/отмены.
    На одну группу+пару+день — не больше одного занятия (разовое перекрывает недельное)."""
    bells = {b["id"]: b for b in c.execute("SELECT * FROM bells")}
    rows = c.execute("""SELECT s.*, g.name gname, sub.name sname, t.name tname FROM schedule s
      JOIN groups g ON g.id=s.group_id JOIN subjects sub ON sub.id=s.subject_id JOIN users t ON t.id=s.teacher_id""").fetchall()
    ch = {(x["schedule_id"], x["day"]): x for x in c.execute(
        "SELECT c.*, u.name tname FROM changes c LEFT JOIN users u ON u.id=c.teacher_id")}
    out, d = [], start
    while d <= end:
        iso, par = d.isoformat(), 1 if d.isocalendar()[1] % 2 else 2
        # собираем кандидатов, ключ (group_id, bell_id) — приоритет у записи с day=iso
        slot = {}  # (gid, bell_id) -> (priority, row_dict_builder_args)
        for s in rows:
            if s["day"]:
                if s["day"] != iso:
                    continue
                prio = 0  # разовое важнее
            else:
                if s["weekday"] != d.weekday() or s["parity"] not in (0, par):
                    continue
                prio = 1
            if gid and s["group_id"] != gid:
                continue
            key = (s["group_id"], s["bell_id"])
            prev = slot.get(key)
            if prev is not None and prev[0] <= prio:
                continue
            slot[key] = (prio, s)

        for _prio, s in slot.values():
            x, b = ch.get((s["id"], iso)), bells.get(s["bell_id"])
            if not b:
                continue
            rep_ = bool(x and x["kind"] == "replace")
            eff = x["teacher_id"] if rep_ and x["teacher_id"] else s["teacher_id"]
            if tid and tid not in (s["teacher_id"], eff):
                continue
            base_tname = s["tname"]
            extra = ""
            try:
                extra = (s["teachers_extra"] or "").strip()
            except (KeyError, IndexError, TypeError):
                extra = ""
            if extra:
                extras = [e.strip() for e in extra.split(",") if e.strip() and e.strip().lower() != (base_tname or "").lower()]
                if extras:
                    base_tname = base_tname + ", " + ", ".join(extras)
            orig_sname = s["sname"]
            disp_sname = orig_sname
            disp_tname = base_tname
            note = ""
            room = s["room"] or ""
            status = "ok"
            chid = None
            instead_of = ""
            if x:
                status = (x["kind"] or "ok").strip() or "ok"
                # room-only change is not a replacement
                if status in ("ok", "room", ""):
                    status = "ok"
                chid = x["id"]
                note = (x["note"] or "").strip()
                if x["room"]:
                    room = x["room"]
                if status == "replace":
                    new_subj, new_teach = None, None
                    if note and note.lower() not in ("отмена", "отм", "кабинет"):
                        parts = [p.strip() for p in note.split(" · ") if p.strip()]
                        if parts:
                            looks_like_teachers = bool(re.search(
                                r"[А-ЯЁA-Z][а-яёa-z\-]*\s+[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.", parts[0]))
                            if not looks_like_teachers:
                                new_subj = parts[0]
                                if len(parts) > 1:
                                    new_teach = " · ".join(parts[1:])
                            else:
                                new_teach = " · ".join(parts)
                    if x["tname"] and not new_teach:
                        new_teach = x["tname"]
                    if new_subj:
                        disp_sname = new_subj
                    if new_teach:
                        disp_tname = new_teach
                    changed = []
                    if new_subj and new_subj.lower() != (orig_sname or "").lower():
                        changed.append(orig_sname)
                        if base_tname:
                            changed.append(base_tname)
                    elif new_teach and new_teach.lower() != (base_tname or "").lower():
                        if base_tname:
                            changed.append(base_tname)
                    if changed:
                        instead_of = ", ".join(changed)
                elif status == "cancel":
                    note = note or "отмена"
            out.append(dict(id=s["id"], day=iso, weekday=d.weekday(), num=b["num"], start=b["start"], end=b["end"],
                gid=s["group_id"], sid=s["subject_id"], gname=s["gname"], sname=disp_sname,
                orig_sname=orig_sname, tid=eff,
                tname=disp_tname, orig_tname=base_tname,
                room=room, status=status, note=note, chid=chid,
                teachers_extra=extra, instead_of=instead_of))
        d += timedelta(days=1)
    out.sort(key=lambda o: (o["day"], o["start"], o.get("gname") or "", o.get("num") or 0))
    return out


def sync_lessons(c, gid=None):
    """Создаёт занятия в журналах по расписанию (с начала семестра по сегодня), отменённые пропускает."""
    ts = setting(c, "term_start")
    start, end = date.fromisoformat(ts) if ts else date.today(), date.today()
    if start > end: return
    for o in occurrences(c, start, end, gid=gid):
        if o["status"] != "cancel":
            c.execute("""INSERT INTO lessons(group_id,subject_id,day,teacher_id) VALUES(?,?,?,?)
              ON CONFLICT(group_id,subject_id,day) DO UPDATE SET teacher_id=excluded.teacher_id""",
                      (o["gid"], o["sid"], o["day"], o["tid"]))
    c.commit()

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
    for g in c.execute("SELECT id FROM groups").fetchall():  # демо: староста, график дежурств
        first = c.execute("SELECT id FROM users WHERE role='student' AND group_id=? ORDER BY id LIMIT 1", (g["id"],)).fetchone()
        if first:
            c.execute("UPDATE groups SET head_id=? WHERE id=?", (first["id"], g["id"]))
            gen_duty(c, g["id"], date.today(), 10, 1, "Дежурный по группе")
    for a in c.execute("SELECT group_id, subject_id, MIN(teacher_id) t FROM assignments GROUP BY group_id, subject_id").fetchall():
        c.execute("INSERT INTO homework(group_id,subject_id,author_id,title,body,due,created) VALUES(?,?,?,?,?,?,?)",
                  (a["group_id"], a["subject_id"], a["t"], "Практическая работа №1", "Выполнить задания из методички.",
                   (date.today() + timedelta(days=5)).isoformat(), date.today().isoformat()))
    c.commit(); c.close()

def setup():
    fresh = init_db()
    migrate()
    if fresh: seed_marks()

def gen_duty(c, gid, start, days, per, task, skip_no_lessons=True):
    """Автоматический график дежурств: по кругу по алфавиту, продолжая с того, кто дежурил последним.
    skip_no_lessons=True — не назначать на дни, когда у группы нет занятий (по расписанию/урокам)."""
    studs = [r[0] for r in c.execute("SELECT id FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,))]
    if not studs:
        return
    last = c.execute(
        "SELECT student_id FROM duties WHERE group_id=? AND day<? ORDER BY day DESC, id DESC LIMIT 1",
        (gid, start.isoformat()),
    ).fetchone()
    i = (studs.index(last[0]) + 1) % len(studs) if last and last[0] in studs else 0
    n, day, guard = 0, start, 0
    while n < days and guard < 400:
        guard += 1
        if day.weekday() < 5:
            has_lessons = True
            if skip_no_lessons:
                # есть ли занятия в этот день (журнал или расписание на дату)
                has_lessons = bool(
                    c.execute(
                        "SELECT 1 FROM lessons WHERE group_id=? AND day=? LIMIT 1",
                        (gid, day.isoformat()),
                    ).fetchone()
                )
                if not has_lessons:
                    try:
                        occ = occurrences(c, day, day, gid=gid)
                        has_lessons = any(o.get("status") != "cancel" for o in (occ or []))
                    except Exception:
                        has_lessons = True  # если не удалось проверить — не блокируем
            if has_lessons:
                for _ in range(min(per, len(studs))):
                    c.execute(
                        "INSERT OR IGNORE INTO duties(group_id,day,student_id,task) VALUES(?,?,?,?)",
                        (gid, day.isoformat(), studs[i % len(studs)], task),
                    )
                    i += 1
                n += 1
        day += timedelta(days=1)

def feed(c, gid=None, tid=None):
    t = date.today()
    week = occurrences(c, t, t + timedelta(days=7), gid=gid, tid=tid)
    return dict(today_items=[o for o in week if o["day"] == t.isoformat()], changes_items=[o for o in week if o["status"] != "ok"])

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

def notify(user_id, title, body="", link=""):
    """Создаёт внутрисайтовое уведомление пользователю."""
    if not user_id:
        return
    d = db()
    d.execute(
        "INSERT INTO notifications(user_id, title, body, link, created, read) VALUES(?,?,?,?,?,0)",
        (user_id, title[:200], (body or "")[:500], (link or "")[:300],
         datetime.now().strftime("%d.%m.%Y %H:%M")))
    d.commit()

def notify_many(user_ids, title, body="", link=""):
    for uid in set(user_ids or []):
        notify(uid, title, body, link)

def grant_journal_access(teacher_id, group_id, subject_id, level="edit", granted_by=None):
    d = db()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    d.execute(
        """INSERT INTO journal_access(teacher_id, group_id, subject_id, level, granted_by, created)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(teacher_id, group_id, subject_id) DO UPDATE SET
             level=excluded.level, granted_by=excluded.granted_by, created=excluded.created""",
        (teacher_id, group_id, subject_id, level, granted_by, now))
    d.commit()

def has_journal_access(teacher_id, group_id, subject_id, need="view", lesson_day=None):
    """Постоянный: assignment (group+subject), journal_access, куратор ЭТОЙ группы.
    Временный: schedule/changes на конкретный день (замена)."""
    d = db()
    u = d.execute("SELECT role FROM users WHERE id=?", (teacher_id,)).fetchone()
    role = u["role"] if u else None
    if role == "admin":
        return True
    # куратор только своей группы
    if d.execute("SELECT 1 FROM groups WHERE id=? AND curator_id=?", (group_id, teacher_id)).fetchone():
        return True
    row = d.execute(
        "SELECT level FROM journal_access WHERE teacher_id=? AND group_id=? AND subject_id=?",
        (teacher_id, group_id, subject_id)).fetchone()
    if row:
        return True if need == "view" else (row["level"] == "edit")
    # постоянное назначение: строго group + subject
    if d.execute("SELECT 1 FROM assignments WHERE teacher_id=? AND group_id=? AND subject_id=?",
                 (teacher_id, group_id, subject_id)).fetchone():
        grant_journal_access(teacher_id, group_id, subject_id, "edit")
        return True
    # временный доступ по дню
    if lesson_day:
        if d.execute("""SELECT 1 FROM schedule WHERE teacher_id=? AND group_id=? AND subject_id=? AND day=?""",
                     (teacher_id, group_id, subject_id, lesson_day)).fetchone():
            return True
        if d.execute("""SELECT 1 FROM changes c JOIN schedule s ON s.id=c.schedule_id
                        WHERE c.teacher_id=? AND s.group_id=? AND s.subject_id=? AND c.day=? AND c.kind='replace'""",
                     (teacher_id, group_id, subject_id, lesson_day)).fetchone():
            return True
        return False
    # без дня: есть ли вообще временные слоты (открыть журнал можно, править — с lesson_day)
    if d.execute("""SELECT 1 FROM schedule WHERE teacher_id=? AND group_id=? AND subject_id=?
                    AND day IS NOT NULL AND day!='' LIMIT 1""", (teacher_id, group_id, subject_id)).fetchone():
        return True
    if d.execute("""SELECT 1 FROM changes c JOIN schedule s ON s.id=c.schedule_id
                    WHERE c.teacher_id=? AND s.group_id=? AND s.subject_id=? AND c.kind='replace' LIMIT 1""",
                 (teacher_id, group_id, subject_id)).fetchone():
        return True
    return False

def can_fill_attendance(user_id, group_id):
    """Староста и зам. старосты могут заполнять посещаемость своей группы."""
    d = db()
    g = d.execute("SELECT head_id, deputy_head_id, curator_id FROM groups WHERE id=?", (group_id,)).fetchone()
    if not g:
        return False
    if user_id in (g["head_id"], g["deputy_head_id"], g["curator_id"]):
        return True
    u = d.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    return u and u["role"] in ("admin", "teacher", "curator")


# ---------- Общее ----------
def announcements(where="", args=()):
    d = db()
    # fallback if migrate not yet applied
    try:
        d.execute("SELECT target_user_id FROM announcements LIMIT 0")
        has_tu = True
    except Exception:
        has_tu = False
    if has_tu:
        q = """SELECT a.*, u.name author, g.name gname, tu.name target_name
          FROM announcements a JOIN users u ON u.id=a.author_id
          LEFT JOIN groups g ON g.id=a.group_id
          LEFT JOIN users tu ON tu.id=a.target_user_id """ + where + " ORDER BY a.id DESC LIMIT 20"
    else:
        # strip target_user_id from where clause for old schema
        where2 = where.replace("a.target_user_id IS NULL AND ", "").replace(" OR a.target_user_id=?", "")
        q = """SELECT a.*, u.name author, g.name gname, NULL as target_name
          FROM announcements a JOIN users u ON u.id=a.author_id
          LEFT JOIN groups g ON g.id=a.group_id """ + where2 + " ORDER BY a.id DESC LIMIT 20"
        if where2 != where:
            # drop last arg if we removed target filter
            args = args[:-1] if args and len(args) >= 2 else args
    return d.execute(q, args).fetchall()

@app.post("/announce")
def announce():
    r, d, f = session.get("role"), db(), request.form
    if r not in ("teacher", "admin"): abort(403)
    gid = f.get("group_id") or None
    target_user = f.get("user_id") or None  # личное объявление
    if r == "teacher" and not target_user:
        ok = False
        if gid:
            ok = bool(d.execute(
                """SELECT 1 FROM assignments WHERE teacher_id=? AND group_id=?
                   UNION SELECT 1 FROM groups WHERE id=? AND curator_id=?
                   UNION SELECT 1 FROM journal_access WHERE teacher_id=? AND group_id=?""",
                (session["uid"], gid, gid, session["uid"], session["uid"], gid)).fetchone())
        if not ok:
            abort(403)
    title, body = f["title"].strip(), f["body"].strip()
    if title and body:
        tuid = int(target_user) if target_user else None
        try:
            d.execute("INSERT INTO announcements(author_id,group_id,title,body,created,target_user_id) VALUES(?,?,?,?,?,?)",
                      (session["uid"], None if tuid else gid, title, body, datetime.now().strftime("%d.%m.%Y %H:%M"), tuid))
        except Exception:
            d.execute("INSERT INTO announcements(author_id,group_id,title,body,created) VALUES(?,?,?,?,?)",
                      (session["uid"], None if tuid else gid, title, body, datetime.now().strftime("%d.%m.%Y %H:%M")))
        d.commit(); flash("Объявление опубликовано")
        if tuid:
            notify(tuid, "Личное: " + title, body[:200], url_for("notifications_page"))
        elif gid:
            ids = [x[0] for x in d.execute("SELECT id FROM users WHERE role='student' AND group_id=?", (gid,))]
            notify_many(ids, "Объявление: " + title, body[:200], url_for("index"))
        else:
            ids = [x[0] for x in d.execute("SELECT id FROM users WHERE role='student'")]
            notify_many(ids, "Объявление: " + title, body[:200], url_for("index"))
    return redirect(request.referrer or url_for("index"))

@app.post("/announce/delete")
def announce_del():
    if session.get("role") not in ("teacher", "admin"): abort(403)
    d = db()
    d.execute("DELETE FROM announcements WHERE id=? AND (author_id=? OR ?='admin')", (request.form["id"], session["uid"], session["role"]))
    d.commit()
    return redirect(url_for("index"))

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "uid" not in session: return redirect(url_for("login"))
    if request.method == "POST":
        d, f = db(), request.form
        u = d.execute("SELECT pw FROM users WHERE id=?", (session["uid"],)).fetchone()
        if not check_password_hash(u["pw"], f["old"]): flash("Текущий пароль указан неверно")
        elif len(f["new"]) < 6: flash("Новый пароль должен быть не короче 6 символов")
        elif f["new"] != f["again"]: flash("Пароли не совпадают")
        else:
            d.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(f["new"]), session["uid"])); d.commit()
            flash("Пароль изменён")
        return redirect(url_for("profile"))
    return render_template("profile.html")

# ---------- Преподаватель ----------
@app.route("/teacher")
@role("teacher", "curator", "admin")
def teacher():
    """Постоянные: назначения (предмет→группы) и кураторство (группа→предметы).
    Временные: только дни замены без назначения."""
    d, uid = db(), session["uid"]
    sync_lessons(d)

    # --- permanent ONLY from assignments (не journal_access и не куратор) ---
    assigns = d.execute("""SELECT g.id gid, s.id sid, g.name gname, s.name sname,
        (SELECT COUNT(*) FROM users WHERE group_id=g.id AND role='student') n,
        (SELECT COUNT(*) FROM lessons WHERE group_id=g.id AND subject_id=s.id) l
        FROM assignments a
        JOIN groups g ON g.id=a.group_id JOIN subjects s ON s.id=a.subject_id
        WHERE a.teacher_id=?
        ORDER BY s.name, g.name""", (uid,)).fetchall()

    # явный journal_access, но НЕ для групп, где пользователь куратор (они в блоке кураторства)
    jacc = d.execute("""SELECT g.id gid, s.id sid, g.name gname, s.name sname,
        (SELECT COUNT(*) FROM users WHERE group_id=g.id AND role='student') n,
        (SELECT COUNT(*) FROM lessons WHERE group_id=g.id AND subject_id=s.id) l
        FROM journal_access ja
        JOIN groups g ON g.id=ja.group_id JOIN subjects s ON s.id=ja.subject_id
        WHERE ja.teacher_id=?
          AND (g.curator_id IS NULL OR g.curator_id != ?)
        ORDER BY s.name, g.name""", (uid, uid)).fetchall()

    perm_seen = set()
    by_subject = {}
    for r in list(assigns) + list(jacc):
        key = (r["gid"], r["sid"])
        if key in perm_seen:
            continue
        # не дублировать в «постоянных», если это курируемая группа
        if d.execute("SELECT 1 FROM groups WHERE id=? AND curator_id=?", (r["gid"], uid)).fetchone():
            continue
        perm_seen.add(key)
        by_subject.setdefault(r["sname"], {"sid": r["sid"], "groups": []})
        by_subject[r["sname"]]["groups"].append(dict(r))

    subjects_perm = [{"sname": k, "sid": v["sid"], "groups": v["groups"]}
                     for k, v in sorted(by_subject.items())]

    # --- curator groups ---
    curator_groups = []
    for g in d.execute("SELECT id, name FROM groups WHERE curator_id=? ORDER BY name", (uid,)):
        subs = d.execute("""SELECT DISTINCT s.id sid, s.name sname,
            (SELECT COUNT(*) FROM users WHERE group_id=? AND role='student') n,
            (SELECT COUNT(*) FROM lessons WHERE group_id=? AND subject_id=s.id) l
            FROM subjects s WHERE
              EXISTS(SELECT 1 FROM assignments a WHERE a.group_id=? AND a.subject_id=s.id)
              OR EXISTS(SELECT 1 FROM schedule sch WHERE sch.group_id=? AND sch.subject_id=s.id)
              OR EXISTS(SELECT 1 FROM lessons l WHERE l.group_id=? AND l.subject_id=s.id)
            ORDER BY s.name""", (g["id"], g["id"], g["id"], g["id"], g["id"])).fetchall()
        curator_groups.append({"gid": g["id"], "gname": g["name"], "subjects": [dict(s) for s in subs]})

    # --- temporary ---
    temp_map = {}
    for r in d.execute("""SELECT DISTINCT sch.group_id gid, sch.subject_id sid, g.name gname, s.name sname, sch.day
        FROM schedule sch JOIN groups g ON g.id=sch.group_id JOIN subjects s ON s.id=sch.subject_id
        WHERE sch.teacher_id=? AND sch.day IS NOT NULL AND sch.day!=''""", (uid,)):
        if (r["gid"], r["sid"]) in perm_seen:
            continue
        k = (r["gid"], r["sid"])
        temp_map.setdefault(k, {"gid": r["gid"], "sid": r["sid"], "gname": r["gname"], "sname": r["sname"], "days": set()})
        temp_map[k]["days"].add(r["day"])
    for r in d.execute("""SELECT DISTINCT s.group_id gid, s.subject_id sid, g.name gname, sub.name sname, c.day
        FROM changes c JOIN schedule s ON s.id=c.schedule_id
        JOIN groups g ON g.id=s.group_id JOIN subjects sub ON sub.id=s.subject_id
        WHERE c.teacher_id=? AND c.kind='replace' AND c.day IS NOT NULL""", (uid,)):
        if (r["gid"], r["sid"]) in perm_seen:
            continue
        k = (r["gid"], r["sid"])
        temp_map.setdefault(k, {"gid": r["gid"], "sid": r["sid"], "gname": r["gname"], "sname": r["sname"], "days": set()})
        temp_map[k]["days"].add(r["day"])
    temporary = []
    for v in temp_map.values():
        v["days"] = sorted(v["days"], reverse=True)[:12]
        n = d.execute("SELECT COUNT(*) c FROM users WHERE group_id=? AND role='student'", (v["gid"],)).fetchone()["c"]
        v["n"] = n
        temporary.append(v)
    temporary.sort(key=lambda x: (x["sname"], x["gname"]))

    # группы и студенты для объявлений (назначения + кураторство)
    ann_groups = d.execute("""
        SELECT DISTINCT g.id, g.name FROM groups g
        WHERE g.id IN (SELECT group_id FROM assignments WHERE teacher_id=?)
           OR g.curator_id=?
        ORDER BY g.name""", (uid, uid)).fetchall()
    ann_students = d.execute("""
        SELECT u.id, u.name, g.name gname FROM users u
        LEFT JOIN groups g ON g.id=u.group_id
        WHERE u.role='student' AND (
          u.group_id IN (SELECT group_id FROM assignments WHERE teacher_id=?)
          OR u.group_id IN (SELECT id FROM groups WHERE curator_id=?)
        )
        ORDER BY g.name, u.name""", (uid, uid)).fetchall()
    # личные сообщения: также преподаватели / админы / диспетчеры
    ann_staff = d.execute("""
        SELECT u.id, u.name, u.role FROM users u
        WHERE u.role IN ('teacher','admin','dispatcher','curator') AND u.id != ?
        ORDER BY u.role, u.name""", (uid,)).fetchall()

    return render_template("teacher.html",
                           subjects_perm=subjects_perm,
                           curator_groups=curator_groups,
                           temporary=temporary,
                           anns=announcements("WHERE a.author_id=?", (uid,)), can_del=True,
                           groups=ann_groups, students=ann_students, staff=ann_staff, all_ok=False,
                           **feed(d, tid=uid))


def course(gid, sid, need="edit"):
    """Курс (группа+предмет). Для преподавателя проверяет journal_access (с автовыдачей по назначению/расписанию)."""
    d = db()
    base = d.execute("""SELECT g.id gid, s.id sid, g.name gname, s.name sname
      FROM groups g, subjects s WHERE g.id=? AND s.id=?""", (gid, sid)).fetchone()
    if not base:
        abort(404)
    role = session.get("role")
    if role in ("teacher", "curator"):
        if not has_journal_access(session["uid"], gid, sid, need=need):
            abort(404)
    elif role not in ("admin",):
        abort(404)
    return base

def load_journal(gid, sid):
    d = db(); sync_lessons(d, gid)
    lessons = d.execute("""SELECT l.id, l.day, l.topic, t.name tname FROM lessons l LEFT JOIN users t ON t.id=l.teacher_id
      WHERE l.group_id=? AND l.subject_id=? ORDER BY l.day""", (gid, sid)).fetchall()
    studs = d.execute("SELECT id, name FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,)).fetchall()
    m = {(r["student_id"], r["lesson_id"]): r for r in d.execute(
        "SELECT * FROM marks WHERE lesson_id IN (SELECT id FROM lessons WHERE group_id=? AND subject_id=?)", (gid, sid))}
    _ensure_bonus_tables(d)
    bonuses = {r["student_id"]: r for r in d.execute(
        "SELECT student_id, balance FROM bonus_points WHERE subject_id=?", (sid,))}
    rules = d.execute("SELECT conditions FROM bonus_rules WHERE group_id=? AND subject_id=?", (gid, sid)).fetchone()
    subject_conditions = rules["conditions"] if rules else ""
    apps_raw = d.execute(
        """SELECT id, student_id, lesson_id, points_used, original_grade, new_grade
           FROM bonus_applications WHERE subject_id=? AND cancelled_at IS NULL""", (sid,)).fetchall()
    apps = {(r["student_id"], r["lesson_id"]): dict(r) for r in apps_raw}
    rows = []
    for s in studs:
        cells = [m.get((s["id"], l["id"])) for l in lessons]
        b = bonuses.get(s["id"])
        applied = {int(l["id"]): apps.get((s["id"], l["id"])) for l in lessons}
        rows.append(dict(id=s["id"], name=s["name"], cells=cells,
                         bonus_balance=(b["balance"] if b else 0),
                         applied=applied, **stats(cells)))
    return lessons, rows, subject_conditions

@app.route("/journal/<int:gid>/<int:sid>")
@role("teacher", "curator", "admin")
def journal(gid, sid):
    c = course(gid, sid, need="view")
    can_edit = has_journal_access(session["uid"], gid, sid, need="edit")
    lessons, rows, subject_conditions = load_journal(gid, sid)
    team = ", ".join(r["name"] for r in db().execute("""SELECT t.name FROM assignments a JOIN users t ON t.id=a.teacher_id
      WHERE a.group_id=? AND a.subject_id=? ORDER BY t.name""", (gid, sid)))
    return render_template("journal.html", a=c, lessons=lessons, rows=rows, team=team,
                           subject_conditions=subject_conditions, today=date.today().isoformat(),
                           can_edit=can_edit)

@app.route("/journal/<int:gid>/<int:sid>/export")
@role("teacher", "admin", "curator")
def export(gid, sid):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    c = course(gid, sid, need="view")
    lessons, rows, _ = load_journal(gid, sid)
    team = ", ".join(r["name"] for r in db().execute("""SELECT t.name FROM assignments a JOIN users t ON t.id=a.teacher_id
      WHERE a.group_id=? AND a.subject_id=? ORDER BY t.name""", (gid, sid)))
    wb = Workbook(); ws = wb.active; ws.title = "Журнал"; ws.sheet_view.showGridLines = False
    fill = lambda h: PatternFill("solid", fgColor=h)
    thin = Side(style="thin", color="D9DEEC"); box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws["A1"] = f'{c["gname"]}, {c["sname"]}'; ws["A1"].font = Font(size=16, bold=True, color="14213D")
    ws["A2"] = f"Преподаватели: {team}. Выгружено {datetime.now():%d.%m.%Y %H:%M}"; ws["A2"].font = Font(color="66708C")
    heads = ["Студент", "Средний балл", "Пропуски", "Часы пропусков", "Уваж.", "Посещаемость", "Доп. баллы"] + [f'{l["day"][8:10]}.{l["day"][5:7]}' for l in lessons]
    for i, h in enumerate(heads, 1):
        x = ws.cell(4, i, h); x.font = Font(bold=True, color="FFFFFF"); x.fill = fill("14213D"); x.alignment = center; x.border = box
    ws.cell(5, 1, "Тема занятия")
    for i in range(1, 8 + len(lessons)):
        ws.cell(5, i).border = box; ws.cell(5, i).alignment = center; ws.cell(5, i).font = Font(italic=True, size=9, color="66708C")
    for i, l in enumerate(lessons, 8):
        ws.cell(5, i, l["topic"] or "")
    gfill = {2: "FFC9C9", 3: "FFD84D", 4: "CFE0FF", 5: "B8F0D3"}
    for ri, r in enumerate(rows, 6):
        risk = (r["avg"] and r["avg"] < 3) or (r["pct"] is not None and r["pct"] < 70)
        hours = r.get("missed_hours")
        if hours is None:
            hours = round((r.get("unexcused") or 0) * 95 / 60, 1)
        vals = [r["name"], r["avg"], r["missed"], hours, r.get("excused") or 0,
                r["pct"] / 100 if r["pct"] is not None else None, r.get("bonus_balance") or 0]
        for i, v in enumerate(vals, 1):
            x = ws.cell(ri, i, v); x.border = box
            x.alignment = Alignment(horizontal="left" if i == 1 else "center", vertical="center")
            if risk: x.fill = fill("FFF5F5")
            if i == 3 and v: x.fill = fill("FFE4E4"); x.font = Font(bold=True, color="E5484D")
            if i == 4 and v: x.fill = fill("FFE4E4"); x.font = Font(bold=True, color="E5484D")
            if i == 5 and v: x.fill = fill("FFF3CD"); x.font = Font(bold=True, color="B45309")
            if i == 7 and v: x.fill = fill("FFF8DC"); x.font = Font(bold=True)
        ws.cell(ri, 2).number_format = "0.00"; ws.cell(ri, 6).number_format = "0%"
        for j, cl in enumerate(r["cells"], 8):
            x = ws.cell(ri, j); x.border = box; x.alignment = center
            if cl and cl["grade"]:
                x.value = cl["grade"]; x.fill = fill(gfill[cl["grade"]]); x.font = Font(bold=True)
            elif cl and cl["present"] == 2:
                reason = ""
                try: reason = (cl["excuse"] or "") if "excuse" in cl.keys() else ""
                except Exception: reason = ""
                x.value = ("у: " + reason) if reason else "у"
                x.fill = fill("FFF3CD"); x.font = Font(bold=True, color="B45309")
                if reason:
                    try:
                        from openpyxl.comments import Comment
                        x.comment = Comment(reason, "Campus")
                    except Exception:
                        pass
            elif cl and cl["present"] == 0:
                x.value = "н"; x.fill = fill("FFE4E4"); x.font = Font(bold=True, color="E5484D")
            elif cl and cl["present"] == 1:
                x.value = "•"; x.font = Font(color="1FA971")
    foot = 6 + len(rows)
    ws.cell(foot, 1, "Присутствовало")
    for k in range(len(lessons)):
        ws.cell(foot, 8 + k, sum(1 for r in rows if r["cells"][k] and r["cells"][k]["present"] == 1))
    for i in range(1, 8 + len(lessons)):
        x = ws.cell(foot, i); x.font = Font(bold=True); x.fill = fill("EEF1F8"); x.border = box; x.alignment = center
    # legend
    leg = foot + 2
    ws.cell(leg, 1, "Легенда: • — был; н — не был; у — уважительная. Часы = только «н» × 95 мин / 60 (уваж. не входит).")
    ws.cell(leg, 1).font = Font(color="66708C", size=9)
    ws.column_dimensions["A"].width = 30
    for i in range(2, 8): ws.column_dimensions[L(i)].width = 13
    for i in range(8, 8 + len(lessons)): ws.column_dimensions[L(i)].width = 11
    ws.row_dimensions[4].height = 28; ws.row_dimensions[5].height = 40
    ws.freeze_panes = "H6"; ws.page_setup.orientation = "landscape"
    buf = io.BytesIO(); wb.save(buf)
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=journal_{gid}_{sid}.xlsx"})

@app.post("/api/mark")
@role("teacher", "curator", "admin", "student")
def mark():
    j, d = request.get_json(), db()
    l = d.execute("SELECT id, group_id, subject_id, day FROM lessons WHERE id=?", (j["lesson"],)).fetchone()
    if not l: abort(404)
    day = l["day"] if "day" in l.keys() else None
    is_editor = has_journal_access(session["uid"], l["group_id"], l["subject_id"], need="edit", lesson_day=day)
    is_att = can_fill_attendance(session["uid"], l["group_id"])
    if not is_editor and not is_att:
        abort(403)
    # староста/зам — только посещаемость, не оценки
    if not is_editor and is_att:
        j.pop("grade", None)
    if not d.execute("SELECT 1 FROM users WHERE id=? AND role='student' AND group_id=?", (j["student"], l["group_id"])).fetchone():
        abort(403)
    sid, lid = j["student"], l["id"]
    d.execute("INSERT OR IGNORE INTO marks(student_id,lesson_id) VALUES(?,?)", (sid, lid))
    # ensure excuse column exists
    try:
        d.execute("SELECT excuse FROM marks LIMIT 1")
    except sqlite3.OperationalError:
        d.execute("ALTER TABLE marks ADD COLUMN excuse TEXT DEFAULT ''")
        d.commit()
    grade_set = None
    if "present" in j:
        p = j["present"]
        if p is not None:
            p = int(p)
            if p not in (0, 1, 2):
                abort(400)
        d.execute("UPDATE marks SET present=? WHERE student_id=? AND lesson_id=?", (p, sid, lid))
        if p in (0, 2) or p is None:
            d.execute("UPDATE marks SET grade=NULL WHERE student_id=? AND lesson_id=?", (sid, lid))
        if p != 2:
            d.execute("UPDATE marks SET excuse='' WHERE student_id=? AND lesson_id=?", (sid, lid))
    if "excuse" in j:
        reason = (j.get("excuse") or "").strip()[:300]
        d.execute("UPDATE marks SET excuse=? WHERE student_id=? AND lesson_id=?", (reason, sid, lid))
        if reason and j.get("present") is None:
            # if only excuse sent, set present=2
            d.execute("UPDATE marks SET present=2, grade=NULL WHERE student_id=? AND lesson_id=?", (sid, lid))
    if "grade" in j:
        if j["grade"] not in (None, 2, 3, 4, 5): abort(400)
        d.execute("UPDATE marks SET grade=? WHERE student_id=? AND lesson_id=?", (j["grade"], sid, lid))
        if j["grade"]:
            d.execute("UPDATE marks SET present=1, excuse='' WHERE student_id=? AND lesson_id=?", (sid, lid))
            grade_set = j["grade"]
    d.execute("DELETE FROM marks WHERE student_id=? AND lesson_id=? AND present IS NULL AND grade IS NULL", (sid, lid))
    d.commit()
    if grade_set is not None:
        sub = d.execute("SELECT name FROM subjects WHERE id=?", (l["subject_id"],)).fetchone()
        sname = sub["name"] if sub else "предмет"
        notify(sid, f"Оценка: {grade_set}", f"По предмету «{sname}» выставлена оценка {grade_set}.",
               url_for("student"))
    allm = d.execute("SELECT * FROM marks WHERE student_id=? AND lesson_id IN (SELECT id FROM lessons WHERE group_id=? AND subject_id=?)",
                     (sid, l["group_id"], l["subject_id"])).fetchall()
    cur = d.execute("SELECT present, grade, excuse FROM marks WHERE student_id=? AND lesson_id=?", (sid, lid)).fetchone()
    # stats() тоже содержит ключ present (счётчик) — не смешиваем с present ячейки
    st = stats(allm)
    st.pop("present", None)
    return jsonify(ok=True,
                   present=cur["present"] if cur else None,
                   grade=cur["grade"] if cur else None,
                   excuse=(cur["excuse"] if cur and "excuse" in cur.keys() else "") or "",
                   **st)


@app.post("/api/mark/bulk")
@role("teacher", "curator", "admin", "student")
def mark_bulk():
    """Отметить всех студентов урока одной операцией (избегает SQLite lock при параллельных /api/mark)."""
    j, d = request.get_json() or {}, db()
    lid = j.get("lesson")
    present = j.get("present")
    if lid is None or present is None:
        abort(400)
    try:
        present = int(present)
    except (TypeError, ValueError):
        abort(400)
    if present not in (0, 1):
        abort(400)
    l = d.execute("SELECT id, group_id, subject_id, day FROM lessons WHERE id=?", (lid,)).fetchone()
    if not l:
        abort(404)
    day = l["day"] if "day" in l.keys() else None
    is_editor = has_journal_access(session["uid"], l["group_id"], l["subject_id"], need="edit", lesson_day=day)
    is_att = can_fill_attendance(session["uid"], l["group_id"])
    if not is_editor and not is_att:
        abort(403)
    try:
        d.execute("SELECT excuse FROM marks LIMIT 1")
    except sqlite3.OperationalError:
        d.execute("ALTER TABLE marks ADD COLUMN excuse TEXT DEFAULT ''")
        d.commit()
    studs = d.execute(
        "SELECT id FROM users WHERE role='student' AND group_id=?", (l["group_id"],)
    ).fetchall()
    for st in studs:
        sid = st["id"]
        d.execute("INSERT OR IGNORE INTO marks(student_id,lesson_id) VALUES(?,?)", (sid, lid))
        d.execute(
            "UPDATE marks SET present=?, excuse='' WHERE student_id=? AND lesson_id=?",
            (present, sid, lid),
        )
        if present == 0:
            d.execute(
                "UPDATE marks SET grade=NULL WHERE student_id=? AND lesson_id=?",
                (sid, lid),
            )
    d.commit()
    return jsonify(ok=True, present=present, count=len(studs))


@app.post("/api/lesson")
@role("teacher")
def lesson():
    j = request.get_json(); c = course(j["gid"], j["sid"]); d = db(); act = j["action"]
    if act == "add":
        try:
            d.execute("INSERT INTO lessons(group_id,subject_id,day,topic,teacher_id) VALUES(?,?,?,?,?)",
                      (c["gid"], c["sid"], date.fromisoformat(j["day"]).isoformat(), j.get("topic", "").strip(), session["uid"]))
        except ValueError:
            return jsonify(ok=False, error="Укажите корректную дату"), 400
        except sqlite3.IntegrityError:
            return jsonify(ok=False, error="Занятие на эту дату уже есть"), 409
    else:
        l = d.execute("SELECT id FROM lessons WHERE id=? AND group_id=? AND subject_id=?", (j["lesson"], c["gid"], c["sid"])).fetchone()
        if not l: abort(404)
        if act == "del":
            # Удаление пары только у администратора / диспетчера — преподавателям запрещено
            return jsonify(ok=False, error="Удаление занятия недоступно"), 403
        elif act == "topic":
            d.execute("UPDATE lessons SET topic=? WHERE id=?", (j["topic"].strip(), l["id"]))
        elif act == "all":  # всех — присутствующие (принудительно)
            d.execute(
                "INSERT OR IGNORE INTO marks(student_id,lesson_id) SELECT id,? FROM users WHERE role='student' AND group_id=?",
                (l["id"], c["gid"]),
            )
            d.execute("UPDATE marks SET present=1, excuse='' WHERE lesson_id=?", (l["id"],))
        elif act == "abs_all":  # всех — отсутствующие
            d.execute(
                "INSERT OR IGNORE INTO marks(student_id,lesson_id) SELECT id,? FROM users WHERE role='student' AND group_id=?",
                (l["id"], c["gid"]),
            )
            d.execute(
                "UPDATE marks SET present=0, excuse='', grade=NULL WHERE lesson_id=?",
                (l["id"],),
            )
    d.commit()
    return jsonify(ok=True)


# ---------- Доп. баллы ----------
def _ensure_bonus_tables(d):
    d.execute("""CREATE TABLE IF NOT EXISTS bonus_points(
      student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
      balance INTEGER NOT NULL DEFAULT 0,
      conditions TEXT NOT NULL DEFAULT '',
      updated TEXT,
      PRIMARY KEY(student_id, subject_id))""")
    d.execute("""CREATE TABLE IF NOT EXISTS bonus_applications(
      id INTEGER PRIMARY KEY,
      student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
      lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
      points_used INTEGER NOT NULL,
      original_grade INTEGER NOT NULL,
      new_grade INTEGER NOT NULL,
      applied_at TEXT NOT NULL,
      cancelled_at TEXT,
      cancelled_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
      UNIQUE(student_id, lesson_id))""")
    d.execute("""CREATE TABLE IF NOT EXISTS bonus_rules(
      group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
      subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
      conditions TEXT NOT NULL DEFAULT '',
      updated TEXT,
      PRIMARY KEY(group_id, subject_id))""")
    d.execute("""CREATE TABLE IF NOT EXISTS bonus_log(
      id INTEGER PRIMARY KEY,
      student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
      teacher_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
      delta INTEGER NOT NULL,
      reason TEXT NOT NULL DEFAULT '',
      created TEXT NOT NULL)""")
    # миграция: если conditions были на студенте — перенесём в rules один раз
    try:
        cols = [r[1] for r in d.execute("PRAGMA table_info(bonus_points)")]
        if "conditions" in cols:
            for row in d.execute("SELECT subject_id, conditions FROM bonus_points WHERE conditions IS NOT NULL AND conditions != ''").fetchall():
                # group_id неизвестен здесь — пропускаем автоперенос
                pass
    except Exception:
        pass

@app.post("/api/bonus/set")
@role("teacher")
def bonus_set():
    """Преподаватель выдаёт/меняет баланс доп. баллов. Причина — опционально (не условия)."""
    j = request.get_json(silent=True) or {}
    d = db()
    _ensure_bonus_tables(d)
    try:
        gid, sid = int(j["gid"]), int(j["sid"])
        st = int(j["student"])
        bal = int(j.get("balance", 0))
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="Некорректные данные"), 400
    if not has_journal_access(session["uid"], gid, sid, need="edit"):
        return jsonify(ok=False, error="Нет доступа к этому предмету"), 403
    if not d.execute("SELECT 1 FROM users WHERE id=? AND role='student' AND group_id=?", (st, gid)).fetchone():
        return jsonify(ok=False, error="Студент не найден в группе"), 403
    if bal < 0 or bal > 100:
        return jsonify(ok=False, error="Баланс должен быть от 0 до 100"), 400
    reason = (j.get("reason") or "").strip()[:500]
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    prev = d.execute("SELECT balance FROM bonus_points WHERE student_id=? AND subject_id=?", (st, sid)).fetchone()
    old_bal = prev["balance"] if prev else 0
    delta = bal - old_bal
    d.execute("""INSERT INTO bonus_points(student_id, subject_id, balance, conditions, updated)
      VALUES(?,?,?,?,?) ON CONFLICT(student_id, subject_id) DO UPDATE SET
      balance=excluded.balance, updated=excluded.updated""",
              (st, sid, bal, "", now))
    if delta != 0:
        d.execute("""INSERT INTO bonus_log(student_id, subject_id, teacher_id, delta, reason, created)
          VALUES(?,?,?,?,?,?)""", (st, sid, session["uid"], delta, reason, now))
    d.commit()
    if delta != 0:
        sub = d.execute("SELECT name FROM subjects WHERE id=?", (sid,)).fetchone()
        sname = sub["name"] if sub else "предмет"
        if delta > 0:
            notify(st, f"Доп. баллы: +{delta}",
                   f"По предмету «{sname}» начислено {delta} доп. балл(ов)." + (f" {reason}" if reason else ""),
                   url_for("student"))
        else:
            notify(st, f"Доп. баллы: {delta}",
                   f"По предмету «{sname}» списано {abs(delta)} доп. балл(ов)." + (f" {reason}" if reason else ""),
                   url_for("student"))
    return jsonify(ok=True, balance=bal, delta=delta, reason=reason)

@app.post("/api/bonus/rules")
@role("teacher")
def bonus_rules():
    """Условия получения доп. баллов по предмету (для всей группы) — отдельно от выдачи."""
    j = request.get_json(silent=True) or {}
    d = db()
    _ensure_bonus_tables(d)
    try:
        gid, sid = int(j["gid"]), int(j["sid"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="Некорректные данные"), 400
    if not d.execute("""SELECT 1 FROM assignments WHERE group_id=? AND subject_id=? AND teacher_id=?""",
                     (gid, sid, session["uid"])).fetchone():
        return jsonify(ok=False, error="Нет доступа к этому предмету"), 403
    cond = (j.get("conditions") or "").strip()[:2000]
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    d.execute("""INSERT INTO bonus_rules(group_id, subject_id, conditions, updated)
      VALUES(?,?,?,?) ON CONFLICT(group_id, subject_id) DO UPDATE SET
      conditions=excluded.conditions, updated=excluded.updated""", (gid, sid, cond, now))
    d.commit()
    return jsonify(ok=True, conditions=cond)


@app.post("/api/bonus/apply")
@role("student")
def bonus_apply():
    """Студент применяет доп. баллы к оценке. Отменить может только преподаватель."""
    j = request.get_json(silent=True) or {}
    d = db()
    _ensure_bonus_tables(d)
    try:
        lid = int(j["lesson"])
        points = int(j.get("points", 1))
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="Некорректные данные"), 400
    if points < 1 or points > 10:
        return jsonify(ok=False, error="Укажите целое число баллов от 1 до 10"), 400
    uid = session["uid"]
    row = d.execute("""SELECT m.grade, m.present, l.subject_id, l.group_id, l.day
      FROM marks m JOIN lessons l ON l.id=m.lesson_id
      JOIN users u ON u.id=m.student_id
      WHERE m.student_id=? AND m.lesson_id=? AND u.group_id=l.group_id""", (uid, lid)).fetchone()
    if not row or not row["grade"]:
        return jsonify(ok=False, error="Нет оценки, к которой можно применить баллы"), 400
    if row["grade"] >= 5:
        return jsonify(ok=False, error="Оценка уже максимальная"), 400
    if d.execute("SELECT 1 FROM bonus_applications WHERE student_id=? AND lesson_id=? AND cancelled_at IS NULL",
                 (uid, lid)).fetchone():
        return jsonify(ok=False, error="К этой оценке уже применены доп. баллы"), 409
    bp = d.execute("SELECT balance FROM bonus_points WHERE student_id=? AND subject_id=?",
                   (uid, row["subject_id"])).fetchone()
    bal = bp["balance"] if bp else 0
    if bal < points:
        return jsonify(ok=False, error=f"Недостаточно баллов (доступно {bal})"), 400
    new_g = min(5, row["grade"] + points)
    used = new_g - row["grade"]
    if used < 1:
        return jsonify(ok=False, error="Нечего повышать"), 400
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    if not bp:
        d.execute("""INSERT INTO bonus_points(student_id, subject_id, balance, conditions, updated)
          VALUES(?,?,?,?,?)""", (uid, row["subject_id"], 0, "", now))
    d.execute("UPDATE bonus_points SET balance=balance-?, updated=? WHERE student_id=? AND subject_id=?",
              (used, now, uid, row["subject_id"]))
    # UNIQUE(student_id, lesson_id): переиспользуем отменённую запись, если есть
    prev = d.execute(
        "SELECT id FROM bonus_applications WHERE student_id=? AND lesson_id=?",
        (uid, lid),
    ).fetchone()
    if prev:
        d.execute(
            """UPDATE bonus_applications
               SET subject_id=?, points_used=?, original_grade=?, new_grade=?,
                   applied_at=?, cancelled_at=NULL, cancelled_by=NULL
               WHERE id=?""",
            (row["subject_id"], used, row["grade"], new_g, now, prev["id"]),
        )
    else:
        d.execute(
            """INSERT INTO bonus_applications(student_id,subject_id,lesson_id,points_used,original_grade,new_grade,applied_at)
               VALUES(?,?,?,?,?,?,?)""",
            (uid, row["subject_id"], lid, used, row["grade"], new_g, now),
        )
    d.execute("UPDATE marks SET grade=? WHERE student_id=? AND lesson_id=?", (new_g, uid, lid))
    d.commit()
    allm = d.execute("""SELECT * FROM marks WHERE student_id=? AND lesson_id IN
      (SELECT id FROM lessons WHERE group_id=? AND subject_id=?)""", (uid, row["group_id"], row["subject_id"])).fetchall()
    st = stats(allm)
    st.pop("present", None)  # не конфликтовать с grade-ответом
    return jsonify(ok=True, grade=new_g, used=used, balance=bal - used, **st)

@app.post("/api/bonus/cancel")
@role("teacher")
def bonus_cancel():
    """Преподаватель отменяет применение доп. баллов и возвращает исходную оценку."""
    j = request.get_json(silent=True) or {}
    d = db()
    _ensure_bonus_tables(d)
    try:
        app_id = int(j["id"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="Некорректные данные"), 400
    a = d.execute("SELECT * FROM bonus_applications WHERE id=? AND cancelled_at IS NULL", (app_id,)).fetchone()
    if not a:
        return jsonify(ok=False, error="Запись не найдена"), 404
    les = d.execute("SELECT group_id, subject_id FROM lessons WHERE id=?", (a["lesson_id"],)).fetchone()
    if not les:
        return jsonify(ok=False, error="Занятие не найдено"), 404
    if not d.execute("""SELECT 1 FROM assignments WHERE group_id=? AND subject_id=? AND teacher_id=?""",
                     (les["group_id"], les["subject_id"], session["uid"])).fetchone():
        return jsonify(ok=False, error="Нет доступа"), 403
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    d.execute("UPDATE bonus_applications SET cancelled_at=?, cancelled_by=? WHERE id=?",
              (now, session["uid"], app_id))
    d.execute("UPDATE marks SET grade=? WHERE student_id=? AND lesson_id=?",
              (a["original_grade"], a["student_id"], a["lesson_id"]))
    d.execute("""INSERT INTO bonus_points(student_id, subject_id, balance, conditions, updated)
      VALUES(?,?,?,?,?) ON CONFLICT(student_id, subject_id) DO UPDATE SET
      balance=balance+?, updated=excluded.updated""",
              (a["student_id"], a["subject_id"], a["points_used"], "", now, a["points_used"]))
    d.commit()
    return jsonify(ok=True, grade=a["original_grade"])

# ---------- Студент ----------
@app.route("/student")
@role("student")
def student():
    d, uid = db(), session["uid"]
    _ensure_bonus_tables(d)
    me = d.execute("SELECT u.group_id, g.name gname FROM users u LEFT JOIN groups g ON g.id=u.group_id WHERE u.id=?", (uid,)).fetchone()
    if not me:
        session.clear()
        return redirect(url_for("login"))
    gid = me["group_id"]
    if gid:
        try:
            sync_lessons(d, gid)
        except Exception:
            pass  # расписание могло быть ещё не настроено
    subs = []
    if gid:
        for a in d.execute("""SELECT s.id, s.name sname,
            (SELECT group_concat(t.name, ', ') FROM assignments a2 JOIN users t ON t.id=a2.teacher_id
              WHERE a2.group_id=? AND a2.subject_id=s.id) tname
            FROM subjects s WHERE s.id IN (SELECT subject_id FROM assignments WHERE group_id=?) ORDER BY s.name""",
                           (gid, gid)).fetchall():
            cells = d.execute("""SELECT l.id lid, l.day, l.topic, m.present, m.grade, m.excuse FROM lessons l
                                 LEFT JOIN marks m ON m.lesson_id=l.id AND m.student_id=?
                                 WHERE l.group_id=? AND l.subject_id=? ORDER BY l.day""", (uid, gid, a["id"])).fetchall()
            bp = d.execute("SELECT balance FROM bonus_points WHERE student_id=? AND subject_id=?",
                           (uid, a["id"])).fetchone()
            rules = d.execute("SELECT conditions FROM bonus_rules WHERE group_id=? AND subject_id=?",
                              (gid, a["id"])).fetchone()
            applied = {r["lesson_id"]: dict(r) for r in d.execute(
                "SELECT lesson_id, points_used, original_grade, new_grade FROM bonus_applications WHERE student_id=? AND subject_id=? AND cancelled_at IS NULL",
                (uid, a["id"]))}
            cells2 = []
            for c in cells:
                cd = dict(c)
                cd["applied"] = applied.get(c["lid"])
                cells2.append(cd)
            subs.append(dict(dict(a), cells=cells2,
                             bonus_balance=bp["balance"] if bp else 0,
                             bonus_conditions=rules["conditions"] if rules else "",
                             **stats(cells)))
    allc = [c for s in subs for c in s["cells"]]
    tot = stats(allc)
    today = date.today().isoformat()
    my_duty = d.execute("SELECT day, task FROM duties WHERE student_id=? AND day>=? ORDER BY day LIMIT 3", (uid, today)).fetchall() if gid else []
    hw = []
    if gid:
        hw = d.execute("""SELECT h.title, h.due, s.name sname FROM homework h JOIN subjects s ON s.id=h.subject_id WHERE h.group_id=?
          AND NOT EXISTS(SELECT 1 FROM hw_done x WHERE x.hw_id=h.id AND x.student_id=?) ORDER BY h.due LIMIT 3""", (gid, uid)).fetchall()
    # Итоговые аттестации студента
    my_finals = []
    if gid:
        for a in d.execute("""SELECT s.id, s.name sname FROM subjects s
            WHERE s.id IN (SELECT subject_id FROM assignments WHERE group_id=?) ORDER BY s.name""", (gid,)).fetchall():
            kinds = get_final_kinds(d, gid, a["id"])
            grades = d.execute("SELECT kind, grade, note, updated FROM final_grades WHERE student_id=? AND subject_id=?",
                               (uid, a["id"])).fetchall()
            by_k = {g["kind"]: dict(g) for g in grades}
            items = []
            for k, lab in kinds.items():
                g = by_k.get(k)
                if g and g.get("grade"):
                    per = period_for(d, gid, a["id"], k)
                    items.append({
                        "kind": k, "label": lab, "grade": g["grade"],
                        "note": g.get("note") or "", "updated": g.get("updated") or "",
                        "period": per["label"],
                    })
            if items:
                my_finals.append({"sname": a["sname"], "atts": items})
    # Фильтр объявлений: глобальные + своей группы + личные мне
    ann_where = "WHERE (a.target_user_id IS NULL AND (a.group_id IS NULL OR a.group_id=?)) OR a.target_user_id=?"
    is_head = bool(gid and d.execute("SELECT 1 FROM groups WHERE id=? AND head_id=?", (gid, uid)).fetchone())
    is_deputy = bool(gid and d.execute("SELECT 1 FROM groups WHERE id=? AND deputy_head_id=?", (gid, uid)).fetchone())
    return render_template("student.html", my_duty=my_duty, hw=hw, subs=subs, grp=me["gname"], avg=tot["avg"] or 0, pct=tot["pct"] or 0,
                           cnt=sum(1 for c in allc if c.get("grade")), groups=None,
                           my_finals=my_finals,
                           is_head=is_head, is_deputy=is_deputy,
                           missed=tot.get("missed") or 0,
                           missed_hours=tot.get("missed_hours") or 0,
                           **feed(d, gid=gid or -1),
                           anns=announcements(ann_where, (gid, uid)))

# ---------- Администратор ----------
@app.route("/admin")
@role("admin")
def admin():
    d = db(); q = lambda x: d.execute(x).fetchall(); one = lambda x: d.execute(x).fetchone()[0]
    users = q("""SELECT u.*, g.name gname FROM users u LEFT JOIN groups g ON g.id=u.group_id
      ORDER BY CASE u.role WHEN 'admin' THEN 0 WHEN 'dispatcher' THEN 1 WHEN 'teacher' THEN 2 ELSE 3 END, g.name, u.name""")
    sbg = {}
    for u in users:
        if u["role"] == "student" and u["group_id"]: sbg.setdefault(u["group_id"], []).append(u)
    jacc = q("""SELECT ja.teacher_id, ja.group_id, ja.subject_id, ja.level, t.name tname, g.name gname, s.name sname
                FROM journal_access ja
                JOIN users t ON t.id=ja.teacher_id JOIN groups g ON g.id=ja.group_id JOIN subjects s ON s.id=ja.subject_id
                ORDER BY g.name, s.name, t.name""")
    students_list = [u for u in users if u["role"] == "student"]
    staff_list = [u for u in users if u["role"] in ("teacher", "admin", "dispatcher", "curator")]
    return render_template("admin.html", users=users, sbg=sbg, groups=q("SELECT * FROM groups ORDER BY name"),
        subjects=q("SELECT * FROM subjects ORDER BY name"), teachers=q("SELECT * FROM users WHERE role='teacher' ORDER BY name"),
        assigns=q("""SELECT a.id, g.name gname, s.name sname, t.name tname FROM assignments a JOIN groups g ON g.id=a.group_id
                     JOIN subjects s ON s.id=a.subject_id JOIN users t ON t.id=a.teacher_id ORDER BY g.name, s.name, t.name"""),
        journal_access=jacc,
        counts=dict(groups=one("SELECT COUNT(*) FROM groups"), students=one("SELECT COUNT(*) FROM users WHERE role='student'"),
                    teachers=one("SELECT COUNT(*) FROM users WHERE role='teacher'"), lessons=one("SELECT COUNT(*) FROM lessons")),
        anns=announcements(), can_del=True, all_ok=True, students=students_list, staff=staff_list)

@app.post("/admin/<act>")
@role("admin")
def admin_act(act):
    d, f = db(), request.form
    try:
        if act == "group":
            d.execute("INSERT INTO groups(name) VALUES(?)", (f["name"].strip(),))
        elif act == "subject":
            d.execute("INSERT INTO subjects(name) VALUES(?)", (f["name"].strip(),))
        elif act == "user":
            r, gid = f["role"], f.get("group_id") or None
            if r not in ("teacher", "student", "dispatcher", "curator"): raise ValueError("Выберите роль")
            if r == "student" and not gid: raise ValueError("Для студента выберите группу")
            d.execute("INSERT INTO users(login,pw,name,role,group_id) VALUES(?,?,?,?,?)",
                      (f["login"].strip(), generate_password_hash(f["password"]), f["name"].strip(), r, gid if r == "student" else None))
            if r == "curator" and gid:
                uid = d.execute("SELECT id FROM users WHERE login=?", (f["login"].strip(),)).fetchone()[0]
                d.execute("UPDATE groups SET curator_id=? WHERE id=?", (uid, gid))
        elif act == "assign":
            d.execute("INSERT INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)", (f["teacher_id"], f["group_id"], f["subject_id"]))
            grant_journal_access(int(f["teacher_id"]), int(f["group_id"]), int(f["subject_id"]), "edit", session["uid"])
        elif act == "journal_access":
            tid, gid, sid = int(f["teacher_id"]), int(f["group_id"]), int(f["subject_id"])
            level = f.get("level") or "edit"
            if level not in ("edit", "view"):
                raise ValueError("Уровень: edit или view")
            grant_journal_access(tid, gid, sid, level, session["uid"])
        elif act == "journal_access_del":
            d.execute("DELETE FROM journal_access WHERE teacher_id=? AND group_id=? AND subject_id=?",
                      (f["teacher_id"], f["group_id"], f["subject_id"]))
        elif act == "head":
            sid = f.get("student_id") or None
            if sid and not d.execute("SELECT 1 FROM users WHERE id=? AND group_id=? AND role='student'", (sid, f["group_id"])).fetchone():
                raise ValueError("Староста должен быть студентом этой группы")
            d.execute("UPDATE groups SET head_id=? WHERE id=?", (sid, f["group_id"]))
        elif act == "deputy_head":
            sid = f.get("student_id") or None
            if sid and not d.execute("SELECT 1 FROM users WHERE id=? AND group_id=? AND role='student'", (sid, f["group_id"])).fetchone():
                raise ValueError("Зам. старосты должен быть студентом этой группы")
            d.execute("UPDATE groups SET deputy_head_id=? WHERE id=?", (sid, f["group_id"]))
        elif act == "curator":
            tid = f.get("teacher_id") or None
            if tid and not d.execute("SELECT 1 FROM users WHERE id=? AND role IN ('teacher','curator','admin')", (tid,)).fetchone():
                raise ValueError("Куратор должен быть преподавателем или куратором")
            d.execute("UPDATE groups SET curator_id=? WHERE id=?", (tid or None, f["group_id"]))
            # доступ куратора — через groups.curator_id в has_journal_access, без journal_access
        elif act == "student_info":
            # расширенные данные студента
            uid = int(f["id"])
            d.execute("""UPDATE users SET email=?, phone=?, birth_date=?, student_id_number=?,
                         parent_name=?, parent_phone=?, address=?, notes=? WHERE id=? AND role='student'""",
                      (f.get("email","").strip(), f.get("phone","").strip(), f.get("birth_date","").strip(),
                       f.get("student_id_number","").strip(), f.get("parent_name","").strip(),
                       f.get("parent_phone","").strip(), f.get("address","").strip(),
                       f.get("notes","").strip(), uid))
        elif act == "toggle_active":
            uid = int(f["id"])
            d.execute("UPDATE users SET active=CASE WHEN COALESCE(active,1)=1 THEN 0 ELSE 1 END WHERE id=? AND id!=?",
                      (uid, session["uid"]))
        elif act == "move":
            d.execute("UPDATE users SET group_id=? WHERE id=? AND role='student'", (f.get("group_id") or None, f["id"]))
        elif act == "pw":
            if len(f["password"]) < 6: raise ValueError("Пароль должен быть не короче 6 символов")
            d.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(f["password"]), f["id"]))
        elif act == "del":
            t = {"group": "groups", "subject": "subjects", "user": "users", "assign": "assignments"}[f["kind"]]
            if t == "users" and int(f["id"]) == session["uid"]: raise ValueError("Нельзя удалить самого себя")
            if t == "groups":  # студенты остаются в системе, но без группы
                d.execute("UPDATE users SET group_id=NULL WHERE group_id=?", (f["id"],))
            d.execute(f"DELETE FROM {t} WHERE id=?", (f["id"],))
        d.commit(); flash("Готово")
    except sqlite3.IntegrityError:
        d.rollback(); flash("Не получилось: такая запись уже есть, либо на неё ссылаются (например, в группе остались студенты).")
    except (ValueError, KeyError) as e:
        d.rollback(); flash(str(e))
    return redirect(url_for("admin"))

# ---------- Расписание ----------
def bell_rows(bells):
    out = []
    for i, b in enumerate(bells):
        r = dict(b)
        if i:
            g = minutes(b["start"]) - minutes(bells[i - 1]["end"])
            r["gap_label"] = gap_label(g) if g > 0 else None
        out.append(r)
    return out

def scope(d):
    r = session["role"]
    if r == "student":
        return d.execute("SELECT group_id FROM users WHERE id=?", (session["uid"],)).fetchone()[0] or -1, None, None
    if r == "teacher":
        # группы из назначений и из расписания (чтобы фильтр и ссылки «Журнал» были согласованы)
        my_groups = d.execute("""SELECT DISTINCT g.id, g.name FROM groups g
            WHERE g.id IN (SELECT group_id FROM assignments WHERE teacher_id=?)
               OR g.id IN (SELECT group_id FROM schedule WHERE teacher_id=?)
            ORDER BY g.name""", (session["uid"], session["uid"])).fetchall()
        gid = request.args.get("group", type=int)
        if gid and not any(g["id"] == gid for g in my_groups):
            gid = None
        return gid, session["uid"], my_groups
    return request.args.get("group", type=int), None, d.execute("SELECT * FROM groups ORDER BY name").fetchall()

@app.route("/schedule")
def schedule():
    if "uid" not in session: return redirect(url_for("login"))
    d, off = db(), request.args.get("week", 0, type=int)
    gid, tid, groups = scope(d)
    mon = date.today() - timedelta(days=date.today().weekday()) + timedelta(weeks=off)
    occ = occurrences(d, mon, mon + timedelta(days=5), gid=gid, tid=tid)
    days = []
    for i in range(6):
        day = mon + timedelta(days=i); lst = [o for o in occ if o["day"] == day.isoformat()]
        if gid or tid:
            for x, y in zip(lst, lst[1:]):
                g = minutes(y["start"]) - minutes(x["end"])
                if g > 0: y["gap_label"] = gap_label(g)
        days.append(dict(date=day, lst=lst))
    is_teacher = session.get("role") == "teacher"
    return render_template("schedule.html", days=days, off=off, gid=gid if gid and gid > 0 else None, groups=groups, wd=WDF,
                           today=date.today(), bells=bell_rows(d.execute("SELECT * FROM bells ORDER BY num").fetchall()),
                           is_teacher=is_teacher, show_group_filter=bool(groups))

@app.route("/schedule.xlsx")
def schedule_xlsx():
    """Экспорт расписания на неделю в Excel (сетка + список) с оформлением."""
    if "uid" not in session: return redirect(url_for("login"))
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    d, off = db(), request.args.get("week", 0, type=int)
    gid, tid, groups = scope(d)
    mon = date.today() - timedelta(days=date.today().weekday()) + timedelta(weeks=off)
    occ = occurrences(d, mon, mon + timedelta(days=5), gid=gid, tid=tid)
    bells = d.execute("SELECT * FROM bells ORDER BY num").fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Расписание"
    ws.sheet_view.showGridLines = False
    fill = lambda h: PatternFill("solid", fgColor=h)
    thin = Side(style="thin", color="D9DEEC")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left = Alignment(horizontal="left", vertical="top", wrap_text=True)

    title = "Расписание"
    if gid and groups:
        gname = next((g["name"] for g in groups if g["id"] == gid), None)
        if gname:
            title += f" · {gname}"
    elif session.get("role") == "teacher":
        title += " · мои занятия"
    title += f"  {mon.strftime('%d.%m.%Y')} – {(mon + timedelta(days=5)).strftime('%d.%m.%Y')}"

    ws["A1"] = title
    ws["A1"].font = Font(size=16, bold=True, color="14213D")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=7)
    ws["A2"] = f"Выгружено {datetime.now():%d.%m.%Y %H:%M} · Campus"
    ws["A2"].font = Font(color="66708C", size=10)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=7)

    # Заголовки
    ws.cell(4, 1, "Пара / время").font = Font(bold=True, color="FFFFFF", size=11)
    ws.cell(4, 1).fill = fill("14213D")
    ws.cell(4, 1).alignment = center
    ws.cell(4, 1).border = box
    for i in range(6):
        cell = ws.cell(4, 2 + i, f"{WDF[i]}\n{(mon + timedelta(days=i)).strftime('%d.%m')}")
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.fill = fill("14213D")
        cell.alignment = center
        cell.border = box
    ws.row_dimensions[4].height = 36

    by_day_bell = {}
    for o in occ:
        by_day_bell.setdefault((o["day"], o["num"]), []).append(o)

    for ri, b in enumerate(bells, 5):
        time_cell = ws.cell(ri, 1, f'{b["num"]} пара\n{b["start"]}–{b["end"]}')
        time_cell.font = Font(bold=True, size=10)
        time_cell.alignment = center
        time_cell.border = box
        time_cell.fill = fill("EEF1F8")
        max_lines = 1
        for di in range(6):
            day = (mon + timedelta(days=di)).isoformat()
            items = by_day_bell.get((day, b["num"]), [])
            cell = ws.cell(ri, 2 + di)
            cell.border = box
            cell.alignment = left
            if not items:
                cell.value = ""
                continue
            blocks = []
            for o in items:
                st = ""
                if o["status"] == "cancel":
                    st = " [отменено]"
                elif o["status"] == "replace":
                    st = " [замена]"
                block = f'{o["sname"]}{st}\n{o["gname"]}\n{o["tname"]}\nауд. {o["room"] or "—"}'
                if o.get("note"):
                    block += f'\n{o["note"]}'
                blocks.append(block)
            text = "\n———\n".join(blocks)
            cell.value = text
            max_lines = max(max_lines, text.count("\n") + 1)
            if any(o["status"] == "cancel" for o in items):
                cell.fill = fill("FFE4E4")
            elif any(o["status"] == "replace" for o in items):
                cell.fill = fill("FFFBE8")
            else:
                cell.fill = fill("F5F7FE")
        ws.row_dimensions[ri].height = max(50, 14 * max_lines + 8)

    ws.column_dimensions["A"].width = 14
    for i in range(2, 8):
        ws.column_dimensions[L(i)].width = 22
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.freeze_panes = "B5"

    # Лист «Список»
    ws2 = wb.create_sheet("Список")
    ws2.sheet_view.showGridLines = False
    ws2["A1"] = title
    ws2["A1"].font = Font(size=14, bold=True, color="14213D")
    ws2.merge_cells("A1:J1")
    cols = ["Дата", "День", "Пара", "Время", "Предмет", "Группа", "Преподаватель", "Аудитория", "Статус", "Примечание"]
    for i, h in enumerate(cols, 1):
        x = ws2.cell(3, i, h)
        x.font = Font(bold=True, color="FFFFFF")
        x.fill = fill("14213D")
        x.alignment = center
        x.border = box
    for ri, o in enumerate(sorted(occ, key=lambda x: (x["day"], x["start"])), 4):
        st = {"ok": "ок", "cancel": "отменено", "replace": "замена"}.get(o["status"], o["status"])
        vals = [
            f'{o["day"][8:10]}.{o["day"][5:7]}.{o["day"][:4]}',
            WDF[o["weekday"]],
            o["num"],
            f'{o["start"]}–{o["end"]}',
            o["sname"],
            o["gname"],
            o["tname"],
            o["room"] or "—",
            st,
            o.get("note") or "",
        ]
        for ci, v in enumerate(vals, 1):
            x = ws2.cell(ri, ci, v)
            x.border = box
            x.alignment = center if ci <= 4 else left
            if o["status"] == "cancel":
                x.fill = fill("FFE4E4")
            elif o["status"] == "replace":
                x.fill = fill("FFFBE8")
    for i, w in enumerate([12, 10, 8, 12, 22, 12, 20, 12, 12, 24], 1):
        ws2.column_dimensions[L(i)].width = w
    if occ:
        ws2.auto_filter.ref = f"A3:J{3 + len(occ)}"
    ws2.freeze_panes = "A4"

    buf = io.BytesIO()
    wb.save(buf)
    fname = f"schedule_{mon.strftime('%Y%m%d')}.xlsx"
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ---------- Импорт расписания из файлов ----------
WD_RU = {"понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3, "пятница": 4, "суббота": 5}
WD_SHORT = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5}


# --- import parsers ---
try:
    from services.importers import (
        import_weekly_docx, import_replacements_pdf, import_rooms_pdf,
        ensure_group, ensure_subject, ensure_teacher, ensure_bell,
        _norm_space, _parse_room, _title_case_subject, _split_subj_teacher,
        parse_time_range, _migrate_day_entries_to_changes, cleanup_bogus_bells,
    )
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from services.importers import (
        import_weekly_docx, import_replacements_pdf, import_rooms_pdf,
        ensure_group, ensure_subject, ensure_teacher, ensure_bell,
        _norm_space, _parse_room, _title_case_subject, _split_subj_teacher,
        parse_time_range, _migrate_day_entries_to_changes, cleanup_bogus_bells,
    )

@app.route("/dispatcher/import", methods=["GET", "POST"])
@role("dispatcher", "admin")
def dispatcher_import():
    if request.method == "GET":
        return redirect(url_for("dispatcher"))
    d = db()
    f = request.files.get("file")
    kind = (request.form.get("kind") or "auto").strip()
    day = (request.form.get("day") or "").strip() or None
    if day:
        try:
            day = date.fromisoformat(day).isoformat()
        except Exception:
            day = None
    if not f or not f.filename:
        flash("Выберите файл")
        return redirect(url_for("dispatcher"))
    fname = secure_filename(f.filename)
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix="." + ext)
    try:
        f.save(tmp.name)
        tmp.close()
        if kind == "auto":
            if ext in ("docx",):
                kind = "weekly"
            elif any(x in fname.lower() for x in ("kabinet", "кабинет", "auditori", "аудитор", "rooms")):
                kind = "rooms"
            elif any(x in fname.lower() for x in ("zamen", "замен", "изменен", "change")):
                kind = "replace"
            elif ext == "pdf":
                # без подсказки в имени — спрашиваем через kind в форме; по умолчанию замены
                kind = "replace"
            else:
                flash("Неизвестный тип файла. Используйте DOCX (неделя) или PDF (замены/кабинеты).")
                return redirect(url_for("dispatcher"))
        if kind == "weekly":
            if ext != "docx":
                flash("Недельное расписание: нужен файл .docx")
                return redirect(url_for("dispatcher"))
            st = import_weekly_docx(d, tmp.name)
            try:
                cleanup_bogus_bells(d)
            except Exception:
                pass
            flash(f"Импорт недели: групп {st['groups']}, занятий {st['lessons']}. "
                  f"Новые группы: {len(st['created_groups'])}, предметы: {len(set(st['created_subjects']))}.")
            try:
                sync_lessons(d)
            except Exception:
                pass
        elif kind == "replace":
            st = import_replacements_pdf(d, tmp.name, day)
            flash(f"Импорт замен на {st.get('day') or '?'}: замен {st['replacements']}, отмен {st['cancels']}.")
            try:
                sync_lessons(d)
            except Exception:
                pass
        elif kind == "rooms":
            st = import_rooms_pdf(d, tmp.name, day)
            flash(f"Импорт кабинетов на {st.get('day') or '?'}: кабинетов {st['rooms']}, отмен {st.get('cancels',0)}, "
                  f"групп в файле {st.get('groups_seen',0)}, новых групп {st.get('groups_created',0)}. "
                  f"{'Сначала загрузите недельное расписание — тогда кабинеты привяжутся к парам.' if st['rooms']==0 and st.get('groups_created',0)>=0 else ''}")
        else:
            flash("Неизвестный режим импорта")
    except Exception as e:
        d.rollback()
        flash(f"Ошибка импорта: {e}")
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    return redirect(url_for("dispatcher"))


@app.route("/dispatcher")
@role("dispatcher", "admin")
def dispatcher():
    d = db()
    groups = d.execute("SELECT * FROM groups ORDER BY name").fetchall()
    gid = request.args.get("group", type=int) or (groups[0]["id"] if groups else None)
    view = request.args.get("view") or "week"  # kept for compatibility, UI now shows both
    off = request.args.get("week", 0, type=int)
    mon = date.today() - timedelta(days=date.today().weekday()) + timedelta(weeks=off)
    grid, once, occ, wgrid = {}, [], [], {}
    if gid:
        for e in d.execute("""SELECT s.*, sub.name sname, t.name tname FROM schedule s JOIN subjects sub ON sub.id=s.subject_id
                JOIN users t ON t.id=s.teacher_id WHERE s.group_id=? ORDER BY s.parity""", (gid,)):
            e = dict(e)
            extra = (e.get("teachers_extra") or "").strip()
            if extra:
                extras = [x.strip() for x in extra.split(",") if x.strip() and x.strip().lower() != (e["tname"] or "").lower()]
                if extras:
                    e["tname"] = e["tname"] + ", " + ", ".join(extras)
            if e["day"]: once.append(e)
            else: grid.setdefault((e["weekday"], e["bell_id"]), []).append(e)
        once.sort(key=lambda e: e["day"] or "")
        occ = occurrences(d, mon, mon + timedelta(days=5), gid=gid)
        for o in occ: wgrid.setdefault((o["weekday"], o["num"]), []).append(o)
    return render_template("dispatcher.html", groups=groups, gid=gid, view=view, off=off, grid=grid, once=once, occ=occ, wgrid=wgrid,
        dates=[mon + timedelta(days=i) for i in range(6)], wd=WD, term=setting(d, "term_start"),
        bells=bell_rows(d.execute("SELECT * FROM bells ORDER BY num").fetchall()),
        subjects=d.execute("SELECT * FROM subjects ORDER BY name").fetchall(),
        teachers=d.execute("SELECT id, name FROM users WHERE role='teacher' ORDER BY name").fetchall())

@app.post("/dispatcher/<act>")
@role("dispatcher", "admin")
def dispatcher_act(act):
    d, f = db(), request.form
    gid = f.get("group") or None
    try:
        if act == "bell":
            d.execute("INSERT INTO bells(num,start,end) VALUES(?,?,?)", (int(f["num"]), f["start"], f["end"]))
        elif act == "bell_del":
            d.execute("DELETE FROM bells WHERE id=?", (f["id"],))
        elif act == "term":
            d.execute("INSERT OR REPLACE INTO settings VALUES('term_start',?)", (date.fromisoformat(f["value"]).isoformat(),))
        elif act == "entry":
            new = dict(group_id=int(f["group"]), subject_id=int(f["subject_id"]), teacher_id=int(f["teacher_id"]), bell_id=int(f["bell_id"]),
                       weekday=int(f["weekday"]), day=f.get("day") or None, parity=int(f["parity"]))
            if new["day"]:
                new["day"] = date.fromisoformat(new["day"]).isoformat(); new["weekday"] = None
            for e in d.execute("SELECT * FROM schedule WHERE bell_id=?", (new["bell_id"],)):
                if (e["group_id"] == new["group_id"] or e["teacher_id"] == new["teacher_id"]) and overlap(e, new):
                    raise Bad("В это время у группы или у преподавателя уже есть занятие")
            d.execute("INSERT INTO schedule(group_id,subject_id,teacher_id,bell_id,weekday,day,room,parity) VALUES(?,?,?,?,?,?,?,?)",
                      (new["group_id"], new["subject_id"], new["teacher_id"], new["bell_id"], new["weekday"], new["day"], f.get("room", "").strip(), new["parity"]))
            d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)", (new["teacher_id"], new["group_id"], new["subject_id"]))
        elif act == "entry_del":
            d.execute("DELETE FROM schedule WHERE id=?", (f["id"],))
        elif act == "change":
            s = d.execute("SELECT * FROM schedule WHERE id=?", (f["schedule_id"],)).fetchone()
            if not s: raise Bad("Занятие не найдено")
            kind, day, room = f["kind"], date.fromisoformat(f["day"]).isoformat(), f.get("room", "").strip()
            tid = int(f["teacher_id"]) if f.get("teacher_id") else None
            if kind == "replace" and not (tid or room): raise Bad("Для замены укажите преподавателя или аудиторию")
            rep_ = kind == "replace"
            d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
              ON CONFLICT(schedule_id,day) DO UPDATE SET kind=excluded.kind, teacher_id=excluded.teacher_id, room=excluded.room, note=excluded.note""",
                      (s["id"], day, kind, tid if rep_ else None, room if rep_ else "", f.get("note", "").strip()))
            if kind == "cancel":  # пустое занятие убираем из журнала, с отметками оставляем
                d.execute("""DELETE FROM lessons WHERE group_id=? AND subject_id=? AND day=?
                  AND NOT EXISTS(SELECT 1 FROM marks WHERE lesson_id=lessons.id)""", (s["group_id"], s["subject_id"], day))
            elif tid:  # заменяющий преподаватель получает доступ к журналу группы
                d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)", (tid, s["group_id"], s["subject_id"]))
        elif act == "change_del":
            d.execute("DELETE FROM changes WHERE id=?", (f["id"],))
        elif act == "reset_schedule":
            # scope: all | group ; what: weekly | day | changes | all
            scope = (f.get("scope") or "group").strip()
            what = (f.get("what") or "all").strip()
            day = (f.get("day") or "").strip() or None
            if day:
                try:
                    day = date.fromisoformat(day).isoformat()
                except Exception:
                    day = None
            target_gid = int(f["group"]) if f.get("group") else None
            if scope == "group" and not target_gid:
                raise Bad("Выберите группу для сброса")
            if what == "day" and not day:
                raise Bad("Укажите дату для сброса замен/разовых на день")

            def _sch_filter(sql_extra="", params=()):
                if scope == "group":
                    return sql_extra + (" AND group_id=?" if "group_id" not in sql_extra else ""), params + (target_gid,)
                return sql_extra, params

            if what in ("weekly", "all"):
                # постоянное расписание (weekday, без day)
                if scope == "group":
                    d.execute("DELETE FROM schedule WHERE group_id=? AND weekday IS NOT NULL AND (day IS NULL OR day='')", (target_gid,))
                else:
                    d.execute("DELETE FROM schedule WHERE weekday IS NOT NULL AND (day IS NULL OR day='')")
            if what in ("day", "all"):
                if day:
                    # разовые на день + changes на день
                    if scope == "group":
                        ids = [r["id"] for r in d.execute("SELECT id FROM schedule WHERE group_id=?", (target_gid,))]
                        if ids:
                            q = ",".join("?" * len(ids))
                            d.execute(f"DELETE FROM changes WHERE day=? AND schedule_id IN ({q})", [day] + ids)
                        d.execute("DELETE FROM schedule WHERE group_id=? AND day=?", (target_gid, day))
                    else:
                        d.execute("DELETE FROM changes WHERE day=?", (day,))
                        d.execute("DELETE FROM schedule WHERE day=?", (day,))
                elif what == "all":
                    # все разовые и все замены
                    if scope == "group":
                        ids = [r["id"] for r in d.execute("SELECT id FROM schedule WHERE group_id=?", (target_gid,))]
                        if ids:
                            q = ",".join("?" * len(ids))
                            d.execute(f"DELETE FROM changes WHERE schedule_id IN ({q})", ids)
                        d.execute("DELETE FROM schedule WHERE group_id=? AND day IS NOT NULL AND day!=''", (target_gid,))
                    else:
                        d.execute("DELETE FROM changes")
                        d.execute("DELETE FROM schedule WHERE day IS NOT NULL AND day!=''")
            if what == "changes":
                if day:
                    if scope == "group":
                        ids = [r["id"] for r in d.execute("SELECT id FROM schedule WHERE group_id=?", (target_gid,))]
                        if ids:
                            q = ",".join("?" * len(ids))
                            d.execute(f"DELETE FROM changes WHERE day=? AND schedule_id IN ({q})", [day] + ids)
                    else:
                        d.execute("DELETE FROM changes WHERE day=?", (day,))
                else:
                    if scope == "group":
                        ids = [r["id"] for r in d.execute("SELECT id FROM schedule WHERE group_id=?", (target_gid,))]
                        if ids:
                            q = ",".join("?" * len(ids))
                            d.execute(f"DELETE FROM changes WHERE schedule_id IN ({q})", ids)
                    else:
                        d.execute("DELETE FROM changes")
            if what == "all" and not day:
                # полный сброс слотов
                if scope == "group":
                    d.execute("DELETE FROM schedule WHERE group_id=?", (target_gid,))
                else:
                    d.execute("DELETE FROM schedule")
                    d.execute("DELETE FROM changes")
            flash("Расписание сброшено")
            d.commit()
            return redirect(url_for("dispatcher", group=gid, view=f.get("view") or None, week=f.get("week") or None))
        d.commit(); flash("Готово")
    except Bad as e:
        d.rollback(); flash(str(e))
    except sqlite3.IntegrityError:
        d.rollback(); flash("Такая запись уже существует")
    except (ValueError, KeyError):
        d.rollback(); flash("Проверьте введённые данные")
    return redirect(url_for("dispatcher", group=gid, view=f.get("view") or None, week=f.get("week") or None))

# ---------- Дежурства (староста) ----------
def head_group(uid):
    r = db().execute("SELECT g.id FROM groups g JOIN users u ON u.group_id=g.id WHERE g.head_id=? AND u.id=?", (uid, uid)).fetchone()
    return r[0] if r else None

@app.route("/duty")
@role("student")
def duty():
    d, uid = db(), session["uid"]
    me = d.execute("""SELECT u.group_id, g.name gname, g.head_id, h.name hname FROM users u LEFT JOIN groups g ON g.id=u.group_id
      LEFT JOIN users h ON h.id=g.head_id WHERE u.id=?""", (uid,)).fetchone()
    gid, byday = me["group_id"], {}
    rows = d.execute("""SELECT d.*, u.name sname FROM duties d JOIN users u ON u.id=d.student_id WHERE d.group_id=? AND d.day>=?
      ORDER BY d.day, u.name""", (gid, (date.today() - timedelta(days=7)).isoformat())).fetchall() if gid else []
    for r in rows: byday.setdefault(r["day"], []).append(r)
    studs = d.execute("SELECT id, name FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,)).fetchall() if gid else []
    return render_template("duty.html", me=me, is_head=head_group(uid) is not None, studs=studs, wd=WD, today=date.today().isoformat(),
        duty_days=[dict(day=k, lst=v, wd=date.fromisoformat(k).weekday()) for k, v in byday.items()])

@app.post("/duty/<act>")
@role("student")
def duty_act(act):
    d, f = db(), request.form
    gid = head_group(session["uid"])
    if not gid: abort(403)
    task = f.get("task", "").strip() or "Дежурный по группе"
    try:
        if act == "add":
            if not d.execute("SELECT 1 FROM users WHERE id=? AND group_id=?", (f["student_id"], gid)).fetchone():
                raise Bad("Студент не из вашей группы")
            d.execute("INSERT INTO duties(group_id,day,student_id,task) VALUES(?,?,?,?)", (gid, date.fromisoformat(f["day"]).isoformat(), f["student_id"], task))
        elif act == "gen":
            skip = f.get("skip_no_lessons") in ("1", "on", "true", "yes")
            gen_duty(
                d, gid,
                date.fromisoformat(f["start"]),
                max(1, min(int(f["days"]), 60)),
                max(1, min(int(f["per"]), 5)),
                task,
                skip_no_lessons=skip if "skip_no_lessons" in f else True,
            )
        elif act == "del":
            d.execute("DELETE FROM duties WHERE id=? AND group_id=?", (f["id"], gid))
        elif act == "clear":
            d.execute("DELETE FROM duties WHERE group_id=? AND day>=?", (gid, date.today().isoformat()))
        d.commit(); flash("Готово")
    except Bad as e:
        d.rollback(); flash(str(e))
    except sqlite3.IntegrityError:
        d.rollback(); flash("Этот студент уже назначен на этот день")
    except (ValueError, KeyError):
        d.rollback(); flash("Проверьте введённые данные")
    return redirect(url_for("duty"))

# ---------- Задания ----------

@app.route("/homework")
def homework():
    if "uid" not in session: return redirect(url_for("login"))
    d, uid, role = db(), session["uid"], session["role"]
    if role not in ("teacher", "student", "curator", "admin"): abort(403)
    courses = None
    if role == "teacher":
        courses = d.execute("""SELECT g.id gid, s.id sid, g.name gname, s.name sname FROM assignments a
            JOIN groups g ON g.id=a.group_id JOIN subjects s ON s.id=a.subject_id
            WHERE a.teacher_id=? ORDER BY g.name, s.name""", (uid,)).fetchall()
        rows = d.execute("""SELECT h.*, g.name gname, s.name sname,
          (SELECT COUNT(*) FROM hw_done x WHERE x.hw_id=h.id) done,
          (SELECT COUNT(*) FROM users u WHERE u.group_id=h.group_id AND u.role='student') total
          FROM homework h JOIN groups g ON g.id=h.group_id JOIN subjects s ON s.id=h.subject_id
          WHERE EXISTS(SELECT 1 FROM assignments a WHERE a.teacher_id=? AND a.group_id=h.group_id AND a.subject_id=h.subject_id)
          ORDER BY COALESCE(h.due, '9999'), h.id DESC""", (uid,)).fetchall()
        notes = {}
    else:
        gid = d.execute("SELECT group_id FROM users WHERE id=?", (uid,)).fetchone()[0]
        rows = d.execute("""SELECT h.*, s.name sname, u.name author,
          EXISTS(SELECT 1 FROM hw_done x WHERE x.hw_id=h.id AND x.student_id=?) done
          FROM homework h JOIN subjects s ON s.id=h.subject_id LEFT JOIN users u ON u.id=h.author_id
          WHERE h.group_id=? ORDER BY done, COALESCE(h.due, '9999'), h.id DESC""", (uid, gid)).fetchall()
        notes = {r["hw_id"]: r["body"] for r in d.execute(
            "SELECT hw_id, body FROM hw_notes WHERE student_id=?", (uid,))}
    # attachments for all listed homework
    ids = [r["id"] for r in rows]
    attach = {}
    if ids:
        q = ",".join("?" * len(ids))
        for a in d.execute(f"SELECT * FROM hw_attach WHERE hw_id IN ({q}) ORDER BY id", ids):
            attach.setdefault(a["hw_id"], []).append(a)
    # group by due date (or 'без срока')
    from collections import OrderedDict
    by_day = OrderedDict()
    today = date.today().isoformat()
    for r in rows:
        key = r["due"] or "без срока"
        by_day.setdefault(key, []).append(r)
    return render_template("homework.html", courses=courses, by_day=by_day, rows=rows,
                           today=today, attach=attach, notes=notes, is_teacher=(role == "teacher"))

@app.post("/homework/add")
@role("teacher")
def homework_add():
    f, d = request.form, db()
    try:
        gid, sid = map(int, f["course"].split(":"))
    except (ValueError, KeyError):
        abort(400)
    course(gid, sid)
    try:
        due = date.fromisoformat(f["due"]).isoformat() if f.get("due") else None
    except ValueError:
        due = None
    title = f["title"].strip()
    if not title:
        flash("Укажите название"); return redirect(url_for("homework"))
    cur = d.execute(
        "INSERT INTO homework(group_id,subject_id,author_id,title,body,due,created) VALUES(?,?,?,?,?,?,?)",
        (gid, sid, session["uid"], title, f.get("body", "").strip(), due, date.today().isoformat()))
    hw_id = cur.lastrowid
    # links (one per line or single field)
    for link in (f.get("links") or "").splitlines():
        link = link.strip()
        if not link:
            continue
        if not link.startswith(("http://", "https://")):
            link = "https://" + link
        name = f.get("link_name", "").strip() or link
        d.execute("INSERT INTO hw_attach(hw_id,kind,name,url) VALUES(?,?,?,?)", (hw_id, "link", name[:120], link[:500]))
    # files
    files = request.files.getlist("files")
    for fs in files:
        if not fs or not fs.filename:
            continue
        fn = secure_filename(fs.filename)
        ext = os.path.splitext(fn)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        if fs.content_length and fs.content_length > 15 * 1024 * 1024:
            continue
        stored = f"{hw_id}_{int(datetime.now().timestamp())}_{fn}"
        path = os.path.join(UPLOADS, stored)
        fs.save(path)
        d.execute("INSERT INTO hw_attach(hw_id,kind,name,url) VALUES(?,?,?,?)",
                  (hw_id, "file", fn, stored))
    d.commit()
    ids = [x[0] for x in d.execute("SELECT id FROM users WHERE role='student' AND group_id=?", (gid,))]
    sub = d.execute("SELECT name FROM subjects WHERE id=?", (sid,)).fetchone()
    sname = sub["name"] if sub else ""
    notify_many(ids, "Новое задание: " + title,
                f"Предмет: {sname}" + (f". Срок: {due}" if due else ""),
                url_for("homework"))
    flash("Задание опубликовано")
    return redirect(url_for("homework"))

@app.post("/homework/delete")
@role("teacher")
def homework_del():
    d = db()
    hid = request.form["id"]
    row = d.execute("SELECT id FROM homework WHERE id=? AND author_id=?", (hid, session["uid"])).fetchone()
    if row:
        for a in d.execute("SELECT url, kind FROM hw_attach WHERE hw_id=?", (hid,)):
            if a["kind"] == "file":
                fp = os.path.join(UPLOADS, a["url"])
                if os.path.isfile(fp):
                    try: os.remove(fp)
                    except OSError: pass
        d.execute("DELETE FROM homework WHERE id=?", (hid,))
        d.commit()
    return redirect(url_for("homework"))

@app.post("/homework/done")
@role("student")
def homework_done():
    j, d, uid = request.get_json(), db(), session["uid"]
    if not d.execute("SELECT 1 FROM homework h JOIN users u ON u.group_id=h.group_id WHERE h.id=? AND u.id=?",
                     (j["id"], uid)).fetchone():
        abort(403)
    if j["done"]:
        d.execute("INSERT OR IGNORE INTO hw_done VALUES(?,?)", (j["id"], uid))
    else:
        d.execute("DELETE FROM hw_done WHERE hw_id=? AND student_id=?", (j["id"], uid))
    d.commit()
    return jsonify(ok=True)

@app.post("/homework/note")
@role("student")
def homework_note():
    j, d, uid = request.get_json(), db(), session["uid"]
    if not d.execute("SELECT 1 FROM homework h JOIN users u ON u.group_id=h.group_id WHERE h.id=? AND u.id=?",
                     (j["id"], uid)).fetchone():
        abort(403)
    body = (j.get("body") or "").strip()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    if body:
        d.execute("""INSERT INTO hw_notes(hw_id,student_id,body,updated) VALUES(?,?,?,?)
          ON CONFLICT(hw_id,student_id) DO UPDATE SET body=excluded.body, updated=excluded.updated""",
                  (j["id"], uid, body, now))
    else:
        d.execute("DELETE FROM hw_notes WHERE hw_id=? AND student_id=?", (j["id"], uid))
    d.commit()
    return jsonify(ok=True, updated=now)

@app.route("/uploads/<path:name>")
def serve_upload(name):
    if "uid" not in session:
        abort(403)
    # only basename
    name = os.path.basename(name)
    path = os.path.join(UPLOADS, name)
    if not os.path.isfile(path):
        abort(404)
    # check access: teacher of assignment or student of group
    att = db().execute("SELECT a.hw_id, h.group_id, h.subject_id FROM hw_attach a JOIN homework h ON h.id=a.hw_id WHERE a.url=? AND a.kind='file'",
                       (name,)).fetchone()
    if not att:
        abort(404)
    role, uid = session["role"], session["uid"]
    d = db()
    if role == "teacher":
        if not d.execute("SELECT 1 FROM assignments WHERE teacher_id=? AND group_id=? AND subject_id=?",
                         (uid, att["group_id"], att["subject_id"])).fetchone():
            abort(403)
    elif role == "student":
        if not d.execute("SELECT 1 FROM users WHERE id=? AND group_id=?", (uid, att["group_id"])).fetchone():
            abort(403)
    else:
        abort(403)
    from flask import send_from_directory
    return send_from_directory(UPLOADS, name, as_attachment=False)


@app.get("/api/bonus/ping")
def bonus_ping():
    """Проверка, что маршруты доп. баллов загружены."""
    return jsonify(ok=True, routes=[r.rule for r in app.url_map.iter_rules() if "bonus" in r.rule])

# ---------- Уведомления ----------
@app.route("/notifications")
def notifications_page():
    if "uid" not in session:
        return redirect(url_for("login"))
    d = db()
    rows = d.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50",
        (session["uid"],)).fetchall()
    return render_template("notifications.html", items=rows)

@app.post("/api/notifications/read")
def notifications_read():
    if "uid" not in session:
        return jsonify(ok=False), 403
    d = db()
    j = request.get_json(silent=True) or {}
    nid = j.get("id")
    if nid:
        d.execute("UPDATE notifications SET read=1 WHERE id=? AND user_id=?", (nid, session["uid"]))
    else:
        d.execute("UPDATE notifications SET read=1 WHERE user_id=?", (session["uid"],))
    d.commit()
    return jsonify(ok=True)

@app.post("/api/notifications/clear")
def notifications_clear():
    """Стереть всю историю уведомлений текущего пользователя."""
    if "uid" not in session:
        return jsonify(ok=False), 403
    d = db()
    d.execute("DELETE FROM notifications WHERE user_id=?", (session["uid"],))
    d.commit()
    return jsonify(ok=True)

# ---------- Итоговые оценки ----------
DEFAULT_FINAL_KINDS = {
    "diff_winter": "Диф. зачёт (зима)",
    "interim": "Промежуточная аттестация",
    "exam_summer": "Экзамен (лето)",
}
# alias for backward compat
FINAL_KINDS = DEFAULT_FINAL_KINDS


def fmt_period(d_from, d_to):
    """ISO dates -> 'дд.мм.гггг – дд.мм.гггг' or empty."""
    def f(s):
        if not s:
            return ""
        s = str(s).strip()[:10]
        try:
            y, m, d = s.split("-")
            return f"{d}.{m}.{y}"
        except Exception:
            return s
    a, b = f(d_from), f(d_to)
    if a and b:
        return f"{a} – {b}"
    if a:
        return f"с {a}"
    if b:
        return f"по {b}"
    return ""

def period_for(d, gid, sid, kind):
    return {
        "from": setting(d, f"final_{gid}_{sid}_{kind}_from") or "",
        "to": setting(d, f"final_{gid}_{sid}_{kind}_to") or "",
        "label": fmt_period(
            setting(d, f"final_{gid}_{sid}_{kind}_from") or "",
            setting(d, f"final_{gid}_{sid}_{kind}_to") or "",
        ),
    }

def get_final_kinds(d, gid, sid):
    """Ordered dict kind_key -> label. Custom from final_kinds, else defaults."""
    try:
        rows = d.execute(
            "SELECT kind_key, label FROM final_kinds WHERE group_id=? AND subject_id=? ORDER BY sort_order, id",
            (gid, sid)).fetchall()
    except Exception:
        return dict(DEFAULT_FINAL_KINDS)
    if rows:
        return {r["kind_key"]: r["label"] for r in rows}
    return dict(DEFAULT_FINAL_KINDS)

def _seed_final_kinds(d, gid, sid):
    existing = d.execute("SELECT COUNT(*) FROM final_kinds WHERE group_id=? AND subject_id=?", (gid, sid)).fetchone()[0]
    if existing == 0:
        for i, (k, lab) in enumerate(DEFAULT_FINAL_KINDS.items()):
            d.execute("INSERT OR IGNORE INTO final_kinds(group_id,subject_id,kind_key,label,sort_order) VALUES(?,?,?,?,?)",
                      (gid, sid, k, lab, i))

@app.route("/journal/<int:gid>/<int:sid>/finals")
@role("teacher", "admin")
def finals_page(gid, sid):
    c = course(gid, sid, need="view")
    can_edit = has_journal_access(session["uid"], gid, sid, need="edit") if session.get("role") == "teacher" else True
    d = db()
    students = d.execute("SELECT id, name FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,)).fetchall()
    kinds = get_final_kinds(d, gid, sid)
    rows = d.execute("""SELECT * FROM final_grades WHERE group_id=? AND subject_id=?""", (gid, sid)).fetchall()
    by_st = {}
    for r in rows:
        by_st.setdefault(r["student_id"], {})[r["kind"]] = dict(r)
    periods = {k: period_for(d, gid, sid, k) for k in kinds}
    return render_template("finals.html", a=c, students=students, by_st=by_st,
                           kinds=kinds, periods=periods, can_edit=can_edit)

@app.post("/api/finals/set")
@role("teacher", "admin")
def finals_set():
    j, d = request.get_json(), db()
    gid, sid = int(j["gid"]), int(j["sid"])
    if session.get("role") == "teacher" and not has_journal_access(session["uid"], gid, sid, need="edit"):
        abort(403)
    kinds = get_final_kinds(d, gid, sid)
    kind = j.get("kind")
    if kind not in kinds:
        return jsonify(ok=False, error="Неизвестный тип"), 400
    st = int(j["student"])
    grade = (j.get("grade") or "").strip()[:20] or None
    note = (j.get("note") or "").strip()[:300]
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    if grade is None and not note:
        d.execute("DELETE FROM final_grades WHERE student_id=? AND subject_id=? AND kind=?", (st, sid, kind))
    else:
        d.execute("""INSERT INTO final_grades(student_id,group_id,subject_id,kind,grade,note,set_by,updated)
          VALUES(?,?,?,?,?,?,?,?)
          ON CONFLICT(student_id,subject_id,kind) DO UPDATE SET grade=excluded.grade, note=excluded.note,
            set_by=excluded.set_by, updated=excluded.updated""",
                  (st, gid, sid, kind, grade, note, session["uid"], now))
    d.commit()
    if grade:
        sub = d.execute("SELECT name FROM subjects WHERE id=?", (sid,)).fetchone()
        per = period_for(d, gid, sid, kind)
        per_txt = f" (период {per['label']})" if per["label"] else ""
        notify(st, f"{kinds[kind]}: {grade}",
               f"По предмету «{sub['name'] if sub else ''}» выставлена итоговая оценка «{kinds[kind]}»{per_txt}.",
               url_for("student"))
    return jsonify(ok=True)

@app.post("/api/finals/period")
@role("teacher", "admin")
def finals_period():
    j, d = request.get_json(), db()
    gid, sid = int(j["gid"]), int(j["sid"])
    if session.get("role") == "teacher" and not has_journal_access(session["uid"], gid, sid, need="edit"):
        abort(403)
    kinds = get_final_kinds(d, gid, sid)
    kind = j.get("kind")
    if kind not in kinds:
        return jsonify(ok=False, error="Неизвестный тип"), 400
    d.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
              (f"final_{gid}_{sid}_{kind}_from", (j.get("date_from") or "").strip()))
    d.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
              (f"final_{gid}_{sid}_{kind}_to", (j.get("date_to") or "").strip()))
    d.commit()
    return jsonify(ok=True)

@app.post("/api/finals/kind")
@role("teacher", "admin")
def finals_kind():
    """Добавить / переименовать / удалить тип аттестации."""
    j, d = request.get_json(), db()
    gid, sid = int(j["gid"]), int(j["sid"])
    if session.get("role") == "teacher" and not has_journal_access(session["uid"], gid, sid, need="edit"):
        abort(403)
    action = j.get("action") or "add"
    if action == "add":
        label = (j.get("label") or "").strip()[:80]
        if not label:
            return jsonify(ok=False, error="Укажите название"), 400
        _seed_final_kinds(d, gid, sid)
        import re, time
        key = re.sub(r"[^a-z0-9_]+", "_", label.lower().replace(" ", "_"))[:40] or f"k{int(time.time())}"
        if d.execute("SELECT 1 FROM final_kinds WHERE group_id=? AND subject_id=? AND kind_key=?", (gid, sid, key)).fetchone():
            key = f"{key}_{int(time.time()) % 10000}"
        max_ord = d.execute("SELECT COALESCE(MAX(sort_order),0) FROM final_kinds WHERE group_id=? AND subject_id=?",
                            (gid, sid)).fetchone()[0]
        d.execute("INSERT INTO final_kinds(group_id,subject_id,kind_key,label,sort_order) VALUES(?,?,?,?,?)",
                  (gid, sid, key, label, max_ord + 1))
        d.commit()
        return jsonify(ok=True, kind_key=key, label=label)
    if action == "rename":
        key = j.get("kind")
        label = (j.get("label") or "").strip()[:80]
        if not key or not label:
            return jsonify(ok=False, error="Нет данных"), 400
        _seed_final_kinds(d, gid, sid)
        d.execute("UPDATE final_kinds SET label=? WHERE group_id=? AND subject_id=? AND kind_key=?",
                  (label, gid, sid, key))
        d.commit()
        return jsonify(ok=True)
    if action == "delete":
        key = j.get("kind")
        if not key:
            return jsonify(ok=False, error="Нет ключа"), 400
        _seed_final_kinds(d, gid, sid)
        d.execute("DELETE FROM final_kinds WHERE group_id=? AND subject_id=? AND kind_key=?", (gid, sid, key))
        d.execute("DELETE FROM final_grades WHERE group_id=? AND subject_id=? AND kind=?", (gid, sid, key))
        d.commit()
        return jsonify(ok=True)
    return jsonify(ok=False, error="Неизвестное действие"), 400

@app.route("/journal/<int:gid>/<int:sid>/finals/export")
@role("teacher", "admin")
def finals_export(gid, sid):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    c = course(gid, sid, need="view")
    d = db()
    students = d.execute("SELECT id, name FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,)).fetchall()
    kinds = get_final_kinds(d, gid, sid)
    rows = d.execute("SELECT * FROM final_grades WHERE group_id=? AND subject_id=?", (gid, sid)).fetchall()
    by_st = {}
    for r in rows:
        by_st.setdefault(r["student_id"], {})[r["kind"]] = r
    wb = Workbook(); ws = wb.active; ws.title = "Итоги"
    fill = lambda h: PatternFill("solid", fgColor=h)
    thin = Side(style="thin", color="D9DEEC"); box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    ws["A1"] = f'Итоговые оценки: {c["gname"]}, {c["sname"]}'
    ws["A1"].font = Font(size=14, bold=True, color="14213D")
    periods = {k: period_for(d, gid, sid, k) for k in kinds}
    heads = ["Студент"]
    for k, lab in kinds.items():
        pl = periods[k]["label"]
        heads.append(f"{lab}\n{pl}" if pl else lab)
    for i, h in enumerate(heads, 1):
        x = ws.cell(3, i, h); x.font = Font(bold=True, color="FFFFFF"); x.fill = fill("14213D"); x.border = box
        x.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[3].height = 36
    for ri, st in enumerate(students, 4):
        ws.cell(ri, 1, st["name"]).border = box
        data = by_st.get(st["id"], {})
        for ci, kind in enumerate(kinds, 2):
            g = data.get(kind)
            val = (g["grade"] if g else "") or ""
            x = ws.cell(ri, ci, val); x.border = box; x.alignment = center
            if val:
                x.fill = fill("B8F0D3"); x.font = Font(bold=True)
    ws.column_dimensions["A"].width = 28
    for i in range(2, len(kinds) + 2):
        ws.column_dimensions[L(i)].width = 22
    buf = io.BytesIO(); wb.save(buf)
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=finals_{gid}_{sid}.xlsx"})


# ---------- Глобальная выгрузка оценок / посещаемости ----------
@app.route("/export/student/<int:uid>")
@role("admin", "teacher")
def export_student(uid):
    """Подробная выгрузка студента: сводка, все предметы с оценками/посещаемостью/итогами/ДЗ."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    d = db()
    st = d.execute("SELECT u.*, g.name gname FROM users u LEFT JOIN groups g ON g.id=u.group_id WHERE u.id=? AND u.role='student'", (uid,)).fetchone()
    if not st:
        abort(404)
    if session.get("role") == "teacher":
        ok = d.execute("SELECT 1 FROM assignments a WHERE a.teacher_id=? AND a.group_id=?",
                       (session["uid"], st["group_id"])).fetchone()
        if not ok:
            abort(403)
    wb = Workbook()
    fill = lambda h: PatternFill("solid", fgColor=h)
    thin = Side(style="thin", color="D9DEEC"); box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left = Alignment(horizontal="left", vertical="center", wrap_text=True)
    gfill = {2: "FFC9C9", 3: "FFD84D", 4: "CFE0FF", 5: "B8F0D3"}
    head_font = Font(bold=True, color="FFFFFF")
    title_font = Font(size=14, bold=True, color="14213D")

    subjects = []
    if st["group_id"]:
        subjects = d.execute("""SELECT s.id, s.name,
            (SELECT group_concat(t.name, ', ') FROM assignments a2 JOIN users t ON t.id=a2.teacher_id
              WHERE a2.group_id=? AND a2.subject_id=s.id) tname
            FROM subjects s WHERE s.id IN (SELECT subject_id FROM assignments WHERE group_id=?)
            ORDER BY s.name""", (st["group_id"], st["group_id"])).fetchall()
        # also subjects that have marks even without assignment
        extra = d.execute("""SELECT DISTINCT s.id, s.name FROM subjects s
            JOIN lessons l ON l.subject_id=s.id JOIN marks m ON m.lesson_id=l.id
            WHERE m.student_id=? AND l.group_id=?""", (uid, st["group_id"])).fetchall()
        known = {s["id"] for s in subjects}
        for e in extra:
            if e["id"] not in known:
                subjects.append(e)
                known.add(e["id"])

    # ===== Sheet 1: Сводка =====
    ws = wb.active; ws.title = "Сводка"
    ws["A1"] = f'Студент: {st["name"]}'; ws["A1"].font = title_font
    ws["A2"] = f'Группа: {st["gname"] or "—"}. Логин: {st["login"]}. Выгружено {datetime.now():%d.%m.%Y %H:%M}'
    ws["A2"].font = Font(color="66708C")
    heads = ["№", "Предмет", "Преподаватели", "Ср. балл", "Оценок", "Был", "Не был", "Уваж.", "Пропуски",
             "Посещ. %", "Доп. баллы", "Итоги"]
    for i, h in enumerate(heads, 1):
        x = ws.cell(4, i, h); x.font = head_font; x.fill = fill("14213D"); x.border = box; x.alignment = center
    row_i = 5
    all_cells = []
    for si, sub in enumerate(subjects, 1):
        cells = d.execute("""SELECT m.present, m.grade, m.excuse FROM lessons l
            LEFT JOIN marks m ON m.lesson_id=l.id AND m.student_id=?
            WHERE l.group_id=? AND l.subject_id=?""", (uid, st["group_id"], sub["id"])).fetchall()
        all_cells.extend(cells)
        stt = stats(cells)
        grades_n = sum(1 for c in cells if c and c["grade"])
        was = sum(1 for c in cells if c and c["present"] == 1)
        bp = d.execute("SELECT balance FROM bonus_points WHERE student_id=? AND subject_id=?", (uid, sub["id"])).fetchone()
        kinds = get_final_kinds(d, st["group_id"], sub["id"])
        frows = d.execute("SELECT kind, grade FROM final_grades WHERE student_id=? AND subject_id=?", (uid, sub["id"])).fetchall()
        by_k = {r["kind"]: r["grade"] for r in frows if r["grade"]}
        finals_txt = "; ".join(f"{kinds.get(k,k)}: {g}" for k, g in by_k.items()) or "—"
        tname = sub["tname"] if "tname" in sub.keys() else ""
        vals = [si, sub["name"], tname or "—", stt["avg"], grades_n, was,
                stt.get("unexcused") or 0, stt.get("excused") or 0, stt["missed"],
                stt["pct"], bp["balance"] if bp else 0, finals_txt]
        for i, v in enumerate(vals, 1):
            x = ws.cell(row_i, i, v); x.border = box
            x.alignment = center if i not in (2, 3, 12) else left
            if i == 4 and v is not None and v < 3:
                x.fill = fill("FFE4E4")
            if i == 10 and v is not None and v < 70:
                x.fill = fill("FFF3CD")
        row_i += 1
    # totals
    tot = stats(all_cells)
    ws.cell(row_i + 1, 1, "ИТОГО").font = Font(bold=True)
    ws.cell(row_i + 1, 4, tot["avg"]); ws.cell(row_i + 1, 9, tot["missed"]); ws.cell(row_i + 1, 10, tot["pct"])
    for i in range(1, 13):
        ws.cell(row_i + 1, i).border = box; ws.cell(row_i + 1, i).font = Font(bold=True)
        ws.cell(row_i + 1, i).fill = fill("EEF1F8")
    widths = [5, 28, 28, 11, 10, 8, 10, 10, 10, 11, 12, 40]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[L(i)].width = w
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:L{max(4, row_i-1)}"

    # ===== Sheet 2: Все оценки (плоская таблица) =====
    wsA = wb.create_sheet("Все оценки")
    wsA["A1"] = f'Все отметки: {st["name"]} ({st["gname"] or "—"})'
    wsA["A1"].font = title_font
    ah = ["Предмет", "Дата", "Тема", "Пара", "Статус", "Оценка", "Причина уваж.", "Доп. баллы"]
    for i, h in enumerate(ah, 1):
        x = wsA.cell(3, i, h); x.font = head_font; x.fill = fill("14213D"); x.border = box; x.alignment = center
    ri = 4
    for sub in subjects:
        lessons = d.execute("""SELECT l.id, l.day, l.topic, m.present, m.grade, m.excuse,
            (SELECT points_used FROM bonus_applications ba WHERE ba.lesson_id=l.id AND ba.student_id=? AND ba.cancelled_at IS NULL) pts
            FROM lessons l LEFT JOIN marks m ON m.lesson_id=l.id AND m.student_id=?
            WHERE l.group_id=? AND l.subject_id=? ORDER BY l.day""", (uid, uid, st["group_id"], sub["id"])).fetchall()
        for l in lessons:
            status = "—"
            if l["grade"]:
                status = "оценка"
            elif l["present"] == 2:
                status = "уваж."
            elif l["present"] == 0:
                status = "н"
            elif l["present"] == 1:
                status = "был"
            vals = [sub["name"],
                    f'{l["day"][8:10]}.{l["day"][5:7]}.{l["day"][:4]}' if l["day"] else "",
                    l["topic"] or "", "", status, l["grade"] or "",
                    (l["excuse"] or "") if l["present"] == 2 else "",
                    l["pts"] or ""]
            for i, v in enumerate(vals, 1):
                x = wsA.cell(ri, i, v); x.border = box
                if l["grade"] and l["grade"] in gfill and i == 6:
                    x.fill = fill(gfill[l["grade"]]); x.font = Font(bold=True)
                if status == "н" and i == 5:
                    x.fill = fill("FFE4E4")
                if status == "уваж." and i == 5:
                    x.fill = fill("FFF3CD")
            ri += 1
    for i, w in enumerate([24, 12, 32, 8, 10, 10, 28, 12], 1):
        wsA.column_dimensions[L(i)].width = w
    wsA.freeze_panes = "A4"
    if ri > 4:
        wsA.auto_filter.ref = f"A3:H{ri-1}"

    # ===== Sheet 3: Итоги =====
    wsF = wb.create_sheet("Итоговые аттестации")
    wsF["A1"] = f'Итоговые: {st["name"]}'
    wsF["A1"].font = title_font
    fh = ["Предмет", "Аттестация", "Период", "Оценка", "Заметка", "Обновлено"]
    for i, h in enumerate(fh, 1):
        x = wsF.cell(3, i, h); x.font = head_font; x.fill = fill("14213D"); x.border = box; x.alignment = center
    ri = 4
    for sub in subjects:
        kinds = get_final_kinds(d, st["group_id"], sub["id"])
        frows = {r["kind"]: r for r in d.execute(
            "SELECT * FROM final_grades WHERE student_id=? AND subject_id=?", (uid, sub["id"]))}
        for k, lab in kinds.items():
            g = frows.get(k)
            per = period_for(d, st["group_id"], sub["id"], k)
            vals = [sub["name"], lab, per.get("label") or "",
                    (g["grade"] if g else "") or "—",
                    (g["note"] if g else "") or "",
                    (g["updated"] if g else "") or ""]
            for i, v in enumerate(vals, 1):
                x = wsF.cell(ri, i, v); x.border = box
                if g and g["grade"] and i == 4:
                    x.fill = fill("B8F0D3"); x.font = Font(bold=True)
            ri += 1
    for i, w in enumerate([24, 28, 22, 12, 30, 16], 1):
        wsF.column_dimensions[L(i)].width = w

    # ===== Sheet 4: ДЗ =====
    wsH = wb.create_sheet("Задания")
    wsH["A1"] = f'Задания: {st["name"]}'
    wsH["A1"].font = title_font
    hh = ["Предмет", "Задание", "Срок", "Статус", "Выполнено"]
    for i, h in enumerate(hh, 1):
        x = wsH.cell(3, i, h); x.font = head_font; x.fill = fill("14213D"); x.border = box; x.alignment = center
    ri = 4
    if st["group_id"]:
        hws = d.execute("""SELECT h.title, h.due, h.created, s.name sname,
            (SELECT 1 FROM hw_done x WHERE x.hw_id=h.id AND x.student_id=?) done
            FROM homework h JOIN subjects s ON s.id=h.subject_id
            WHERE h.group_id=? ORDER BY h.due DESC""", (uid, st["group_id"])).fetchall()
        for h in hws:
            vals = [h["sname"], h["title"], h["due"] or "", "сдано" if h["done"] else "не сдано", "✓" if h["done"] else ""]
            for i, v in enumerate(vals, 1):
                x = wsH.cell(ri, i, v); x.border = box
                if not h["done"] and i == 4:
                    x.fill = fill("FFF3CD")
            ri += 1
    for i, w in enumerate([22, 40, 14, 12, 12], 1):
        wsH.column_dimensions[L(i)].width = w

    # ===== per-subject sheets (capped) =====
    for sub in subjects[:20]:
        safe_title = re.sub(r'[\\/*?:\[\]]', "_", sub["name"])[:28] or "Предмет"
        # unique sheet name
        base, n = safe_title, 1
        while safe_title in [s.title for s in wb.worksheets]:
            n += 1
            safe_title = f"{base[:25]}_{n}"
        ws2 = wb.create_sheet(title=safe_title)
        ws2["A1"] = f'{st["name"]} — {sub["name"]}'
        ws2["A1"].font = Font(size=12, bold=True)
        tname = sub["tname"] if "tname" in sub.keys() else ""
        ws2["A2"] = f'Преподаватели: {tname or "—"}'
        for i, h in enumerate(["Дата", "Тема", "Статус", "Оценка", "Причина"], 1):
            x = ws2.cell(4, i, h); x.font = head_font; x.fill = fill("14213D"); x.border = box
        lessons = d.execute("""SELECT l.id, l.day, l.topic, m.present, m.grade, m.excuse FROM lessons l
            LEFT JOIN marks m ON m.lesson_id=l.id AND m.student_id=?
            WHERE l.group_id=? AND l.subject_id=? ORDER BY l.day""", (uid, st["group_id"], sub["id"])).fetchall()
        for ri, l in enumerate(lessons, 5):
            ws2.cell(ri, 1, f'{l["day"][8:10]}.{l["day"][5:7]}' if l["day"] else "").border = box
            ws2.cell(ri, 2, l["topic"] or "").border = box
            status = "—"
            if l["grade"]: status = "оценка"
            elif l["present"] == 2: status = "уваж."
            elif l["present"] == 0: status = "н"
            elif l["present"] == 1: status = "был"
            ws2.cell(ri, 3, status).border = box
            gx = ws2.cell(ri, 4, l["grade"] or ""); gx.border = box
            if l["grade"] and l["grade"] in gfill:
                gx.fill = fill(gfill[l["grade"]]); gx.font = Font(bold=True)
            ws2.cell(ri, 5, (l["excuse"] or "") if l["present"] == 2 else "").border = box
        for i in range(1, 6):
            ws2.column_dimensions[L(i)].width = [12, 30, 12, 10, 36][i-1]
        stt = stats(lessons)
        fr = 5 + len(lessons) + 1
        ws2.cell(fr, 1, f'Средний: {stt["avg"] or "—"}; пропуски: {stt["missed"]}; посещ.: {stt["pct"] if stt["pct"] is not None else "—"}%')
        kinds = get_final_kinds(d, st["group_id"], sub["id"])
        frows = {r["kind"]: r for r in d.execute(
            "SELECT * FROM final_grades WHERE student_id=? AND subject_id=?", (uid, sub["id"]))}
        ws2.cell(fr + 2, 1, "Итоговые аттестации").font = Font(bold=True)
        for i, (k, lab) in enumerate(kinds.items()):
            per = period_for(d, st["group_id"], sub["id"], k)
            g = frows.get(k)
            ws2.cell(fr + 3 + i, 1, lab).border = box
            ws2.cell(fr + 3 + i, 2, per.get("label") or "").border = box
            ws2.cell(fr + 3 + i, 3, (g["grade"] if g else "") or "—").border = box
            ws2.cell(fr + 3 + i, 4, (g["note"] if g else "") or "").border = box

    buf = io.BytesIO(); wb.save(buf)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in st["name"])[:40]
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=student_{safe}.xlsx"})

@app.route("/export/all")
@role("admin")
def export_all():
    """Выгрузка сводки по всем студентам."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    d = db()
    wb = Workbook()
    fill = lambda h: PatternFill("solid", fgColor=h)
    thin = Side(style="thin", color="D9DEEC"); box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    ws = wb.active; ws.title = "Все студенты"
    ws["A1"] = f'Сводка по всем студентам. Выгружено {datetime.now():%d.%m.%Y %H:%M}'
    ws["A1"].font = Font(size=14, bold=True, color="14213D")
    heads = ["Студент", "Группа", "Предмет", "Ср. балл", "Оценок", "Был", "Не был", "Уваж.", "Пропуски", "Посещаемость %", "Доп. баллы", "Итоги"]
    for i, h in enumerate(heads, 1):
        x = ws.cell(3, i, h); x.font = Font(bold=True, color="FFFFFF"); x.fill = fill("14213D"); x.border = box; x.alignment = center
    ri = 4
    students = d.execute("""SELECT u.id, u.name, u.group_id, g.name gname FROM users u
        LEFT JOIN groups g ON g.id=u.group_id WHERE u.role='student' ORDER BY g.name, u.name""").fetchall()
    for st in students:
        if not st["group_id"]:
            ws.cell(ri, 1, st["name"]).border = box
            ws.cell(ri, 2, "—").border = box
            ri += 1
            continue
        subjects = d.execute("""SELECT s.id, s.name FROM subjects s
            WHERE s.id IN (SELECT subject_id FROM assignments WHERE group_id=?) ORDER BY s.name""", (st["group_id"],)).fetchall()
        for sub in subjects:
            cells = d.execute("""SELECT m.present, m.grade, m.excuse FROM lessons l
                LEFT JOIN marks m ON m.lesson_id=l.id AND m.student_id=?
                WHERE l.group_id=? AND l.subject_id=?""", (st["id"], st["group_id"], sub["id"])).fetchall()
            stt = stats(cells)
            bp = d.execute("SELECT balance FROM bonus_points WHERE student_id=? AND subject_id=?", (st["id"], sub["id"])).fetchone()
            grades_n = sum(1 for c in cells if c and c["grade"])
            was = sum(1 for c in cells if c and c["present"] == 1)
            kinds = get_final_kinds(d, st["group_id"], sub["id"])
            frows = d.execute("SELECT kind, grade FROM final_grades WHERE student_id=? AND subject_id=?", (st["id"], sub["id"])).fetchall()
            by_k = {r["kind"]: r["grade"] for r in frows if r["grade"]}
            finals_txt = "; ".join(f"{kinds.get(k,k)}:{g}" for k, g in by_k.items()) or ""
            vals = [st["name"], st["gname"] or "—", sub["name"], stt["avg"], grades_n, was,
                    stt.get("unexcused") or 0, stt.get("excused") or 0, stt["missed"],
                    stt["pct"], bp["balance"] if bp else 0, finals_txt]
            for i, v in enumerate(vals, 1):
                x = ws.cell(ri, i, v); x.border = box; x.alignment = center if i > 2 else Alignment(horizontal="left")
            ri += 1
    for i, w in enumerate([22, 12, 24, 11, 9, 8, 9, 9, 10, 12, 11, 36], 1):
        ws.column_dimensions[L(i)].width = w
    buf = io.BytesIO(); wb.save(buf)
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=all_students.xlsx"})

# ---------- Админ: служебные действия ----------
@app.post("/admin/tools")
@role("admin")
def admin_tools():
    d, f = db(), request.form
    act = f.get("act")
    try:
        if act == "clear_marks_group":
            gid = int(f["group_id"])
            d.execute("""DELETE FROM marks WHERE student_id IN
              (SELECT id FROM users WHERE role='student' AND group_id=?)""", (gid,))
            d.execute("""DELETE FROM lessons WHERE group_id=?""", (gid,))
            flash("Оценки и занятия группы очищены")
        elif act == "clear_marks_student":
            sid = int(f["student_id"])
            d.execute("DELETE FROM marks WHERE student_id=?", (sid,))
            d.execute("DELETE FROM bonus_points WHERE student_id=?", (sid,))
            d.execute("DELETE FROM final_grades WHERE student_id=?", (sid,))
            flash("Данные студента (оценки, доп. баллы, итоги) очищены")
        elif act == "clear_all_notifications":
            d.execute("DELETE FROM notifications")
            flash("Все уведомления удалены")
        elif act == "cleanup_bells":
            n = 0
            for b in d.execute("SELECT id,num,start,end FROM bells").fetchall():
                if b["num"] > 12 or b["num"] < 1:
                    used = d.execute("SELECT COUNT(*) c FROM schedule WHERE bell_id=?", (b["id"],)).fetchone()["c"]
                    if used == 0:
                        d.execute("DELETE FROM bells WHERE id=?", (b["id"],)); n += 1
                elif b["start"]=="00:00" and b["end"]=="00:00":
                    defaults = {1:("09:00","10:35"),2:("10:55","12:30"),3:("13:10","14:45"),
                                4:("14:55","16:30"),5:("16:40","18:15"),6:("18:25","20:00")}
                    if 1 <= b["num"] <= 6 and b["num"] in defaults:
                        s,e = defaults[b["num"]]
                        d.execute("UPDATE bells SET start=?,end=? WHERE id=?", (s,e,b["id"])); n += 1
                    else:
                        used = d.execute("SELECT COUNT(*) c FROM schedule WHERE bell_id=?", (b["id"],)).fetchone()["c"]
                        if used == 0:
                            d.execute("DELETE FROM bells WHERE id=?", (b["id"],)); n += 1
            flash(f"Звонки: исправлено записей — {n}")
        elif act == "logout_hint":
            # Flask sessions are cookie-based; force password rotate is safest "logout all"
            flash("Сессии cookie-based: смените SECRET_KEY или пароли пользователей, чтобы инвалидировать входы")
        elif act == "personal_announce":
            uid = int(f["user_id"])
            title = f["title"].strip()
            body = f["body"].strip()
            if title and body:
                try:
                    d.execute("INSERT INTO announcements(author_id,group_id,title,body,created,target_user_id) VALUES(?,?,?,?,?,?)",
                              (session["uid"], None, title, body, datetime.now().strftime("%d.%m.%Y %H:%M"), uid))
                except Exception:
                    d.execute("INSERT INTO announcements(author_id,group_id,title,body,created) VALUES(?,?,?,?,?)",
                              (session["uid"], None, title, body, datetime.now().strftime("%d.%m.%Y %H:%M")))
                notify(uid, "Личное: " + title, body[:200], url_for("notifications_page"))
                flash("Личное объявление отправлено")
            else:
                flash("Укажите заголовок и текст")
        else:
            flash("Неизвестное действие")
        d.commit()
    except Exception as e:
        d.rollback()
        flash(f"Ошибка: {e}")
    return redirect(url_for("admin"))


# ========== Замечания в журнале ==========
@app.post("/journal/remark")
@role("admin", "teacher", "curator")
def journal_remark():
    j, d = request.get_json(force=True, silent=True) or {}, db()
    sid = int(j.get("student_id") or 0)
    gid = int(j.get("group_id") or 0)
    body = (j.get("body") or "").strip()[:1000]
    if not sid or not gid or not body:
        return jsonify(ok=False, error="Нужны student_id, group_id и текст"), 400
    subj = j.get("subject_id")
    lid = j.get("lesson_id")
    if session["role"] not in ("admin",):
        ok_access = False
        if subj and has_journal_access(session["uid"], gid, int(subj), need="edit"):
            ok_access = True
        g = d.execute("SELECT curator_id FROM groups WHERE id=?", (gid,)).fetchone()
        if g and g["curator_id"] == session["uid"]:
            ok_access = True
        if not ok_access:
            abort(403)
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    d.execute("""INSERT INTO journal_remarks(student_id,group_id,subject_id,lesson_id,author_id,body,created)
                 VALUES(?,?,?,?,?,?,?)""",
              (sid, gid, subj, lid, session["uid"], body, now))
    d.commit()
    notify(sid, "Замечание", body[:200], "/")
    return jsonify(ok=True)

@app.get("/journal/<int:gid>/remarks/<int:student_id>")
@role("admin", "teacher", "curator", "student")
def journal_remarks_list(gid, student_id):
    d = db()
    rows = d.execute("""SELECT r.*, u.name author_name, s.name subject_name
                        FROM journal_remarks r
                        JOIN users u ON u.id=r.author_id
                        LEFT JOIN subjects s ON s.id=r.subject_id
                        WHERE r.group_id=? AND r.student_id=?
                        ORDER BY r.id DESC LIMIT 100""", (gid, student_id)).fetchall()
    return jsonify([dict(x) for x in rows])

@app.route("/journal/<int:gid>/<int:sid>/import", methods=["GET", "POST"])
@role("admin", "teacher", "curator")
def journal_import(gid, sid):
    d = db()
    if session["role"] != "admin" and not has_journal_access(session["uid"], gid, sid, need="edit"):
        abort(403)
    if request.method == "GET":
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.utils import get_column_letter as L
        from openpyxl.formatting.rule import FormulaRule
        wb = Workbook()
        thin = Border(
            left=Side(style="thin", color="D9DEEC"), right=Side(style="thin", color="D9DEEC"),
            top=Side(style="thin", color="D9DEEC"), bottom=Side(style="thin", color="D9DEEC"))
        head_fill = PatternFill("solid", fgColor="14213D")
        head_font = Font(bold=True, color="FFFFFF", size=10)
        green = PatternFill("solid", fgColor="E9F8F1")
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)

        wi = wb.active; wi.title = "Инструкция"
        wi["A1"] = "Как заполнять журнал (Excel)"
        wi["A1"].font = Font(bold=True, size=14, color="14213D")
        for i, t in enumerate([
            "",
            "Лист «Журнал» — матрица: студенты × даты (как на сайте).",
            "В ячейке можно написать:",
            "  2–5     = оценка (студент отмечается присутствующим)",
            "  (пусто) = был",
            "  н       = не был",
            "  у       = уважительная причина",
            "",
            "Лист «Причины» — текст причины при «у» (Студент | Дата | Причина).",
            "Лист «Доп.баллы» — Студент | Баллы (дельта).",
            "Лист «Итоги» — итоговые аттестации.",
            "",
            "1 пара = 95 мин. Цвета ячеек подскажут статус при заполнении.",
        ], 1):
            wi.cell(i, 1, t)
        wi["A15"] = "• / пусто"; wi["A15"].fill = PatternFill("solid", fgColor="B8F0D3")
        wi["B15"] = "н"; wi["B15"].fill = PatternFill("solid", fgColor="FFC9C9")
        wi["C15"] = "у"; wi["C15"].fill = PatternFill("solid", fgColor="FFF3CD")
        wi["D15"] = "5"; wi["D15"].fill = PatternFill("solid", fgColor="B8F0D3")
        wi.column_dimensions["A"].width = 70

        try:
            sync_lessons(d, gid)
        except Exception:
            pass
        lessons = d.execute(
            "SELECT id, day, topic FROM lessons WHERE group_id=? AND subject_id=? ORDER BY day",
            (gid, sid)).fetchall()
        studs = d.execute("SELECT id, name FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,)).fetchall()
        marks = {(m["student_id"], m["lesson_id"]): m for m in d.execute(
            "SELECT * FROM marks WHERE lesson_id IN (SELECT id FROM lessons WHERE group_id=? AND subject_id=?)",
            (gid, sid))}

        ws = wb.create_sheet("Журнал", 0)
        ws.cell(1, 1, "Студент").font = head_font
        ws.cell(1, 1).fill = head_fill
        ws.cell(1, 1).border = thin
        for i, l in enumerate(lessons, 2):
            c = ws.cell(1, i, f'{l["day"][8:10]}.{l["day"][5:7]}')
            c.font = head_font; c.fill = head_fill; c.alignment = center; c.border = thin
            ws.cell(2, i, l["day"]).font = Font(size=1, color="FFFFFF")
            ws.cell(3, i, l["id"]).font = Font(size=1, color="FFFFFF")
        ws.cell(2, 1, "date"); ws.cell(3, 1, "lid")
        ws.row_dimensions[2].hidden = True
        ws.row_dimensions[3].hidden = True
        gfill = {2: "FFC9C9", 3: "FFD84D", 4: "CFE0FF", 5: "B8F0D3"}
        for ri, st in enumerate(studs, 4):
            ws.cell(ri, 1, st["name"]).border = thin
            for ci, l in enumerate(lessons, 2):
                m = marks.get((st["id"], l["id"]))
                val, fill = "", green
                if m:
                    if m["grade"] in (2, 3, 4, 5):
                        val = m["grade"]; fill = PatternFill("solid", fgColor=gfill[m["grade"]])
                    elif m["present"] == 0:
                        val = "н"; fill = PatternFill("solid", fgColor="FFE4E4")
                    elif m["present"] == 2:
                        val = "у"; fill = PatternFill("solid", fgColor="FFF3CD")
                x = ws.cell(ri, ci, val)
                x.fill = fill; x.alignment = center; x.border = thin; x.font = Font(bold=True)
        if lessons and studs:
            last_row = 3 + len(studs)
            last_col = 1 + len(lessons)
            rng = f"B4:{L(last_col)}{last_row}"
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(B4="н",B4="Н",B4="0")'], fill=PatternFill("solid", fgColor="FFC9C9")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(B4="у",B4="У",B4="2")'], fill=PatternFill("solid", fgColor="FFF3CD")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(B4=5,B4="5")'], fill=PatternFill("solid", fgColor="B8F0D3")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(B4=4,B4="4")'], fill=PatternFill("solid", fgColor="CFE0FF")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(B4=3,B4="3")'], fill=PatternFill("solid", fgColor="FFD84D")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(B4=2,B4="2")'], fill=PatternFill("solid", fgColor="FFC9C9")))
        ws.column_dimensions["A"].width = 28
        for i in range(2, 2 + len(lessons)):
            ws.column_dimensions[L(i)].width = 6
        ws.freeze_panes = "B4"
        ws.sheet_view.showGridLines = False

        # flat fallback sheet still useful
        wf = wb.create_sheet("Список")
        wf.append(["Студент", "Дата", "Оценка", "Посещаемость", "Причина", "Доп.баллы"])
        for c in range(1, 7):
            wf.cell(1, c).font = Font(bold=True, color="FFFFFF")
            wf.cell(1, c).fill = head_fill
        for st in studs:
            for l in lessons[:5]:
                wf.append([st["name"], l["day"], "", "", "", ""])
        for col, w in zip("ABCDEF", (28, 14, 10, 14, 28, 12)):
            wf.column_dimensions[col].width = w
        # finals template sheet
        wf = wb.create_sheet("Итоги")
        wf.append(["Студент", "Тип", "Оценка", "Заметка", "Дата_с", "Дата_по"])
        for c in range(1, 7):
            wf.cell(1, c).font = Font(bold=True)
            wf.cell(1, c).fill = PatternFill("solid", fgColor="DCE3F5")
        wf.append(["", "dif_zach", "зачёт", "", "", ""])
        wf.append(["", "exam", "4", "", "", ""])
        wf.append(["", "course", "", "не сдавал", "", ""])
        wf["A8"] = "Тип: dif_zach (диф. зачёт), exam (экзамен), course (курсовая) или свой kind_key"
        for col, w in zip("ABCDEF", (28, 14, 12, 24, 12, 12)):
            wf.column_dimensions[col].width = w
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        return Response(buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=journal_template_{gid}_{sid}.xlsx"})
    f = request.files.get("file")
    if not f:
        flash("Выберите файл Excel"); return redirect(url_for("journal", gid=gid, sid=sid))
    from openpyxl import load_workbook
    wb = load_workbook(f, data_only=True); ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        flash("Пустой файл"); return redirect(url_for("journal", gid=gid, sid=sid))
    header = [str(c or "").strip().lower() for c in rows[0]]
    def col(*names):
        for n in names:
            if n in header: return header.index(n)
        return None
    ci_name, ci_day = col("студент", "фио", "имя"), col("дата", "день")
    ci_grade, ci_pres, ci_exc = col("оценка", "балл"), col("посещаемость", "явка"), col("причина", "excuse")
    ci_bonus = col("доп.баллы", "доп баллы", "бонус", "bonus")
    if ci_name is None or ci_day is None:
        flash("Нужны колонки Студент и Дата"); return redirect(url_for("journal", gid=gid, sid=sid))
    name_map = {r["name"].strip().lower(): r["id"] for r in d.execute(
        "SELECT id, name FROM users WHERE role='student' AND group_id=?", (gid,))}
    ok = err = 0
    for row in rows[1:]:
        if not row or not row[ci_name]: continue
        name = str(row[ci_name]).strip().lower()
        st_id = name_map.get(name)
        if not st_id:
            err += 1; continue
        day_raw = row[ci_day]
        if hasattr(day_raw, "strftime"):
            day = day_raw.strftime("%Y-%m-%d")
        else:
            day = str(day_raw).strip()[:10]
            if "." in day:
                parts = day.split(".")
                if len(parts) == 3:
                    day = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
        lid_row = d.execute("SELECT id FROM lessons WHERE group_id=? AND subject_id=? AND day=?",
                            (gid, sid, day)).fetchone()
        if not lid_row:
            d.execute("INSERT OR IGNORE INTO lessons(group_id,subject_id,day,topic) VALUES(?,?,?,?)",
                      (gid, sid, day, ""))
            lid_row = d.execute("SELECT id FROM lessons WHERE group_id=? AND subject_id=? AND day=?",
                                (gid, sid, day)).fetchone()
        lid = lid_row["id"]
        d.execute("INSERT OR IGNORE INTO marks(student_id,lesson_id) VALUES(?,?)", (st_id, lid))
        if ci_pres is not None and row[ci_pres] is not None and str(row[ci_pres]).strip() != "":
            try:
                p = int(float(str(row[ci_pres]).strip()))
                if p in (0, 1, 2):
                    d.execute("UPDATE marks SET present=? WHERE student_id=? AND lesson_id=?", (p, st_id, lid))
            except Exception: pass
        if ci_grade is not None and row[ci_grade] is not None and str(row[ci_grade]).strip() != "":
            try:
                gv = int(float(str(row[ci_grade]).strip()))
                if gv in (2, 3, 4, 5):
                    d.execute("UPDATE marks SET grade=?, present=1, excuse='' WHERE student_id=? AND lesson_id=?",
                              (gv, st_id, lid))
            except Exception: pass
        if ci_exc is not None and row[ci_exc]:
            reason = str(row[ci_exc]).strip()[:300]
            d.execute("UPDATE marks SET excuse=?, present=2, grade=NULL WHERE student_id=? AND lesson_id=?",
                      (reason, st_id, lid))
        if ci_bonus is not None and row[ci_bonus] is not None and str(row[ci_bonus]).strip() != "":
            try:
                delta = int(float(str(row[ci_bonus]).strip()))
                d.execute("INSERT OR IGNORE INTO bonus_points(student_id,subject_id,balance) VALUES(?,?,0)", (st_id, sid))
                d.execute("UPDATE bonus_points SET balance=COALESCE(balance,0)+? WHERE student_id=? AND subject_id=?",
                          (delta, st_id, sid))
                d.execute("""INSERT INTO bonus_log(student_id,subject_id,teacher_id,delta,reason,created)
                             VALUES(?,?,?,?,?,?)""",
                          (st_id, sid, session["uid"], delta, "импорт Excel",
                           datetime.now().strftime("%d.%m.%Y %H:%M")))
            except Exception:
                pass
        ok += 1
    d.commit()
    flash(f"Импорт: обновлено {ok}, ошибок сопоставления {err}")
    return redirect(url_for("journal", gid=gid, sid=sid))

@app.route("/admin/backup")
@role("admin")
def admin_backup():
    import zipfile, csv
    d = db()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        try:
            d.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception: pass
        z.write(DB, "campus.db")
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(["id","login","name","role","group","email","phone","birth_date","student_id_number",
                    "parent_name","parent_phone","address","notes","active"])
        for u in d.execute("""SELECT u.*, g.name gname FROM users u LEFT JOIN groups g ON g.id=u.group_id
                              ORDER BY u.role, g.name, u.name"""):
            keys = u.keys()
            w.writerow([u["id"], u["login"], u["name"], u["role"], u["gname"] or "",
                        u["email"] if "email" in keys else "", u["phone"] if "phone" in keys else "",
                        u["birth_date"] if "birth_date" in keys else "",
                        u["student_id_number"] if "student_id_number" in keys else "",
                        u["parent_name"] if "parent_name" in keys else "",
                        u["parent_phone"] if "parent_phone" in keys else "",
                        u["address"] if "address" in keys else "",
                        u["notes"] if "notes" in keys else "",
                        u["active"] if "active" in keys else 1])
        z.writestr("users.csv", out.getvalue())
        out2 = io.StringIO(); w2 = csv.writer(out2)
        w2.writerow(["student","group","subject","day","present","grade","excuse"])
        for m in d.execute("""SELECT u.name uname, g.name gname, s.name sname, l.day, m.present, m.grade,
                              COALESCE(m.excuse,'') excuse
                              FROM marks m
                              JOIN users u ON u.id=m.student_id
                              JOIN lessons l ON l.id=m.lesson_id
                              JOIN groups g ON g.id=l.group_id
                              JOIN subjects s ON s.id=l.subject_id
                              ORDER BY g.name, s.name, l.day, u.name"""):
            w2.writerow([m["uname"], m["gname"], m["sname"], m["day"], m["present"], m["grade"], m["excuse"]])
        z.writestr("marks.csv", out2.getvalue())
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=campus_backup_{stamp}.zip"})

@app.post("/attendance")
@role("admin", "teacher", "curator", "student")
def attendance_set():
    j, d = request.get_json(force=True, silent=True) or {}, db()
    lid = int(j.get("lesson") or 0)
    st = int(j.get("student") or 0)
    l = d.execute("SELECT id, group_id, subject_id FROM lessons WHERE id=?", (lid,)).fetchone()
    if not l: abort(404)
    if not can_fill_attendance(session["uid"], l["group_id"]):
        abort(403)
    d.execute("INSERT OR IGNORE INTO marks(student_id,lesson_id) VALUES(?,?)", (st, lid))
    if "present" in j:
        p = j["present"]
        if p is not None:
            p = int(p)
            if p not in (0, 1, 2): abort(400)
        d.execute("UPDATE marks SET present=? WHERE student_id=? AND lesson_id=?", (p, st, lid))
        if p in (0, 2) or p is None:
            d.execute("UPDATE marks SET grade=NULL WHERE student_id=? AND lesson_id=?", (st, lid))
        if p != 2:
            d.execute("UPDATE marks SET excuse='' WHERE student_id=? AND lesson_id=?", (st, lid))
    if "excuse" in j:
        reason = (j.get("excuse") or "").strip()[:300]
        d.execute("UPDATE marks SET excuse=?, present=2, grade=NULL WHERE student_id=? AND lesson_id=?",
                  (reason, st, lid))
    d.commit()
    return jsonify(ok=True)



@app.route("/attendance/group")
@role("student", "admin", "teacher", "curator")
def head_attendance():
    """Журнал посещаемости для старосты / зама (как обычный журнал, только present)."""
    d, uid = db(), session["uid"]
    g = d.execute("SELECT * FROM groups WHERE head_id=? OR deputy_head_id=?", (uid, uid)).fetchone()
    if not g and session.get("role") not in ("admin",):
        flash("Вы не староста и не зам. старосты")
        return redirect(url_for("student"))
    if not g:
        gid = request.args.get("group", type=int)
        g = d.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone() if gid else None
        if not g:
            flash("Укажите группу")
            return redirect(url_for("admin"))
    gid = g["id"]
    try:
        sync_lessons(d, gid)
    except Exception:
        pass
    sid = request.args.get("sid", type=int)
    date_from = request.args.get("from") or (date.today() - timedelta(days=45)).isoformat()
    date_to = request.args.get("to") or date.today().isoformat()
    try:
        date.fromisoformat(date_from); date.fromisoformat(date_to)
    except ValueError:
        date_from = (date.today() - timedelta(days=45)).isoformat()
        date_to = date.today().isoformat()
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    subjects = d.execute("""SELECT DISTINCT s.id, s.name FROM subjects s
        WHERE s.id IN (SELECT subject_id FROM lessons WHERE group_id=?)
           OR s.id IN (SELECT subject_id FROM assignments WHERE group_id=?)
           OR s.id IN (SELECT subject_id FROM schedule WHERE group_id=?)
        ORDER BY s.name""", (gid, gid, gid)).fetchall()
    studs = d.execute("SELECT id, name FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,)).fetchall()
    q = """SELECT l.id, l.day, l.topic, s.name sname, s.id sid
        FROM lessons l JOIN subjects s ON s.id=l.subject_id
        WHERE l.group_id=? AND l.day>=? AND l.day<=?"""
    args = [gid, date_from, date_to]
    if sid:
        q += " AND l.subject_id=?"
        args.append(sid)
    q += " ORDER BY l.day, s.name LIMIT 80"
    lessons_raw = d.execute(q, args).fetchall()
    # номер пары в рамках дня + группы по датам
    day_pair = {}
    lessons = []
    day_groups = []  # [{day, count, lessons:[{..., pair}]}]
    by_day = {}
    for l in lessons_raw:
        day = l["day"]
        day_pair[day] = day_pair.get(day, 0) + 1
        item = dict(l)
        item["pair"] = day_pair[day]
        lessons.append(item)
        by_day.setdefault(day, []).append(item)
    for day in sorted(by_day.keys()):
        day_groups.append({"day": day, "count": len(by_day[day]), "lessons": by_day[day]})
    marks = {(m["student_id"], m["lesson_id"]): m for m in d.execute(
        "SELECT * FROM marks WHERE lesson_id IN (SELECT id FROM lessons WHERE group_id=?)", (gid,))}
    rows = []
    for st in studs:
        cells = [dict(marks[(st["id"], l["id"])]) if (st["id"], l["id"]) in marks else None for l in lessons]
        st_stats = stats([c for c in cells if c])
        rows.append({"id": st["id"], "name": st["name"], "cells": cells,
                     "present": st_stats.get("present") or 0,
                     "excused": st_stats.get("excused") or 0,
                     "unexcused": st_stats.get("unexcused") or 0,
                     "missed": st_stats.get("missed") or 0,
                     "missed_hours": st_stats.get("missed_hours") or 0})
    return render_template("head_attendance.html", group=g, lessons=lessons, day_groups=day_groups, rows=rows,
                           subjects=subjects, sid=sid, today=date.today().isoformat(),
                           date_from=date_from, date_to=date_to)

@app.route("/attendance/group/<int:gid>/template")
@role("student", "admin", "teacher", "curator")
def head_attendance_template(gid):
    """Шаблон: только дни с занятиями; под датой — пары 1–4; все ячейки зелёные.
    Пусто = был. н = не был (часы). у = уваж. (без часов).
    Отметки в той же таблице marks — синхрон с журналом преподавателя."""
    d, uid = db(), session["uid"]
    if not can_fill_attendance(uid, gid) and session.get("role") != "admin":
        abort(403)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter as L
    from openpyxl.formatting.rule import FormulaRule

    date_from = request.args.get("from") or (date.today().replace(day=1).isoformat())
    date_to = request.args.get("to") or date.today().isoformat()
    try:
        date.fromisoformat(date_from)
        date.fromisoformat(date_to)
    except ValueError:
        date_from = date.today().replace(day=1).isoformat()
        date_to = date.today().isoformat()
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    try:
        sync_lessons(d, gid)
    except Exception:
        pass

    studs = d.execute(
        "SELECT id, name FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,)
    ).fetchall()

    # занятия группы за период
    lessons = d.execute(
        """SELECT l.id AS id, l.day AS day, l.subject_id AS subject_id, s.name AS sname
           FROM lessons l
           JOIN subjects s ON s.id = l.subject_id
           WHERE l.group_id = ? AND l.day >= ? AND l.day <= ?
           ORDER BY l.day, l.id""",
        (gid, date_from, date_to)).fetchall()

    # пара из schedule + bells
    sch_rows = d.execute(
        """SELECT sch.subject_id, sch.day, sch.weekday, b.num AS pair
           FROM schedule sch
           JOIN bells b ON b.id = sch.bell_id
           WHERE sch.group_id = ?""",
        (gid,)).fetchall()
    pair_by_day_subj = {}
    pair_by_wd_subj = {}
    for s in sch_rows:
        p = int(s["pair"]) if s["pair"] is not None else 1
        if s["day"]:
            pair_by_day_subj.setdefault((s["day"], s["subject_id"]), []).append(p)
        elif s["weekday"] is not None:
            pair_by_wd_subj.setdefault((int(s["weekday"]), s["subject_id"]), []).append(p)

    # day -> ordered list of {pair, id, sname}
    by_day = {}
    day_used_pairs = {}
    for l in lessons:
        day = l["day"]
        sid = l["subject_id"]
        pairs = pair_by_day_subj.get((day, sid))
        if not pairs:
            try:
                wd = date.fromisoformat(day).weekday()  # Mon=0
                pairs = pair_by_wd_subj.get((wd, sid)) or pair_by_wd_subj.get(((wd + 1) % 7, sid))
            except Exception:
                pairs = None
        used = day_used_pairs.setdefault(day, set())
        pair = None
        if pairs:
            for p in pairs:
                if p not in used:
                    pair = p
                    break
            if pair is None:
                pair = pairs[0]
        else:
            pair = (len(used) % 4) + 1
        pair = max(1, min(4, int(pair)))
        if pair in used:
            # ищем свободный слот 1..4
            for cand in range(1, 5):
                if cand not in used:
                    pair = cand
                    break
        used.add(pair)
        by_day.setdefault(day, []).append({"pair": pair, "id": l["id"], "sname": l["sname"]})

    # только дни, где есть хотя бы одно занятие (+ дни после последней известной даты до date_to — для будущих отметок)
    days_with = sorted(by_day.keys())
    days = list(days_with)
    if days_with:
        last = date.fromisoformat(days_with[-1])
        end = date.fromisoformat(date_to)
        # после последнего известного занятия — календарные дни до конца периода (будущее)
        cur = last + timedelta(days=1)
        while cur <= end:
            iso = cur.isoformat()
            if iso not in by_day:
                days.append(iso)
                # заготовка пар 1–4 без lesson id (серые не ставим — зелёные, импорт создаст при необходимости)
                by_day[iso] = [{"pair": p, "id": None, "sname": ""} for p in range(1, 5)]
            cur += timedelta(days=1)
    else:
        # нет занятий — все дни периода с парами 1–4
        d0 = date.fromisoformat(date_from)
        d1 = date.fromisoformat(date_to)
        if (d1 - d0).days > 62:
            d1 = d0 + timedelta(days=62)
        cur = d0
        while cur <= d1:
            iso = cur.isoformat()
            days.append(iso)
            by_day[iso] = [{"pair": p, "id": None, "sname": ""} for p in range(1, 5)]
            cur += timedelta(days=1)

    marks = {(m["student_id"], m["lesson_id"]): m for m in d.execute(
        "SELECT * FROM marks WHERE lesson_id IN (SELECT id FROM lessons WHERE group_id=?)", (gid,))}

    thin = Border(
        left=Side(style="thin", color="D9DEEC"), right=Side(style="thin", color="D9DEEC"),
        top=Side(style="thin", color="D9DEEC"), bottom=Side(style="thin", color="D9DEEC"))
    head_fill = PatternFill("solid", fgColor="14213D")
    head_font = Font(bold=True, color="FFFFFF", size=9)
    sub_fill = PatternFill("solid", fgColor="2B4EFF")
    sub_font = Font(bold=True, color="FFFFFF", size=9)
    green = PatternFill("solid", fgColor="B8F0D3")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sum_fill = PatternFill("solid", fgColor="EEF1F8")

    wb = Workbook()
    wi = wb.active
    wi.title = "Инструкция"
    wi["A1"] = (
        f"Посещаемость · {date_from[8:10]}.{date_from[5:7]} — "
        f"{date_to[8:10]}.{date_to[5:7]}.{date_to[:4]}"
    )
    wi["A1"].font = Font(bold=True, size=13, color="14213D")
    for i, t in enumerate([
        "",
        "Заполнение простое:",
        "  пусто     = был          (зелёный)",
        "  н         = не был       (красный) → идёт в часы",
        "  у         = уважительная (жёлтый)  → часы НЕ считаются",
        "",
        "В шаблоне только дни с занятиями + дни после последнего известного до конца периода.",
        "Лишние столбцы (дней без пар) можно удалить перед загрузкой.",
        "Под датой — столбцы пар (1–4), которые есть в этот день.",
        "",
        "Отметки общие с журналом преподавателя / куратора (одна база).",
        "Сводка справа: Был · Уваж. · Не был · Часы.",
    ], 1):
        wi.cell(i, 1, t)
    wi["A15"] = "пусто/•"; wi["A15"].fill = PatternFill("solid", fgColor="B8F0D3")
    wi["B15"] = "н"; wi["B15"].fill = PatternFill("solid", fgColor="FFC9C9")
    wi["C15"] = "у"; wi["C15"].fill = PatternFill("solid", fgColor="FFF3CD")
    wi.column_dimensions["A"].width = 78

    ws = wb.create_sheet("Посещаемость", 0)
    ws.cell(1, 1, "Студент").font = head_font
    ws.cell(1, 1).fill = head_fill
    ws.cell(1, 1).border = thin
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=1)
    ws.cell(2, 1).border = thin

    col = 2
    col_meta = []  # list of {col, day, pair, lid}
    for day in days:
        slots = sorted(by_day.get(day, []), key=lambda x: x["pair"])
        if not slots:
            continue
        n = len(slots)
        label = f"{day[8:10]}.{day[5:7]}"
        cell = ws.cell(1, col, label)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = center
        cell.border = thin
        if n > 1:
            ws.merge_cells(start_row=1, start_column=col, end_row=1, end_column=col + n - 1)
        for i, slot in enumerate(slots):
            c = ws.cell(2, col + i, str(slot["pair"]))
            c.font = sub_font
            c.fill = sub_fill
            c.alignment = center
            c.border = thin
            ws.cell(3, col + i, day).font = Font(size=1, color="FFFFFF")
            ws.cell(4, col + i, slot["id"] or 0).font = Font(size=1, color="FFFFFF")
            col_meta.append({"col": col + i, "day": day, "pair": slot["pair"], "lid": slot["id"]})
        col += n

    sc = col
    for j, label in enumerate(["Был", "Уваж.", "Не был", "Часы"], 0):
        c = ws.cell(1, sc + j, label)
        c.font = head_font
        c.fill = head_fill
        c.alignment = center
        c.border = thin
        ws.merge_cells(start_row=1, start_column=sc + j, end_row=2, end_column=sc + j)
        ws.cell(2, sc + j).border = thin

    ws.row_dimensions[3].hidden = True
    ws.row_dimensions[4].hidden = True
    ws.row_dimensions[1].height = 20
    ws.row_dimensions[2].height = 18

    for ri, st in enumerate(studs, 5):
        ws.cell(ri, 1, st["name"]).border = thin
        cells_for_stats = []
        for meta in col_meta:
            ci = meta["col"]
            lid = meta["lid"]
            val = ""
            fill = green  # всегда зелёный по умолчанию
            if lid:
                m = marks.get((st["id"], lid))
                if m and m["present"] is not None:
                    cells_for_stats.append(dict(m))
                    if m["present"] == 0:
                        val = "н"
                        fill = PatternFill("solid", fgColor="FFE4E4")
                    elif m["present"] == 2:
                        val = "у"
                        fill = PatternFill("solid", fgColor="FFF3CD")
                    else:
                        val = ""
                        fill = green
            x = ws.cell(ri, ci, val)
            x.fill = fill
            x.border = thin
            x.alignment = center
            x.font = Font(bold=True, size=11)
        stt = stats(cells_for_stats)
        for j, v in enumerate([
            stt.get("present") or 0,
            stt.get("excused") or 0,
            stt.get("unexcused") or 0,
            stt.get("missed_hours") or 0,
        ], 0):
            x = ws.cell(ri, sc + j, v)
            x.fill = sum_fill
            x.alignment = center
            x.border = thin
            if j == 3 and v:
                x.font = Font(bold=True, color="E5484D")

    last_row = 4 + max(len(studs), 1)
    last_data_col = max(sc - 1, 2)
    if studs and col_meta:
        rng = f"B5:{L(last_data_col)}{last_row}"
        ws.conditional_formatting.add(rng, FormulaRule(
            formula=['OR(B5="н",B5="Н",B5="0")'],
            fill=PatternFill("solid", fgColor="FFC9C9")))
        ws.conditional_formatting.add(rng, FormulaRule(
            formula=['OR(B5="у",B5="У",B5="2")'],
            fill=PatternFill("solid", fgColor="FFF3CD")))
        ws.conditional_formatting.add(rng, FormulaRule(
            formula=['OR(B5="",B5="•",B5="✓",B5="1")'],
            fill=PatternFill("solid", fgColor="B8F0D3")))

    ws.column_dimensions["A"].width = 28
    for i in range(2, sc):
        ws.column_dimensions[L(i)].width = 4.5
    for j in range(4):
        ws.column_dimensions[L(sc + j)].width = 9
    ws.freeze_panes = "B5"
    ws.sheet_view.showGridLines = False

    foot = last_row + 2
    ws.cell(foot, 1,
            f"Период {date_from} — {date_to}. пусто=был; н=не был (часы); у=уваж. (без часов). "
            f"Общая база с журналом преподавателя.")
    ws.cell(foot, 1).font = Font(size=9, color="66708C")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=attendance_{gid}_{date_from}_{date_to}.xlsx"},
    )



@app.route("/attendance/group/<int:gid>/import", methods=["POST"])
@role("student", "admin", "teacher", "curator")
def head_attendance_import(gid):
    """Импорт матрицы (студенты × даты) или плоского списка."""
    d, uid = db(), session["uid"]
    if not can_fill_attendance(uid, gid) and session.get("role") != "admin":
        abort(403)
    f = request.files.get("file")
    if not f:
        flash("Выберите файл"); return redirect(url_for("head_attendance"))
    from openpyxl import load_workbook
    wb = load_workbook(f, data_only=True)
    ws = wb["Посещаемость"] if "Посещаемость" in wb.sheetnames else wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    if not rows:
        flash("Пустой файл"); return redirect(url_for("head_attendance"))

    name_map = {r["name"].strip().lower(): r["id"] for r in d.execute(
        "SELECT id, name FROM users WHERE role='student' AND group_id=?", (gid,))}
    ok = err = 0

    def parse_p(raw):
        raw = (str(raw).strip().lower() if raw is not None else "")
        if raw in ("", "•", "✓", "+", "был", "1", "true"):
            return 1
        if raw in ("н", "n", "0", "нет", "не был", "-"):
            return 0
        if raw in ("у", "u", "2", "уваж", "уваж."):
            return 2
        try:
            p = int(float(raw))
            return p if p in (0, 1, 2) else 1
        except Exception:
            return 1

    def set_mark(st_id, lid, p, excuse=""):
        nonlocal ok
        d.execute("INSERT OR IGNORE INTO marks(student_id,lesson_id) VALUES(?,?)", (st_id, lid))
        d.execute("UPDATE marks SET present=? WHERE student_id=? AND lesson_id=?", (p, st_id, lid))
        if p == 2 and excuse:
            d.execute("UPDATE marks SET excuse=? WHERE student_id=? AND lesson_id=?", (excuse[:300], st_id, lid))
        elif p != 2:
            d.execute("UPDATE marks SET excuse='' WHERE student_id=? AND lesson_id=?", (st_id, lid))
        ok += 1

    # Matrix: row1=даты, row2=пары, row3=ISO, row4=lesson id, data from row5
    is_matrix = (len(rows) >= 5 and str(rows[0][0] or "").strip().lower() in ("студент", "фио"))
    if is_matrix:
        # row1 dates (merged), row2 pairs, row3 ISO, row4 lesson ids, data from row5
        if len(rows) > 3 and any(str(rows[2][i] or "").strip() for i in range(1, len(rows[2]))):
            date_row = rows[2]
            pair_row = rows[1] if len(rows) > 1 else []
            lid_row = rows[3] if len(rows) > 3 else [None] * len(date_row)
            data_start = 4
        else:
            date_row = rows[0]
            pair_row = []
            lid_row = [None] * len(date_row)
            data_start = 2
        for ri in range(data_start, len(rows)):
            row = rows[ri]
            if not row or not row[0]:
                continue
            # пропуск строк сводки
            name0 = str(row[0]).strip().lower()
            if name0 in ("был", "уваж.", "не был", "часы"):
                continue
            st_id = name_map.get(name0)
            if not st_id:
                err += 1
                continue
            for ci in range(1, len(row)):
                # не трогаем колонки сводки (числа без даты)
                raw = row[ci] if ci < len(row) else None
                lid = lid_row[ci] if ci < len(lid_row) else None
                try:
                    lid = int(lid) if lid is not None and str(lid).strip() not in ("", "0", "None") else None
                except Exception:
                    lid = None
                day = date_row[ci] if ci < len(date_row) else None
                if hasattr(day, "strftime"):
                    day = day.strftime("%Y-%m-%d")
                else:
                    day = str(day or "").strip()[:10]
                if not lid and not day:
                    continue
                if not lid and day:
                    # найти занятие этого дня (предпочтительно с нужной парой)
                    pair = None
                    try:
                        pair = int(pair_row[ci]) if ci < len(pair_row) and pair_row[ci] not in (None, "") else None
                    except Exception:
                        pair = None
                    lr = d.execute(
                        "SELECT id FROM lessons WHERE group_id=? AND day=? ORDER BY id",
                        (gid, day)).fetchall()
                    if not lr:
                        # создать занятие из schedule, если есть
                        sch = d.execute(
                            """SELECT subject_id, teacher_id FROM schedule
                               WHERE group_id=? AND (day=? OR (IFNULL(day,'')='' AND weekday=?))
                               LIMIT 1""",
                            (gid, day, date.fromisoformat(day).weekday())).fetchone()
                        if not sch:
                            err += 1
                            continue
                        d.execute(
                            "INSERT INTO lessons(group_id, subject_id, day, topic, teacher_id) VALUES(?,?,?,?,?)",
                            (gid, sch["subject_id"], day, "", sch["teacher_id"]))
                        d.commit()
                        lid = d.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                    else:
                        # если несколько — берём по индексу пары
                        if pair and 1 <= pair <= len(lr):
                            lid = lr[pair - 1]["id"]
                        else:
                            lid = lr[0]["id"]
                # пустые ячейки в зелёных = был
                set_mark(st_id, lid, parse_p(raw))
    else:
        # flat: Студент | Дата | Предмет | Посещаемость | Причина
        header = [str(c or "").strip().lower() for c in rows[0]]
        def col(*names):
            for n in names:
                if n in header: return header.index(n)
            return None
        ci_name = col("студент", "фио", "имя")
        ci_day = col("дата", "день")
        ci_pres = col("посещаемость", "явка")
        ci_exc = col("причина", "excuse")
        if ci_name is None:
            flash("Не удалось распознать формат файла"); return redirect(url_for("head_attendance"))
        for row in rows[1:]:
            if not row or not row[ci_name]:
                continue
            st_id = name_map.get(str(row[ci_name]).strip().lower())
            if not st_id:
                err += 1; continue
            day_raw = row[ci_day] if ci_day is not None else None
            if hasattr(day_raw, "strftime"):
                day = day_raw.strftime("%Y-%m-%d")
            else:
                day = str(day_raw or "").strip()[:10]
                if "." in day:
                    parts = day.split(".")
                    if len(parts) == 3:
                        day = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
            lr = d.execute("SELECT id FROM lessons WHERE group_id=? AND day=? LIMIT 1", (gid, day)).fetchone()
            if not lr:
                err += 1; continue
            p = parse_p(row[ci_pres] if ci_pres is not None and ci_pres < len(row) else "")
            excuse = str(row[ci_exc]).strip() if ci_exc is not None and ci_exc < len(row) and row[ci_exc] else ""
            set_mark(st_id, lr["id"], p, excuse)

    # optional reasons sheet
    if "Причины" in wb.sheetnames:
        for row in wb["Причины"].iter_rows(min_row=2, values_only=True):
            if not row or not row[0] or not row[1]:
                continue
            st_id = name_map.get(str(row[0]).strip().lower())
            if not st_id:
                continue
            day_raw = row[1]
            if hasattr(day_raw, "strftime"):
                day = day_raw.strftime("%Y-%m-%d")
            else:
                day = str(day_raw).strip()
                if "." in day and len(day.split(".")) >= 2:
                    parts = day.replace("/", ".").split(".")
                    if len(parts) == 2:
                        day = f"{date.today().year}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
                    elif len(parts) == 3:
                        day = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
            lr = d.execute("SELECT id FROM lessons WHERE group_id=? AND day=? LIMIT 1", (gid, day)).fetchone()
            if lr and row[3]:
                d.execute("INSERT OR IGNORE INTO marks(student_id,lesson_id) VALUES(?,?)", (st_id, lr["id"]))
                d.execute("UPDATE marks SET present=2, excuse=? WHERE student_id=? AND lesson_id=?",
                          (str(row[3])[:300], st_id, lr["id"]))
                ok += 1

    d.commit()
    flash(f"Посещаемость: обновлено {ok}, ошибок {err}")
    return redirect(url_for("head_attendance", sid=request.form.get("sid") or None))



if __name__ == "__main__":
    setup()
    # Показать маршруты bonus при старте (чтобы убедиться, что они есть)
    bonus_rules = [r.rule for r in app.url_map.iter_rules() if "bonus" in r.rule]
    print("Bonus routes:", bonus_rules if bonus_rules else "NONE — ошибка регистрации!")
    app.run(debug=True, use_reloader=True)

