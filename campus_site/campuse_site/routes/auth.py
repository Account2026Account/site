# -*- coding: utf-8 -*-
from flask import render_template, request, redirect, url_for, session, jsonify, flash, abort, Response, send_from_directory
from datetime import date, datetime, timedelta
from routes import FlatBlueprint
from core import db, role, stats, has_journal_access, grant_journal_access, notify, notify_many, setting, minutes, gap_label, overlap, occurrences, sync_lessons, feed, Bad, WD, WDF, UPLOADS, ALLOWED_EXT, _ROOT, DB, migrate, seed_marks, setup, can_fill_attendance
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import io, re, os, sqlite3, shutil, tempfile, random

bp = FlatBlueprint("auth", __name__)
from core import announcements

@bp.route("/")
def index():
    r = session.get("role")
    return redirect(url_for(r) if r else url_for("login"))

@bp.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE login=?", (request.form["login"].strip(),)).fetchone()
        if u and check_password_hash(u["pw"], request.form["password"]):
            session.clear(); session.update(uid=u["id"], role=u["role"], name=u["name"])
            return redirect(url_for("index"))
        err = "Неверный логин или пароль. Проверьте данные и попробуйте снова."
    return render_template("login.html", err=err)

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@bp.post("/announce")
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

@bp.post("/announce/delete")
def announce_del():
    if session.get("role") not in ("teacher", "admin"): abort(403)
    d = db()
    d.execute("DELETE FROM announcements WHERE id=? AND (author_id=? OR ?='admin')", (request.form["id"], session["uid"], session["role"]))
    d.commit()
    return redirect(url_for("index"))

@bp.route("/profile", methods=["GET", "POST"])
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

