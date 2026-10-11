# -*- coding: utf-8 -*-
from flask import render_template, request, redirect, url_for, session, jsonify, flash, abort, Response, send_from_directory
from datetime import date, datetime, timedelta
from routes import FlatBlueprint
from core import db, role, stats, has_journal_access, grant_journal_access, notify, notify_many, setting, minutes, gap_label, overlap, occurrences, sync_lessons, feed, Bad, WD, WDF, UPLOADS, ALLOWED_EXT, _ROOT, DB, migrate, seed_marks, setup, can_fill_attendance
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import io, re, os, sqlite3, shutil, tempfile, random

bp = FlatBlueprint("student", __name__)
from core import head_group

# ---------- Студент ----------
@bp.route("/student")
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


# ---------- Дежурства ----------
# ---------- Дежурства (староста) ----------
@bp.route("/duty")
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

@bp.post("/duty/<act>")
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
# ---------- Задания ----------

@bp.route("/homework")
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

@bp.post("/homework/add")
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

@bp.post("/homework/delete")
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

@bp.post("/homework/done")
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

@bp.post("/homework/note")
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

@bp.route("/uploads/<path:name>")
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


@bp.get("/api/bonus/ping")
def bonus_ping():
    """Проверка, что маршруты доп. баллов загружены."""
    return jsonify(ok=True, routes=[r.rule for r in app.url_map.iter_rules() if "bonus" in r.rule])


# ---------- Посещаемость старосты ----------
@bp.post("/attendance")
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



@bp.route("/attendance/group")
@role("student", "admin", "teacher", "curator")
def head_attendance():
    """Журнал посещаемости для старосты / зама (только своя группа)."""
    d, uid = db(), session["uid"]
    me = d.execute("SELECT group_id FROM users WHERE id=?", (uid,)).fetchone()
    my_gid = me["group_id"] if me and me["group_id"] else None
    g = None
    # 1) своя группа, где пользователь староста/зам
    if my_gid:
        g = d.execute(
            "SELECT * FROM groups WHERE id=? AND (head_id=? OR deputy_head_id=?)",
            (my_gid, uid, uid)).fetchone()
    # 2) admin: ?group=
    if not g and session.get("role") in ("admin", "teacher", "curator"):
        gid_arg = request.args.get("group", type=int)
        if gid_arg:
            g = d.execute("SELECT * FROM groups WHERE id=?", (gid_arg,)).fetchone()
    # 3) fallback: только своя группа (староста чужой группы — ошибка данных, журнал не открываем)
    if not g:
        rows = d.execute(
            "SELECT * FROM groups WHERE (head_id=? OR deputy_head_id=?) ORDER BY id",
            (uid, uid)).fetchall()
        if my_gid:
            for r in rows:
                if r["id"] == my_gid:
                    g = r
                    break
        # если my_gid нет или не совпало — не открываем чужой журнал студенту
        if not g and session.get("role") == "student":
            g = None
        elif not g and len(rows) == 1 and session.get("role") in ("admin", "teacher", "curator"):
            g = rows[0]
    if not g and session.get("role") not in ("admin",):
        flash("Журнал посещаемости доступен только старосте/заму своей группы")
        return redirect(url_for("student"))
    if not g:
        flash("Укажите группу")
        return redirect(url_for("admin") if session.get("role") == "admin" else url_for("student"))
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
    head_id = g["head_id"] if "head_id" in g.keys() else None
    deputy_id = g["deputy_head_id"] if "deputy_head_id" in g.keys() else None
    # гарантировать, что староста/зам есть в списке (если студент этой группы)
    known_ids = {st["id"] for st in studs}
    for extra_id, label in ((head_id, "староста"), (deputy_id, "зам")):
        if extra_id and extra_id not in known_ids:
            extra = d.execute(
                "SELECT id, name FROM users WHERE id=? AND role='student' AND group_id=?",
                (extra_id, gid)).fetchone()
            if extra:
                studs = list(studs) + [extra]
                known_ids.add(extra["id"])
    # сортировка: староста, зам, остальные по имени
    def _sort_key(st):
        if st["id"] == head_id: return (0, st["name"])
        if st["id"] == deputy_id: return (1, st["name"])
        return (2, st["name"])
    studs = sorted(studs, key=_sort_key)
    rows = []
    for st in studs:
        cells = [dict(marks[(st["id"], l["id"])]) if (st["id"], l["id"]) in marks else None for l in lessons]
        st_stats = stats([c for c in cells if c])
        badge = ""
        if st["id"] == head_id: badge = "староста"
        elif st["id"] == deputy_id: badge = "зам"
        rows.append({"id": st["id"], "name": st["name"], "badge": badge, "cells": cells,
                     "present": st_stats.get("present") or 0,
                     "excused": st_stats.get("excused") or 0,
                     "unexcused": st_stats.get("unexcused") or 0,
                     "missed": st_stats.get("missed") or 0,
                     "missed_hours": st_stats.get("missed_hours") or 0})
    return render_template("head_attendance.html", group=g, lessons=lessons, day_groups=day_groups, rows=rows,
                           subjects=subjects, sid=sid, today=date.today().isoformat(),
                           date_from=date_from, date_to=date_to)

@bp.route("/attendance/group/<int:gid>/template")
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



@bp.route("/attendance/group/<int:gid>/import", methods=["POST"])
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
