# -*- coding: utf-8 -*-
"""Journal: stats, access control, load rows, finals helpers."""
from datetime import date, datetime, timedelta
from flask import session, abort



def get_db():
    from core import db
    return db()

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

def grant_journal_access(teacher_id, group_id, subject_id, level="edit", granted_by=None):
    d = get_db()
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
    d = get_db()
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

def course(gid, sid, need="edit"):
    """Курс (группа+предмет). Для преподавателя проверяет journal_access (с автовыдачей по назначению/расписанию)."""
    d = get_db()
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
    d = get_db(); sync_lessons(d, gid)
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

