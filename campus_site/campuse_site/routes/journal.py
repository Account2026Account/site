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

bp = FlatBlueprint("journal", __name__)
from core import course, load_journal

@bp.route("/journal/<int:gid>/<int:sid>")
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

@bp.route("/journal/<int:gid>/<int:sid>/export")
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
    ws.cell(leg, 1, "Легенда: • — был; н — не был; у — уважительная (у: причина). Часы = «н» × 95/60.")
    ws.cell(leg, 1).font = Font(color="66708C", size=9)
    ws.column_dimensions["A"].width = 28
    for i in range(2, 8): ws.column_dimensions[L(i)].width = 11
    for i in range(8, 8 + len(lessons)): ws.column_dimensions[L(i)].width = 9
    ws.row_dimensions[4].height = 28; ws.row_dimensions[5].height = 36
    ws.freeze_panes = "H6"; ws.page_setup.orientation = "landscape"

    # --- Sheet «Итоги» ---
    d = db()
    kinds = get_final_kinds(d, gid, sid)
    studs_f = d.execute("SELECT id, name FROM users WHERE role='student' AND group_id=? ORDER BY name", (gid,)).fetchall()
    kind_keys = list(kinds.keys()) if kinds else ["dif_zach", "exam", "course"]
    wsF = wb.create_sheet("Итоги")
    wsF["A1"] = f'Итоговые: {c["gname"]}, {c["sname"]}'
    wsF["A1"].font = Font(size=14, bold=True, color="14213D")
    wsF["A2"] = f"Выгружено {datetime.now():%d.%m.%Y %H:%M}"
    wsF["A2"].font = Font(color="66708C")
    fh = ["Студент"] + [kinds.get(k, k) for k in kind_keys] + ["Заметка"]
    for i, h in enumerate(fh, 1):
        x = wsF.cell(4, i, h)
        x.font = Font(bold=True, color="FFFFFF"); x.fill = fill("14213D")
        x.alignment = center; x.border = box
    for ri, st in enumerate(studs_f, 5):
        wsF.cell(ri, 1, st["name"]).border = box
        frows = {r["kind"]: r for r in d.execute(
            "SELECT kind, grade, note FROM final_grades WHERE student_id=? AND subject_id=?",
            (st["id"], sid)).fetchall()}
        notes = []
        for ci, k in enumerate(kind_keys, 2):
            fr = frows.get(k)
            val = fr["grade"] if fr and fr["grade"] else ""
            x = wsF.cell(ri, ci, val); x.border = box; x.alignment = center
            if fr:
                try:
                    n = fr["note"] if "note" in fr.keys() else ""
                except Exception:
                    n = ""
                if n:
                    notes.append(f'{kinds.get(k,k)}: {n}')
        x = wsF.cell(ri, 2 + len(kind_keys), "; ".join(notes))
        x.border = box
    wsF.column_dimensions["A"].width = 28
    for i in range(2, 2 + len(kind_keys) + 1):
        wsF.column_dimensions[L(i)].width = 14
    wsF.freeze_panes = "B5"

    buf = io.BytesIO(); wb.save(buf)
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=journal_{gid}_{sid}.xlsx"})

@bp.route("/journal/<int:gid>/<int:sid>/finals")
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

@bp.route("/journal/<int:gid>/<int:sid>/finals/export")
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



# ========== Замечания / импорт журнала ==========
# ========== Замечания в журнале ==========
@bp.post("/journal/remark")
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

@bp.get("/journal/<int:gid>/remarks/<int:student_id>")
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

