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
_BASE = os.path.dirname(_ROOT)  # campuse_site/
DB = os.path.join(_ROOT, "campus.db")
UPLOADS = os.path.join(_BASE, "uploads")
os.makedirs(UPLOADS, exist_ok=True)
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".rar"}
app = Flask(__name__, template_folder=os.path.join(_BASE, "templates"), static_folder=os.path.join(_BASE, "static"))
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
    """cells: present/grade/excuse. present: 1=был, 0=не был, 2=уваж. причина.
    missed = все отсутствия (0 и 2), excused = только уваж., unexcused = без причины."""
    gr = [c["grade"] for c in cells if c and c["grade"]]
    rec = [c for c in cells if c and c["present"] is not None]
    came = sum(1 for c in rec if c["present"] == 1)
    excused = sum(1 for c in rec if c["present"] == 2)
    unexcused = sum(1 for c in rec if c["present"] == 0)
    missed = excused + unexcused
    return dict(avg=round(sum(gr) / len(gr), 2) if gr else None, missed=missed,
                excused=excused, unexcused=unexcused,
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
  weekday INTEGER, day TEXT, room TEXT DEFAULT '', parity INTEGER NOT NULL DEFAULT 0);
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
    """Реальные занятия за период: шаблон недели + разовые занятия + замены и отмены."""
    bells = {b["id"]: b for b in c.execute("SELECT * FROM bells")}
    rows = c.execute("""SELECT s.*, g.name gname, sub.name sname, t.name tname FROM schedule s
      JOIN groups g ON g.id=s.group_id JOIN subjects sub ON sub.id=s.subject_id JOIN users t ON t.id=s.teacher_id""").fetchall()
    ch = {(x["schedule_id"], x["day"]): x for x in c.execute(
        "SELECT c.*, u.name tname FROM changes c LEFT JOIN users u ON u.id=c.teacher_id")}
    out, d = [], start
    while d <= end:
        iso, par = d.isoformat(), 1 if d.isocalendar()[1] % 2 else 2
        for s in rows:
            if s["day"]:
                if s["day"] != iso: continue
            elif s["weekday"] != d.weekday() or s["parity"] not in (0, par):
                continue
            if gid and s["group_id"] != gid: continue
            x, b = ch.get((s["id"], iso)), bells[s["bell_id"]]
            rep_ = bool(x and x["kind"] == "replace")
            eff = x["teacher_id"] if rep_ and x["teacher_id"] else s["teacher_id"]
            if tid and tid not in (s["teacher_id"], eff): continue
            out.append(dict(id=s["id"], day=iso, weekday=d.weekday(), num=b["num"], start=b["start"], end=b["end"],
                gid=s["group_id"], sid=s["subject_id"], gname=s["gname"], sname=s["sname"], tid=eff,
                tname=x["tname"] if rep_ and x["tname"] else s["tname"], orig_tname=s["tname"],
                room=x["room"] if rep_ and x["room"] else s["room"], status=x["kind"] if x else "ok",
                note=x["note"] if x else "", chid=x["id"] if x else None))
        d += timedelta(days=1)
    out.sort(key=lambda o: (o["day"], o["start"]))
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

