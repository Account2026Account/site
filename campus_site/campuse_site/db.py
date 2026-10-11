# -*- coding: utf-8 -*-
"""Database connection, schema, migrations."""
import os, random, sqlite3
from datetime import date, datetime, timedelta
from flask import g, session
from werkzeug.security import generate_password_hash

from config import DB
from _schemas import SCHEMA, NEW_SCHEMA

def init_db():
    if os.path.exists(DB):
        return False
    c = sqlite3.connect(DB); c.execute("PRAGMA foreign_keys=ON"); c.executescript(SCHEMA)
    def add(login, pw, name, role, gid=None):
        return c.execute("INSERT INTO users(login,pw,name,role,group_id) VALUES(?,?,?,?,?)",
                         (login, generate_password_hash(pw), name, role, gid)).lastrowid
    a = add("admin", "admin123", "Администратор", "admin")
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

def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def setting(c, key):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else None
