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

from services.announcements import announcements

bp = FlatBlueprint("teacher", __name__)

@bp.route("/teacher")
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
    for grp in d.execute("SELECT id, name FROM groups WHERE curator_id=? ORDER BY name", (uid,)):
        subs = d.execute("""SELECT DISTINCT s.id sid, s.name sname,
            (SELECT COUNT(*) FROM users WHERE group_id=? AND role='student') n,
            (SELECT COUNT(*) FROM lessons WHERE group_id=? AND subject_id=s.id) l
            FROM subjects s WHERE
              EXISTS(SELECT 1 FROM assignments a WHERE a.group_id=? AND a.subject_id=s.id)
              OR EXISTS(SELECT 1 FROM schedule sch WHERE sch.group_id=? AND sch.subject_id=s.id)
              OR EXISTS(SELECT 1 FROM lessons l WHERE l.group_id=? AND l.subject_id=s.id)
            ORDER BY s.name""", (grp["id"], grp["id"], grp["id"], grp["id"], grp["id"])).fetchall()
        curator_groups.append({"gid": grp["id"], "gname": grp["name"], "subjects": [dict(s) for s in subs]})

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


