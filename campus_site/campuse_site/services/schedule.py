# -*- coding: utf-8 -*-
"""Schedule: occurrences, sync, feed, scope."""
from datetime import date, datetime, timedelta
import re

from flask import session, request

from services.common import setting, minutes, gap_label


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

def feed(c, gid=None, tid=None):
    t = date.today()
    week = occurrences(c, t, t + timedelta(days=7), gid=gid, tid=tid)
    return dict(today_items=[o for o in week if o["day"] == t.isoformat()], changes_items=[o for o in week if o["status"] != "ok"])

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