@bp.route("/journal/<int:gid>/<int:sid>/import", methods=["GET", "POST"])
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
        wi["A1"] = "Как заполнять"
        wi["A1"].font = Font(bold=True, size=14, color="14213D")
        for i, t in enumerate([
            "",
            "Лист «Журнал» — сводка + даты занятий в одном месте.",
            "В ячейке даты:  2–5 = оценка  |  пусто/• = был  |  н = не был  |  у или у:текст = уважительная",
            "Столбцы «Ср. балл / Пропуски / …» при загрузке не обязательны (сайт пересчитает сам).",
            "Лист «Итоги» — итоговые аттестации (отдельно от журнала).",
            "1 пара = 95 мин. Не переименовывайте скрытые строки date/lid под заголовками дат.",
        ], 1):
            wi.cell(i, 1, t)
        wi["A10"] = "• / пусто"; wi["A10"].fill = PatternFill("solid", fgColor="B8F0D3")
        wi["B10"] = "н"; wi["B10"].fill = PatternFill("solid", fgColor="FFC9C9")
        wi["C10"] = "у"; wi["C10"].fill = PatternFill("solid", fgColor="FFF3CD")
        wi["D10"] = "5"; wi["D10"].fill = PatternFill("solid", fgColor="B8F0D3")
        wi.column_dimensions["A"].width = 78

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
        # bonus balances
        bals = {r["student_id"]: r["balance"] for r in d.execute(
            "SELECT student_id, balance FROM bonus_points WHERE subject_id=?", (sid,))}

        # ===== Sheet «Журнал»: summary + matrix (unified) =====
        ws = wb.create_sheet("Журнал", 0)
        # Row 1: headers
        sum_heads = ["Студент", "Ср. балл", "Пропуски", "Часы", "Уваж.", "Посещ. %", "Доп. баллы"]
        for i, h in enumerate(sum_heads, 1):
            c = ws.cell(1, i, h)
            c.font = head_font; c.fill = head_fill; c.alignment = center; c.border = thin
        for i, l in enumerate(lessons, 8):
            c = ws.cell(1, i, f'{l["day"][8:10]}.{l["day"][5:7]}')
            c.font = head_font; c.fill = head_fill; c.alignment = center; c.border = thin
        # Row 2: ISO dates (hidden) | Row 3: lesson ids (hidden)
        for i, l in enumerate(lessons, 8):
            ws.cell(2, i, l["day"]).font = Font(size=1, color="FFFFFF")
            ws.cell(3, i, l["id"]).font = Font(size=1, color="FFFFFF")
        ws.cell(2, 1, "date"); ws.cell(3, 1, "lid")
        # Row 4: topics under dates
        ws.cell(4, 1, "Тема")
        ws.cell(4, 1).font = Font(italic=True, size=9, color="66708C")
        for i, l in enumerate(lessons, 8):
            x = ws.cell(4, i, l["topic"] or "")
            x.font = Font(italic=True, size=8, color="66708C"); x.alignment = center; x.border = thin
        for i in range(1, 8):
            ws.cell(4, i).border = thin
        ws.row_dimensions[2].hidden = True
        ws.row_dimensions[3].hidden = True

        gfill = {2: "FFC9C9", 3: "FFD84D", 4: "CFE0FF", 5: "B8F0D3"}
        green = PatternFill("solid", fgColor="E9F8F1")
        for ri, st in enumerate(studs, 5):
            # compute quick stats for template preview
            cells_m = []
            for l in lessons:
                m = marks.get((st["id"], l["id"]))
                cells_m.append(m)
            gr = [m["grade"] for m in cells_m if m and m["grade"]]
            avg = round(sum(gr) / len(gr), 2) if gr else None
            unex = sum(1 for m in cells_m if m and m["present"] == 0)
            exc = sum(1 for m in cells_m if m and m["present"] == 2)
            rec = [m for m in cells_m if m and m["present"] is not None]
            was = sum(1 for m in rec if m["present"] == 1)
            pct = round(100 * was / len(rec)) if rec else None
            hours = round(unex * 95 / 60, 1)
            bal = bals.get(st["id"], 0) or 0
            vals = [st["name"], avg, unex, hours, exc, pct, bal]
            for i, v in enumerate(vals, 1):
                x = ws.cell(ri, i, v if v is not None else "")
                x.border = thin
                x.alignment = Alignment(horizontal="left" if i == 1 else "center", vertical="center")
            for ci, l in enumerate(lessons, 8):
                m = marks.get((st["id"], l["id"]))
                val, fl = "", green
                if m:
                    if m["grade"] in (2, 3, 4, 5):
                        val = m["grade"]; fl = PatternFill("solid", fgColor=gfill[m["grade"]])
                    elif m["present"] == 0:
                        val = "н"; fl = PatternFill("solid", fgColor="FFE4E4")
                    elif m["present"] == 2:
                        reason = ""
                        try: reason = (m["excuse"] or "") if "excuse" in m.keys() else ""
                        except Exception: reason = ""
                        val = ("у: " + reason) if reason else "у"
                        fl = PatternFill("solid", fgColor="FFF3CD")
                    elif m["present"] == 1:
                        val = "•"
                x = ws.cell(ri, ci, val)
                x.fill = fl; x.alignment = center; x.border = thin; x.font = Font(bold=True)

        if lessons and studs:
            last_row = 4 + len(studs)
            last_col = 7 + len(lessons)
            rng = f"H5:{L(last_col)}{last_row}"
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(H5="н",H5="Н",H5="0")'], fill=PatternFill("solid", fgColor="FFC9C9")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(H5="у",H5="У",LEFT(H5,2)="у:",LEFT(H5,2)="У:")'], fill=PatternFill("solid", fgColor="FFF3CD")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(H5=5,H5="5")'], fill=PatternFill("solid", fgColor="B8F0D3")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(H5=4,H5="4")'], fill=PatternFill("solid", fgColor="CFE0FF")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(H5=3,H5="3")'], fill=PatternFill("solid", fgColor="FFD84D")))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=['OR(H5=2,H5="2")'], fill=PatternFill("solid", fgColor="FFC9C9")))
        ws.column_dimensions["A"].width = 28
        for i in range(2, 8):
            ws.column_dimensions[L(i)].width = 10
        for i in range(8, 8 + len(lessons)):
            ws.column_dimensions[L(i)].width = 8
        ws.freeze_panes = "H5"
        ws.sheet_view.showGridLines = False
        ws.row_dimensions[1].height = 26
        ws.row_dimensions[4].height = 32

        # ===== Sheet «Итоги» (separate) =====
        kinds = get_final_kinds(d, gid, sid)
        kind_keys = list(kinds.keys()) if kinds else ["dif_zach", "exam", "course"]
        wf = wb.create_sheet("Итоги")
        wf.append(["Студент"] + [kinds.get(k, k) for k in kind_keys] + ["Тип (ключ)", "Оценка", "Заметка", "Дата_с", "Дата_по"])
        # Simpler flat format for import compatibility + readable columns
        # Actually use both: wide view for reading, but import uses flat rows below examples
        # Clear and use practical flat format that is easy to fill:
        wf.delete_rows(1, 1)
        wf.append(["Студент", "Тип", "Оценка", "Заметка", "Дата_с", "Дата_по"])
        for c in range(1, 7):
            wf.cell(1, c).font = Font(bold=True, color="FFFFFF")
            wf.cell(1, c).fill = head_fill
            wf.cell(1, c).alignment = center
        # prefill students empty rows for first kind as examples
        for st in studs[:3]:
            wf.append([st["name"], "dif_zach", "", "", "", ""])
        wf.append(["", "exam", "", "", "", ""])
        wf.append(["", "course", "", "", "", ""])
        note_row = 3 + min(3, len(studs)) + 3
        wf.cell(note_row, 1, "Тип: dif_zach (диф. зачёт), exam (экзамен), course (курсовая) или свой ключ из настроек итогов.")
        wf.cell(note_row, 1).font = Font(color="66708C", size=9)
        for col, w in zip("ABCDEF", (28, 14, 12, 28, 12, 12)):
            wf.column_dimensions[col].width = w

        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        return Response(buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=journal_template_{gid}_{sid}.xlsx"})
    f = request.files.get("file")
    if not f:
        flash("Выберите файл Excel"); return redirect(url_for("journal", gid=gid, sid=sid))
    from openpyxl import load_workbook
    # ensure optional columns exist (old DBs)
    try:
        d.execute("SELECT excuse FROM marks LIMIT 1")
    except Exception:
        try:
            d.execute("ALTER TABLE marks ADD COLUMN excuse TEXT DEFAULT ''")
            d.commit()
        except Exception:
            pass
    try:
        d.execute("SELECT 1 FROM final_grades LIMIT 1")
    except Exception:
        pass
    wb = load_workbook(f, data_only=True)
    name_map = {r["name"].strip().lower(): r["id"] for r in d.execute(
        "SELECT id, name FROM users WHERE role='student' AND group_id=?", (gid,))}
    ok = err = 0

    def _parse_day(day_raw):
        if day_raw is None:
            return None
        if hasattr(day_raw, "strftime"):
            return day_raw.strftime("%Y-%m-%d")
        day = str(day_raw).strip()[:16]
        if "." in day:
            parts = day.replace(" ", "").split(".")
            if len(parts) == 3:
                return f"{parts[2][:4]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
        if len(day) >= 10 and day[4] == "-":
            return day[:10]
        return None

    def _apply_cell(st_id, lid, raw):
        nonlocal ok
        if raw is None:
            return
        s = str(raw).strip()
        if s == "":
            return
        d.execute("INSERT OR IGNORE INTO marks(student_id,lesson_id) VALUES(?,?)", (st_id, lid))
        low = s.lower()
        def _upd(sql, args):
            try:
                d.execute(sql, args)
            except Exception:
                # fallback without excuse column
                sql2 = sql.replace(", excuse=''", "").replace(", excuse=?", "").replace("excuse=?, ", "")
                args2 = args
                if "excuse" in sql and "excuse=?" in sql:
                    # present=2 case: (reason, st_id, lid) -> (st_id, lid) after removing excuse
                    args2 = (args[1], args[2]) if len(args) == 3 else args
                    sql2 = "UPDATE marks SET present=2, grade=NULL WHERE student_id=? AND lesson_id=?"
                try:
                    d.execute(sql2, args2)
                except Exception:
                    pass
        if low in ("н", "0", "нб", "н/б"):
            _upd("UPDATE marks SET present=0, grade=NULL, excuse='' WHERE student_id=? AND lesson_id=?",
                 (st_id, lid))
        elif low in ("•", ".", "1", "б", "был", "+"):
            _upd("UPDATE marks SET present=1, grade=NULL, excuse='' WHERE student_id=? AND lesson_id=?",
                 (st_id, lid))
        elif low.startswith("у"):
            reason = ""
            if ":" in s:
                reason = s.split(":", 1)[1].strip()[:300]
            _upd("UPDATE marks SET present=2, grade=NULL, excuse=? WHERE student_id=? AND lesson_id=?",
                 (reason, st_id, lid))
        else:
            try:
                gv = int(float(s.replace(",", ".")))
                if gv in (2, 3, 4, 5):
                    _upd("UPDATE marks SET grade=?, present=1, excuse='' WHERE student_id=? AND lesson_id=?",
                         (gv, st_id, lid))
                elif gv in (0, 1):
                    _upd("UPDATE marks SET present=?, grade=NULL, excuse='' WHERE student_id=? AND lesson_id=?",
                         (gv, st_id, lid))
            except Exception:
                return
        ok += 1


    def _ensure_lesson(day):
        lid_row = d.execute("SELECT id FROM lessons WHERE group_id=? AND subject_id=? AND day=?",
                            (gid, sid, day)).fetchone()
        if not lid_row:
            d.execute("INSERT OR IGNORE INTO lessons(group_id,subject_id,day,topic) VALUES(?,?,?,?)",
                      (gid, sid, day, ""))
            lid_row = d.execute("SELECT id FROM lessons WHERE group_id=? AND subject_id=? AND day=?",
                                (gid, sid, day)).fetchone()
        return lid_row["id"] if lid_row else None

    # Prefer sheet «Журнал», else active
    ws = None
    for name in wb.sheetnames:
        if name.strip().lower() in ("журнал", "journal"):
            ws = wb[name]; break
    if ws is None:
        ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        flash("Пустой файл"); return redirect(url_for("journal", gid=gid, sid=sid))

    # Detect matrix: header has "студент" and at least one date-like column
    header = [str(c or "").strip() for c in rows[0]]
    header_l = [h.lower() for h in header]
    is_matrix = False
    name_col = None
    for i, h in enumerate(header_l):
        if h in ("студент", "фио", "имя"):
            name_col = i; break
    # look for hidden date row (row 2) or date headers
    date_cols = []  # (col_index, day_iso, lid_or_None)
    if name_col is not None and len(rows) >= 2:
        # Try row index 1 as ISO dates (template has hidden row 2)
        row_dates = rows[1] if len(rows) > 1 else None
        row_lids = rows[2] if len(rows) > 2 else None
        for ci in range(name_col + 1, len(header)):
            day = None
            lid = None
            if row_dates and ci < len(row_dates) and row_dates[ci]:
                day = _parse_day(row_dates[ci])
            if not day:
                day = _parse_day(header[ci])
            if row_lids and ci < len(row_lids) and row_lids[ci] is not None:
                try:
                    lid = int(row_lids[ci])
                except Exception:
                    lid = None
            if day:
                date_cols.append((ci, day, lid))
        if date_cols:
            is_matrix = True

    if is_matrix:
        # data starts after service rows: if row2 looks like dates, skip rows 1-3 (0-index 1..3), else from row 1
        start_ri = 1
        if len(rows) > 2 and rows[1] and any(_parse_day(x) for x in rows[1][1:8] if x):
            start_ri = 4 if len(rows) > 3 else 2
            # if row 4 is "Тема" skip it
            if start_ri < len(rows) and rows[start_ri] and str(rows[start_ri][0] or "").strip().lower() in ("тема", "тема занятия"):
                start_ri += 1
        for row in rows[start_ri:]:
            if not row or not row[name_col]:
                continue
            name = str(row[name_col]).strip().lower()
            if name in ("присутствовало", "легенда", "тема", ""):
                continue
            st_id = name_map.get(name)
            if not st_id:
                err += 1; continue
            for ci, day, lid in date_cols:
                if ci >= len(row):
                    continue
                raw = row[ci]
                if raw is None or str(raw).strip() == "":
                    continue
                if not lid:
                    lid = _ensure_lesson(day)
                if not lid:
                    continue
                _apply_cell(st_id, lid, raw)
            # optional bonus from "Доп. баллы" column if present
            for hi, h in enumerate(header_l):
                if h in ("доп. баллы", "доп.баллы", "доп баллы", "бонус", "bonus") and hi < len(row) and row[hi] is not None:
                    try:
                        # absolute balance set? treat as delta only if looks like signed change; else skip on matrix (read-only summary)
                        pass
                    except Exception:
                        pass
    else:
        # Flat list fallback: Студент | Дата | Оценка | Посещаемость | Причина | Доп.баллы
        def col(*names):
            for n in names:
                if n in header_l: return header_l.index(n)
            return None
        ci_name = col("студент", "фио", "имя")
        ci_day = col("дата", "день")
        ci_grade = col("оценка", "балл")
        ci_pres = col("посещаемость", "явка")
        ci_exc = col("причина", "excuse")
        ci_bonus = col("доп.баллы", "доп баллы", "бонус", "bonus")
        if ci_name is None or ci_day is None:
            flash("Нужен лист «Журнал» (матрица) или колонки Студент и Дата"); return redirect(url_for("journal", gid=gid, sid=sid))
        for row in rows[1:]:
            if not row or not row[ci_name]: continue
            name = str(row[ci_name]).strip().lower()
            st_id = name_map.get(name)
            if not st_id:
                err += 1; continue
            day = _parse_day(row[ci_day])
            if not day:
                err += 1; continue
            lid = _ensure_lesson(day)
            if not lid:
                continue
            # combine grade/pres/excuse into one apply
            if ci_grade is not None and row[ci_grade] is not None and str(row[ci_grade]).strip() != "":
                _apply_cell(st_id, lid, row[ci_grade])
            elif ci_pres is not None and row[ci_pres] is not None and str(row[ci_pres]).strip() != "":
                _apply_cell(st_id, lid, row[ci_pres])
            if ci_exc is not None and row[ci_exc]:
                reason = str(row[ci_exc]).strip()[:300]
                d.execute("INSERT OR IGNORE INTO marks(student_id,lesson_id) VALUES(?,?)", (st_id, lid))
                d.execute("UPDATE marks SET excuse=?, present=2, grade=NULL WHERE student_id=? AND lesson_id=?",
                          (reason, st_id, lid))
                ok += 1
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

    # Optional: import Итоги sheet
    for sname in wb.sheetnames:
        if sname.strip().lower() in ("итоги", "finals", "итоговые"):
            wsF = wb[sname]
            frows = list(wsF.iter_rows(values_only=True))
            if not frows:
                break
            fh = [str(c or "").strip().lower() for c in frows[0]]
            def fcol(*names):
                for n in names:
                    if n in fh: return fh.index(n)
                return None
            fn, ft, fg, fnote = fcol("студент", "фио"), fcol("тип", "kind"), fcol("оценка", "балл"), fcol("заметка", "note")
            if fn is not None and ft is not None:
                for row in frows[1:]:
                    if not row or not row[fn] or not row[ft]:
                        continue
                    st_id = name_map.get(str(row[fn]).strip().lower())
                    if not st_id:
                        continue
                    kind = str(row[ft]).strip()
                    grade = str(row[fg]).strip() if fg is not None and row[fg] is not None else ""
                    note = str(row[fnote]).strip()[:300] if fnote is not None and row[fnote] else ""
                    if not grade and not note:
                        continue
                    try:
                        d.execute("""INSERT INTO final_grades(student_id,group_id,subject_id,kind,grade,note,set_by,updated)
                                     VALUES(?,?,?,?,?,?,?,?)
                                     ON CONFLICT(student_id,subject_id,kind) DO UPDATE SET
                                       grade=excluded.grade, note=excluded.note, set_by=excluded.set_by, updated=excluded.updated""",
                                  (st_id, gid, sid, kind, grade or None, note, session["uid"],
                                   datetime.now().strftime("%d.%m.%Y %H:%M")))
                        ok += 1
                    except Exception:
                        err += 1
            break

    d.commit()
    flash(f"Импорт: обновлено {ok}, ошибок сопоставления {err}")
    return redirect(url_for("journal", gid=gid, sid=sid))

