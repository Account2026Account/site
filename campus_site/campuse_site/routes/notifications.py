# -*- coding: utf-8 -*-
from flask import render_template, request, redirect, url_for, session, jsonify, flash, abort, Response, send_from_directory
from datetime import date, datetime, timedelta
from routes import FlatBlueprint
from core import db, role, stats, has_journal_access, grant_journal_access, notify, notify_many, setting, minutes, gap_label, overlap, occurrences, sync_lessons, feed, Bad, WD, WDF, UPLOADS, ALLOWED_EXT, _ROOT, DB, migrate, seed_marks, setup, can_fill_attendance
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import io, re, os, sqlite3, shutil, tempfile, random

bp = FlatBlueprint("notifications", __name__)

# ---------- Уведомления ----------
@bp.route("/notifications")
def notifications_page():
    if "uid" not in session:
        return redirect(url_for("login"))
    d = db()
    rows = d.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50",
        (session["uid"],)).fetchall()
    return render_template("notifications.html", items=rows)

@bp.post("/api/notifications/read")
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

@bp.post("/api/notifications/clear")
def notifications_clear():
    """Стереть всю историю уведомлений текущего пользователя."""
    if "uid" not in session:
        return jsonify(ok=False), 403
    d = db()
    d.execute("DELETE FROM notifications WHERE user_id=?", (session["uid"],))
    d.commit()
    return jsonify(ok=True)

