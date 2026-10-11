# -*- coding: utf-8 -*-
from flask import render_template, request, redirect, url_for, session, jsonify, flash, abort, Response, send_from_directory
from datetime import date, datetime, timedelta
from routes import FlatBlueprint
from core import (
    db, role, stats, has_journal_access, grant_journal_access, notify, notify_many,
    setting, minutes, gap_label, overlap, occurrences, sync_lessons, feed, Bad,
    WD, WDF, UPLOADS, ALLOWED_EXT, _ROOT, DB, migrate, seed_marks, setup, can_fill_attendance,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import io, re, os, sqlite3, shutil, tempfile, random

bp = FlatBlueprint("api", __name__)
from core import _ensure_bonus_tables, fmt_period, period_for, get_final_kinds, _seed_final_kinds

@bp.post("/api/mark")
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
        # Смена посещаемости всегда снимает оценку (был / не был / уваж. / сброс)
        d.execute("UPDATE marks SET present=?, grade=NULL WHERE student_id=? AND lesson_id=?", (p, sid, lid))
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


@bp.post("/api/mark/bulk")
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


@bp.post("/api/lesson")
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
@bp.post("/api/bonus/set")
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

@bp.post("/api/bonus/rules")
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


@bp.post("/api/bonus/apply")
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

@bp.post("/api/bonus/cancel")
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


# ---------- Итоговые оценки ----------
# ---------- Итоговые оценки ----------
DEFAULT_FINAL_KINDS = {
    "diff_winter": "Диф. зачёт (зима)",
    "interim": "Промежуточная аттестация",
    "exam_summer": "Экзамен (лето)",
}
# alias for backward compat
FINAL_KINDS = DEFAULT_FINAL_KINDS


@bp.post("/api/finals/set")
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

@bp.post("/api/finals/period")
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

@bp.post("/api/finals/kind")
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