def gen_duty(c, gid, start, days, per, task):
    """Автоматический график дежурств: по кругу по алфавиту, продолжая с того, кто дежурил последним."""
    studs = [r[0] for r in c.execute("SELECT id FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,))]
    if not studs: return
    last = c.execute("SELECT student_id FROM duties WHERE group_id=? AND day<? ORDER BY day DESC, id DESC LIMIT 1", (gid, start.isoformat())).fetchone()
    i = (studs.index(last[0]) + 1) % len(studs) if last and last[0] in studs else 0
    n, day = 0, start
    while n < days:
        if day.weekday() < 5:
            for _ in range(min(per, len(studs))):
                c.execute("INSERT OR IGNORE INTO duties(group_id,day,student_id,task) VALUES(?,?,?,?)", (gid, day.isoformat(), studs[i % len(studs)], task))
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

def has_journal_access(teacher_id, group_id, subject_id, need="view"):
    """need: 'view' или 'edit'. edit подразумевает view."""
    d = db()
    row = d.execute(
        "SELECT level FROM journal_access WHERE teacher_id=? AND group_id=? AND subject_id=?",
        (teacher_id, group_id, subject_id)).fetchone()
    if row:
        if need == "view":
            return True
        return row["level"] == "edit"
    # fallback: assignment или пара в расписании → edit
    if d.execute("SELECT 1 FROM assignments WHERE teacher_id=? AND group_id=? AND subject_id=?",
                 (teacher_id, group_id, subject_id)).fetchone():
        grant_journal_access(teacher_id, group_id, subject_id, "edit")
        return True
    if d.execute("SELECT 1 FROM schedule WHERE teacher_id=? AND group_id=? AND subject_id=?",
                 (teacher_id, group_id, subject_id)).fetchone():
        grant_journal_access(teacher_id, group_id, subject_id, "edit")
        return True
    return False


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
        if not (gid and d.execute("SELECT 1 FROM assignments WHERE teacher_id=? AND group_id=?", (session["uid"], gid)).fetchone()):
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
@role("teacher")
def teacher():
    d, uid = db(), session["uid"]
    sync_lessons(d)
    # журналы по явным правам + назначениям + расписанию
    rows = d.execute("""SELECT g.id gid, s.id sid, g.name gname, s.name sname,
      COALESCE(ja.level, 'edit') AS access_level,
      (SELECT COUNT(*) FROM users WHERE group_id=g.id AND role='student') n,
      (SELECT COUNT(*) FROM lessons WHERE group_id=g.id AND subject_id=s.id) l,
      (SELECT group_concat(t.name, ', ') FROM assignments a2 JOIN users t ON t.id=a2.teacher_id
        WHERE a2.group_id=g.id AND a2.subject_id=s.id AND a2.teacher_id<>?) co
      FROM groups g JOIN subjects s
      LEFT JOIN journal_access ja ON ja.group_id=g.id AND ja.subject_id=s.id AND ja.teacher_id=?
      WHERE EXISTS(SELECT 1 FROM journal_access j WHERE j.teacher_id=? AND j.group_id=g.id AND j.subject_id=s.id)
         OR EXISTS(SELECT 1 FROM assignments a WHERE a.teacher_id=? AND a.group_id=g.id AND a.subject_id=s.id)
         OR EXISTS(SELECT 1 FROM schedule sch WHERE sch.teacher_id=? AND sch.group_id=g.id AND sch.subject_id=s.id)
      ORDER BY g.name, s.name""", (uid, uid, uid, uid, uid)).fetchall()
    groups = d.execute("""SELECT DISTINCT g.id, g.name FROM groups g
      WHERE g.id IN (SELECT group_id FROM journal_access WHERE teacher_id=?)
         OR g.id IN (SELECT group_id FROM assignments WHERE teacher_id=?)
         OR g.id IN (SELECT group_id FROM schedule WHERE teacher_id=?)
      ORDER BY g.name""", (uid, uid, uid)).fetchall()
    # студенты групп преподавателя — для личных объявлений
    students = d.execute("""SELECT DISTINCT u.id, u.name, g.name gname FROM users u
      JOIN groups g ON g.id=u.group_id
      WHERE u.role='student' AND (
        g.id IN (SELECT group_id FROM journal_access WHERE teacher_id=?)
        OR g.id IN (SELECT group_id FROM assignments WHERE teacher_id=?)
        OR g.id IN (SELECT group_id FROM schedule WHERE teacher_id=?)
      ) ORDER BY g.name, u.name""", (uid, uid, uid)).fetchall()
    return render_template("teacher.html", rows=rows, groups=groups, students=students,
                           anns=announcements("WHERE a.author_id=?", (uid,)), can_del=True,
                           **feed(d, tid=uid))

def course(gid, sid, need="edit"):
    """Курс (группа+предмет). Для преподавателя проверяет journal_access (с автовыдачей по назначению/расписанию)."""
    d = db()
    base = d.execute("""SELECT g.id gid, s.id sid, g.name gname, s.name sname
      FROM groups g, subjects s WHERE g.id=? AND s.id=?""", (gid, sid)).fetchone()
    if not base:
        abort(404)
    if session.get("role") == "teacher":
        if not has_journal_access(session["uid"], gid, sid, need=need):
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
@role("teacher")
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
@role("teacher")
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
    heads = ["Студент", "Средний балл", "Пропуски", "Уваж.", "Посещаемость", "Доп. баллы"] + [f'{l["day"][8:10]}.{l["day"][5:7]}' for l in lessons]
    for i, h in enumerate(heads, 1):
        x = ws.cell(4, i, h); x.font = Font(bold=True, color="FFFFFF"); x.fill = fill("14213D"); x.alignment = center; x.border = box
    ws.cell(5, 1, "Тема занятия")
    for i in range(1, 7 + len(lessons)):
        ws.cell(5, i).border = box; ws.cell(5, i).alignment = center; ws.cell(5, i).font = Font(italic=True, size=9, color="66708C")
    for i, l in enumerate(lessons, 7):
        ws.cell(5, i, l["topic"] or "")
    gfill = {2: "FFC9C9", 3: "FFD84D", 4: "CFE0FF", 5: "B8F0D3"}
    for ri, r in enumerate(rows, 6):
        risk = (r["avg"] and r["avg"] < 3) or (r["pct"] is not None and r["pct"] < 70)
        vals = [r["name"], r["avg"], r["missed"], r.get("excused") or 0,
                r["pct"] / 100 if r["pct"] is not None else None, r.get("bonus_balance") or 0]
        for i, v in enumerate(vals, 1):
            x = ws.cell(ri, i, v); x.border = box
            x.alignment = Alignment(horizontal="left" if i == 1 else "center", vertical="center")
            if risk: x.fill = fill("FFF5F5")
            if i == 3 and v: x.fill = fill("FFE4E4"); x.font = Font(bold=True, color="E5484D")
            if i == 4 and v: x.fill = fill("FFF3CD"); x.font = Font(bold=True, color="B45309")
            if i == 6 and v: x.fill = fill("FFF8DC"); x.font = Font(bold=True)
        ws.cell(ri, 2).number_format = "0.00"; ws.cell(ri, 5).number_format = "0%"
        for j, cl in enumerate(r["cells"], 7):
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
        ws.cell(foot, 7 + k, sum(1 for r in rows if r["cells"][k] and r["cells"][k]["present"] == 1))
    for i in range(1, 7 + len(lessons)):
        x = ws.cell(foot, i); x.font = Font(bold=True); x.fill = fill("EEF1F8"); x.border = box; x.alignment = center
    # legend
    leg = foot + 2
    ws.cell(leg, 1, "Легенда: • — присутствовал; н — не был; у — уважительная причина; 2–5 — оценка")
    ws.cell(leg, 1).font = Font(color="66708C", size=9)
    ws.column_dimensions["A"].width = 30
    for i in range(2, 7): ws.column_dimensions[L(i)].width = 13
    for i in range(7, 7 + len(lessons)): ws.column_dimensions[L(i)].width = 11
    ws.row_dimensions[4].height = 28; ws.row_dimensions[5].height = 40
    ws.freeze_panes = "G6"; ws.page_setup.orientation = "landscape"
    buf = io.BytesIO(); wb.save(buf)
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=journal_{gid}_{sid}.xlsx"})

@app.post("/api/mark")
@role("teacher")
def mark():
    j, d = request.get_json(), db()
    l = d.execute("SELECT id, group_id, subject_id FROM lessons WHERE id=?", (j["lesson"],)).fetchone()
    if not l: abort(404)
    if not has_journal_access(session["uid"], l["group_id"], l["subject_id"], need="edit"):
        abort(403)
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
    return jsonify(ok=True,
                   present=cur["present"] if cur else None,
                   grade=cur["grade"] if cur else None,
                   excuse=(cur["excuse"] if cur and "excuse" in cur.keys() else "") or "",
                   **stats(allm))

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
            d.execute("DELETE FROM lessons WHERE id=?", (l["id"],))
        elif act == "topic":
            d.execute("UPDATE lessons SET topic=? WHERE id=?", (j["topic"].strip(), l["id"]))
        elif act == "all":  # всех без отметки посещения делаем присутствующими
            d.execute("INSERT OR IGNORE INTO marks(student_id,lesson_id) SELECT id,? FROM users WHERE role='student' AND group_id=?", (l["id"], c["gid"]))
            d.execute("UPDATE marks SET present=1 WHERE lesson_id=? AND present IS NULL", (l["id"],))
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
    # если записи баланса ещё нет — создадим
    if not bp:
        d.execute("""INSERT INTO bonus_points(student_id, subject_id, balance, conditions, updated)
          VALUES(?,?,?,?,?)""", (uid, row["subject_id"], 0, "", now))
    d.execute("UPDATE bonus_points SET balance=balance-?, updated=? WHERE student_id=? AND subject_id=?",
              (used, now, uid, row["subject_id"]))
    d.execute("""INSERT INTO bonus_applications(student_id,subject_id,lesson_id,points_used,original_grade,new_grade,applied_at)
      VALUES(?,?,?,?,?,?,?)""", (uid, row["subject_id"], lid, used, row["grade"], new_g, now))
    d.execute("UPDATE marks SET grade=? WHERE student_id=? AND lesson_id=?", (new_g, uid, lid))
    d.commit()
    allm = d.execute("""SELECT * FROM marks WHERE student_id=? AND lesson_id IN
      (SELECT id FROM lessons WHERE group_id=? AND subject_id=?)""", (uid, row["group_id"], row["subject_id"])).fetchall()
    return jsonify(ok=True, grade=new_g, used=used, balance=bal - used, **stats(allm))

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
    return render_template("student.html", my_duty=my_duty, hw=hw, subs=subs, grp=me["gname"], avg=tot["avg"] or 0, pct=tot["pct"] or 0,
                           cnt=sum(1 for c in allc if c.get("grade")), groups=None,
                           my_finals=my_finals,
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
    return render_template("admin.html", users=users, sbg=sbg, groups=q("SELECT * FROM groups ORDER BY name"),
        subjects=q("SELECT * FROM subjects ORDER BY name"), teachers=q("SELECT * FROM users WHERE role='teacher' ORDER BY name"),
        assigns=q("""SELECT a.id, g.name gname, s.name sname, t.name tname FROM assignments a JOIN groups g ON g.id=a.group_id
                     JOIN subjects s ON s.id=a.subject_id JOIN users t ON t.id=a.teacher_id ORDER BY g.name, s.name, t.name"""),
        journal_access=jacc,
        counts=dict(groups=one("SELECT COUNT(*) FROM groups"), students=one("SELECT COUNT(*) FROM users WHERE role='student'"),
                    teachers=one("SELECT COUNT(*) FROM users WHERE role='teacher'"), lessons=one("SELECT COUNT(*) FROM lessons")),
        anns=announcements(), can_del=True, all_ok=True, students=students_list)

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
            if r not in ("teacher", "student", "dispatcher"): raise ValueError("Выберите роль")
            if r == "student" and not gid: raise ValueError("Для студента выберите группу")
            d.execute("INSERT INTO users(login,pw,name,role,group_id) VALUES(?,?,?,?,?)",
                      (f["login"].strip(), generate_password_hash(f["password"]), f["name"].strip(), r, gid if r == "student" else None))
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

def _norm_space(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

def _parse_room(text):
    m = re.search(r"(?:ауд\.?|каб\.?)\s*([0-9A-Za-zА-Яа-я.,/\-\s]+)", text or "", re.I)
    if m:
        return _norm_space(m.group(1)).rstrip(".,")
    return ""

def _split_subj_teacher(cell):
    """'Русский язык Буртасова И.Л.' / 'отмена' / '-------' -> (subj, teacher, room, is_empty, is_cancel)"""
    t = _norm_space(cell)
    if not t or set(t) <= {"-", "—", "–", "."} or t.lower() in ("-------", "—", "-"):
        return None, None, "", True, False
    if "отмен" in t.lower() or t.lower() in ("отм", "отмена"):
        return None, None, "", False, True
    room = _parse_room(t)
    t2 = re.sub(r"(?:ауд\.?|каб\.?)\s*[0-9A-Za-zА-Яа-я.,/\-\s]+", "", t, flags=re.I).strip(" ,;")
    # teacher: last words like Фамилия И.О. or Фамилия И.О., Фамилия2 И.О.
    m = re.search(r"([А-ЯЁA-Z][а-яёa-z\-]+(?:\s+[А-ЯЁA-Z]\.[А-ЯЁA-Z]\.?)(?:\s*,\s*[А-ЯЁA-Z][а-яёa-z\-]+\s+[А-ЯЁA-Z]\.[А-ЯЁA-Z]\.?)*)\s*$", t2)
    if m:
        teacher = _norm_space(m.group(1))
        subj = _norm_space(t2[:m.start()])
    else:
        # fallback: last 2-3 tokens
        parts = t2.split()
        if len(parts) >= 2 and re.match(r"[А-ЯЁA-Z]", parts[-1]):
            teacher = " ".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
            subj = " ".join(parts[:-2]) if len(parts) > 2 else parts[0]
        else:
            teacher, subj = "", t2
    return subj or None, teacher or None, room, False, False

def ensure_group(d, name):
    name = _norm_space(name)
    if not name:
        return None
    r = d.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone()
    if r:
        return r["id"]
    return d.execute("INSERT INTO groups(name) VALUES(?)", (name,)).lastrowid

def ensure_subject(d, name):
    name = _norm_space(name)
    if not name:
        return None
    r = d.execute("SELECT id FROM subjects WHERE name=?", (name,)).fetchone()
    if r:
        return r["id"]
    # case-insensitive
    r = d.execute("SELECT id, name FROM subjects").fetchall()
    for x in r:
        if x["name"].lower() == name.lower():
            return x["id"]
    return d.execute("INSERT INTO subjects(name) VALUES(?)", (name,)).lastrowid

def ensure_teacher(d, name):
    name = _norm_space(name)
    if not name:
        return None
    # take first teacher if comma-separated
    name = name.split(",")[0].strip()
    r = d.execute("SELECT id FROM users WHERE role='teacher' AND name=?", (name,)).fetchone()
    if r:
        return r["id"]
    r = d.execute("SELECT id, name FROM users WHERE role='teacher'").fetchall()
    for x in r:
        if x["name"].lower() == name.lower():
            return x["id"]
    # auto-create
    base = re.sub(r"[^a-z0-9]", "", name.lower().replace("ё", "e"))[:12] or "t"
    login = base
    n = 1
    while d.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
        n += 1
        login = f"{base}{n}"
    pw = generate_password_hash("teacher123")
    return d.execute("INSERT INTO users(login,pw,name,role) VALUES(?,?,?,'teacher')",
                     (login, pw, name)).lastrowid

def ensure_bell(d, num, start=None, end=None):
    r = d.execute("SELECT id, start, end FROM bells WHERE num=?", (num,)).fetchone()
    if r:
        if start and end and (r["start"] != start or r["end"] != end):
            d.execute("UPDATE bells SET start=?, end=? WHERE id=?", (start, end, r["id"]))
        return r["id"]
    start = start or "09:00"
    end = end or "10:35"
    return d.execute("INSERT INTO bells(num,start,end) VALUES(?,?,?)", (num, start, end)).lastrowid

def parse_time_range(s):
    m = re.search(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})", s or "")
    if m:
        return m.group(1), m.group(2)
    return None, None

def import_weekly_docx(d, path):
    """Импорт недельного расписания из DOCX (таблицы по группам)."""
    from docx import Document
    doc = Document(path)
    stats = {"groups": 0, "lessons": 0, "created_groups": [], "created_subjects": [], "created_teachers": []}
    # collect paragraphs + tables in order
    # python-docx doesn't give mixed order easily; process by matching group headers then following table
    paras = [p.text.strip() for p in doc.paragraphs]
    tables = doc.tables
    # map: for each table index, find preceding group header
    group_re = re.compile(r"РАСПИСАНИЕ\s+ГРУПП[АЫИ]\s+([0-9A-Za-zА-Яа-я\-]+)", re.I)
    headers = []
    for i, p in enumerate(paras):
        m = group_re.search(p)
        if m:
            headers.append((i, m.group(1).strip()))
    # assign tables to groups: sequential
    if not headers or not tables:
        return stats
    # If more tables than headers, pair by index
    for ti, table in enumerate(tables):
        if ti >= len(headers):
            break
        gname = headers[ti][1]
        # normalize group name like 11-А-26
        gname = gname.upper().replace(" ", "")
        before_g = d.execute("SELECT id FROM groups WHERE name=?", (gname,)).fetchone()
        gid = ensure_group(d, gname)
        if not before_g:
            stats["created_groups"].append(gname)
        stats["groups"] += 1
        # clear weekly (weekday-based) schedule for this group
        d.execute("DELETE FROM schedule WHERE group_id=? AND weekday IS NOT NULL", (gid,))
        # parse header row for weekdays
        rows = table.rows
        if len(rows) < 2:
            continue
        head = [_norm_space(c.text).lower() for c in rows[0].cells]
        # find day columns
        day_cols = {}
        for ci, h in enumerate(head):
            for k, v in WD_RU.items():
                if k in h:
                    day_cols[ci] = v
            for k, v in WD_SHORT.items():
                if h == k or h.startswith(k):
                    day_cols[ci] = v
        pair_col = 0
        time_col = 1 if len(head) > 1 else None
        for ri in range(1, len(rows)):
            cells = [_norm_space(c.text) for c in rows[ri].cells]
            if not cells:
                continue
            # pair number
            try:
                pnum = int(re.search(r"\d+", cells[pair_col]).group())
            except Exception:
                continue
            start, end = (None, None)
            if time_col is not None and time_col < len(cells):
                start, end = parse_time_range(cells[time_col])
            bid = ensure_bell(d, pnum, start, end)
            for ci, wd in day_cols.items():
                if ci >= len(cells):
                    continue
                subj, teacher, room, empty, cancel = _split_subj_teacher(cells[ci])
                if empty or cancel or not subj:
                    continue
                before_s = d.execute("SELECT id FROM subjects WHERE name=?", (subj,)).fetchone()
                sid = ensure_subject(d, subj)
                if not before_s:
                    stats["created_subjects"].append(subj)
                tid = ensure_teacher(d, teacher) if teacher else None
                if not tid:
                    # placeholder teacher
                    tid = ensure_teacher(d, "Не назначен")
                else:
                    if teacher and teacher not in stats["created_teachers"]:
                        # check if newly created is hard; skip tracking for simplicity
                        pass
                d.execute("""INSERT INTO schedule(group_id,subject_id,teacher_id,bell_id,weekday,day,room,parity)
                  VALUES(?,?,?,?,?,NULL,?,0)""", (gid, sid, tid, bid, wd, room or ""))
                d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)",
                          (tid, gid, sid))
                stats["lessons"] += 1
    d.commit()
    return stats

def import_replacements_pdf(d, path, day_iso=None):
    """Импорт замен из PDF 'Изменения в расписании на ...'."""
    import pdfplumber
    stats = {"replacements": 0, "cancels": 0, "day": day_iso}
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        if not day_iso:
            m = re.search(r'на\s+"?(\d{1,2})"?\s*(\w+)\s*(\d{4})', text, re.I)
            # or 01 октября 2026 / 01.10.2026
            m2 = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
            m3 = re.search(r'"(\d{1,2})"\s+(\w+)\s+(\d{4})', text)
            months = {"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
                      "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}
            if m2:
                day_iso = f"{m2.group(3)}-{int(m2.group(2)):02d}-{int(m2.group(1)):02d}"
            elif m3 and m3.group(2).lower() in months:
                day_iso = f"{m3.group(3)}-{months[m3.group(2).lower()]:02d}-{int(m3.group(1)):02d}"
            elif m and m.group(2).lower() in months:
                day_iso = f"{m.group(3)}-{months[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
        stats["day"] = day_iso
        if not day_iso:
            return stats
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                for row in table[1:] if table else []:
                    if not row or len(row) < 3:
                        continue
                    cells = [_norm_space(c) for c in row]
                    # try: pair, group, original, replacement, room
                    try:
                        pnum = int(re.search(r"\d+", cells[0] or "").group())
                    except Exception:
                        continue
                    gname = (cells[1] or "").upper().replace(" ", "")
                    if not gname or not re.search(r"\d", gname):
                        continue
                    gid = ensure_group(d, gname)
                    orig = cells[2] if len(cells) > 2 else ""
                    repl = cells[3] if len(cells) > 3 else ""
                    room = cells[4] if len(cells) > 4 else ""
                    room = _norm_space(room)
                    # find schedule entry for group+pair on this weekday
                    wd = date.fromisoformat(day_iso).weekday()
                    bell = d.execute("SELECT id FROM bells WHERE num=?", (pnum,)).fetchone()
                    if not bell:
                        bell_id = ensure_bell(d, pnum)
                    else:
                        bell_id = bell["id"]
                    sch = d.execute("""SELECT * FROM schedule WHERE group_id=? AND bell_id=? AND weekday=?""",
                                    (gid, bell_id, wd)).fetchone()
                    if not sch:
                        # try any schedule that day via parity 0
                        sch = d.execute("""SELECT * FROM schedule WHERE group_id=? AND bell_id=? AND (weekday=? OR day=?)""",
                                        (gid, bell_id, wd, day_iso)).fetchone()
                    if "отмен" in (repl or "").lower() or (repl or "").lower() in ("отм", "отмена"):
                        if sch:
                            d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
                              ON CONFLICT(schedule_id,day) DO UPDATE SET kind='cancel', teacher_id=NULL, room='', note='отмена'""",
                                      (sch["id"], day_iso, "cancel", None, "", "отмена"))
                            stats["cancels"] += 1
                        continue
                    subj, teacher, room2, empty, cancel = _split_subj_teacher(repl or "")
                    if empty:
                        continue
                    room = room or room2
                    tid = ensure_teacher(d, teacher) if teacher else None
                    if sch:
                        d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(schedule_id,day) DO UPDATE SET kind='replace', teacher_id=excluded.teacher_id,
                            room=excluded.room, note=excluded.note""",
                                  (sch["id"], day_iso, "replace", tid, room or "", (subj or "")[:200]))
                        if tid:
                            d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)",
                                      (tid, gid, sch["subject_id"]))
                        stats["replacements"] += 1
                    else:
                        # no base schedule — create one-off entry for this day
                        if subj:
                            sid = ensure_subject(d, subj)
                            tid = tid or ensure_teacher(d, "Не назначен")
                            d.execute("""INSERT INTO schedule(group_id,subject_id,teacher_id,bell_id,weekday,day,room,parity)
                              VALUES(?,?,?,?,NULL,?,?,0)""", (gid, sid, tid, bell_id, day_iso, room or ""))
                            d.execute("INSERT OR IGNORE INTO assignments(teacher_id,group_id,subject_id) VALUES(?,?,?)",
                                      (tid, gid, sid))
                            stats["replacements"] += 1
    d.commit()
    return stats

