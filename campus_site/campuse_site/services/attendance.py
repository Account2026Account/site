# -*- coding: utf-8 -*-
"""Attendance / headman helpers."""



def get_db():
    from core import db
    return db()

def can_fill_attendance(user_id, group_id):
    """Староста и зам. старосты могут заполнять посещаемость своей группы."""
    d = get_db()
    g = d.execute("SELECT head_id, deputy_head_id, curator_id FROM groups WHERE id=?", (group_id,)).fetchone()
    if not g:
        return False
    if user_id in (g["head_id"], g["deputy_head_id"], g["curator_id"]):
        return True
    u = d.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    return u and u["role"] in ("admin", "teacher", "curator")

def head_group(uid):
    r = get_db().execute("SELECT g.id FROM groups g JOIN users u ON u.group_id=g.id WHERE g.head_id=? AND u.id=?", (uid, uid)).fetchone()
    return r[0] if r else None

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