def import_rooms_pdf(d, path, day_iso=None):
    """Импорт кабинетов (матрица группа × пара). Создаёт все группы из файла.
    Обновляет room в schedule/changes; если занятия ещё нет — только создаёт группы."""
    import pdfplumber
    stats = {"rooms": 0, "groups_created": 0, "groups_seen": 0, "day": day_iso, "cancels": 0}
    group_re = re.compile(r"^([0-9]{1,3}[-–]?[А-ЯA-Zа-яa-z]{1,3}[-–]?[0-9]{2,4}|[0-9]{2,3}-[А-ЯA-Z]{1,3}-[0-9]{2})", re.I)

    def norm_g(s):
        s = _norm_space(s).upper().replace(" ", "").replace("–", "-")
        return s

    def apply_room(gid, pnum, room, day_iso, wd):
        bell = d.execute("SELECT id FROM bells WHERE num=?", (pnum,)).fetchone()
        if not bell:
            bell_id = ensure_bell(d, pnum)
        else:
            bell_id = bell["id"]
        room = _norm_space(room)
        is_cancel = room.lower() in ("отм", "отмена")
        sch = d.execute(
            "SELECT id FROM schedule WHERE group_id=? AND bell_id=? AND (weekday=? OR day=?)",
            (gid, bell_id, wd, day_iso)).fetchone()
        if not sch:
            return False
        if is_cancel:
            d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
              ON CONFLICT(schedule_id,day) DO UPDATE SET kind='cancel', note='отмена'""",
                      (sch["id"], day_iso, "cancel", None, "", "отмена"))
            stats["cancels"] += 1
        else:
            d.execute("""INSERT INTO changes(schedule_id,day,kind,teacher_id,room,note) VALUES(?,?,?,?,?,?)
              ON CONFLICT(schedule_id,day) DO UPDATE SET
                room=excluded.room,
                kind=CASE WHEN kind='cancel' THEN 'cancel' ELSE COALESCE(kind,'replace') END""",
                      (sch["id"], day_iso, "replace", None, room, ""))
            stats["rooms"] += 1
        return True

    with pdfplumber.open(path) as pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        if not day_iso:
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", full_text)
            if m:
                y = int(m.group(3))
                if y < 100:
                    y += 2000
                day_iso = f"{y}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            else:
                m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", full_text)
                if m:
                    y = int(m.group(3))
                    if y < 100:
                        y += 2000
                    day_iso = f"{y}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        stats["day"] = day_iso
        if not day_iso:
            return stats
        wd = date.fromisoformat(day_iso).weekday()

        # 1) try structured tables
        parsed_any = False
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                if not table or len(table) < 2:
                    continue
                head = [_norm_space(c) for c in table[0]]
                pair_cols = {}
                for ci, h in enumerate(head):
                    m = re.match(r"^(\d+)$", h or "")
                    if m:
                        pair_cols[ci] = int(m.group(1))
                # sometimes header is second row
                if not pair_cols and len(table) > 1:
                    head2 = [_norm_space(c) for c in table[1]]
                    for ci, h in enumerate(head2):
                        m = re.match(r"^(\d+)$", h or "")
                        if m:
                            pair_cols[ci] = int(m.group(1))
                    data_start = 2
                else:
                    data_start = 1
                if not pair_cols:
                    continue
                parsed_any = True
                for row in table[data_start:]:
                    cells = [_norm_space(c) for c in (row or [])]
                    if not cells:
                        continue
                    gname = norm_g(cells[0] or "")
                    if not gname or not re.search(r"\d", gname):
                        # try find group-like token in first cells
                        for c in cells[:2]:
                            g2 = norm_g(c or "")
                            if group_re.match(g2):
                                gname = g2
                                break
                    if not gname or not re.search(r"\d", gname):
                        continue
                    before = d.execute("SELECT id FROM groups WHERE name=?", (gname,)).fetchone()
                    gid = ensure_group(d, gname)
                    stats["groups_seen"] += 1
                    if not before:
                        stats["groups_created"] += 1
                    for ci, pnum in pair_cols.items():
                        if ci >= len(cells):
                            continue
                        room = cells[ci]
                        if not room or room in ("?", "-", "—"):
                            continue
                        apply_room(gid, pnum, room, day_iso, wd)

        # 2) text-line fallback: "11-А-26 430 404 204 204"
        if stats["rooms"] == 0 and stats["groups_created"] == 0:
            for line in full_text.splitlines():
                line = _norm_space(line)
                m = group_re.match(line.replace(" ", ""))
                # better: start of line is group
                parts = line.split()
                if not parts:
                    continue
                gname = norm_g(parts[0])
                if not group_re.match(gname) and not re.match(r"^\d{1,3}-[А-ЯA-Z]{1,3}-\d{2}$", gname):
                    # try parts[0] with cyrillic
                    if not re.search(r"\d", parts[0]):
                        continue
                    gname = norm_g(parts[0])
                    if not re.search(r"[А-ЯA-Z]", gname):
                        continue
                before = d.execute("SELECT id FROM groups WHERE name=?", (gname,)).fetchone()
                gid = ensure_group(d, gname)
                stats["groups_seen"] += 1
                if not before:
                    stats["groups_created"] += 1
                # remaining tokens as rooms for pairs 1..n
                rooms = parts[1:]
                for i, room in enumerate(rooms, 1):
                    if not room or room in ("?", "-", "—"):
                        continue
                    if room.lower() in ("отм", "отмена") or re.match(r"^[\d,./А-ЯA-Za-z]+$", room):
                        apply_room(gid, i, room, day_iso, wd)

        # 3) always harvest all group-like tokens from text
        for m in re.finditer(r"\b(\d{1,3}-[А-ЯA-Zа-я]{1,3}-\d{2,4})\b", full_text, re.I):
            gname = norm_g(m.group(1))
            before = d.execute("SELECT id FROM groups WHERE name=?", (gname,)).fetchone()
            ensure_group(d, gname)
            if not before:
                stats["groups_created"] += 1
                stats["groups_seen"] += 1

    d.commit()
    return stats

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
            elif "zamen" in fname.lower() or "замен" in fname.lower():
                kind = "replace"
            elif "kabinet" in fname.lower() or "кабинет" in fname.lower():
                kind = "rooms"
            elif ext == "pdf":
                kind = "replace"  # default for pdf
            else:
                flash("Неизвестный тип файла. Используйте DOCX (неделя) или PDF (замены/кабинеты).")
                return redirect(url_for("dispatcher"))
        if kind == "weekly":
            if ext != "docx":
                flash("Недельное расписание: нужен файл .docx")
                return redirect(url_for("dispatcher"))
            st = import_weekly_docx(d, tmp.name)
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
            gen_duty(d, gid, date.fromisoformat(f["start"]), max(1, min(int(f["days"]), 60)), max(1, min(int(f["per"]), 5)), task)
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
    if role not in ("teacher", "student"): abort(403)
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

if __name__ == "__main__":
    setup()
    # Показать маршруты bonus при старте (чтобы убедиться, что они есть)
    bonus_rules = [r.rule for r in app.url_map.iter_rules() if "bonus" in r.rule]
    print("Bonus routes:", bonus_rules if bonus_rules else "NONE — ошибка регистрации!")
    app.run(debug=True, use_reloader=True)

