# -*- coding: utf-8 -*-
"""Bonus points schema ensure."""


def _ensure_bonus_tables(d):
    d.execute("""CREATE TABLE IF NOT EXISTS bonus_points(
      student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
      balance INTEGER NOT NULL DEFAULT 0,
      conditions TEXT NOT NULL DEFAULT '',
      updated TEXT,
      PRIMARY KEY(student_id, subject_id))""")
    d.execute("""CREATE TABLE IF NOT EXISTS bonus_applications(
      id INTEGER PRIMARY KEY,
      student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
      lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
      points_used INTEGER NOT NULL,
      original_grade INTEGER NOT NULL,
      new_grade INTEGER NOT NULL,
      applied_at TEXT NOT NULL,
      cancelled_at TEXT,
      cancelled_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
      UNIQUE(student_id, lesson_id))""")
    d.execute("""CREATE TABLE IF NOT EXISTS bonus_rules(
      group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
      subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
      conditions TEXT NOT NULL DEFAULT '',
      updated TEXT,
      PRIMARY KEY(group_id, subject_id))""")
    d.execute("""CREATE TABLE IF NOT EXISTS bonus_log(
      id INTEGER PRIMARY KEY,
      student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
      teacher_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
      delta INTEGER NOT NULL,
      reason TEXT NOT NULL DEFAULT '',
      created TEXT NOT NULL)""")
    # миграция: если conditions были на студенте — перенесём в rules один раз
    try:
        cols = [r[1] for r in d.execute("PRAGMA table_info(bonus_points)")]
        if "conditions" in cols:
            for row in d.execute("SELECT subject_id, conditions FROM bonus_points WHERE conditions IS NOT NULL AND conditions != ''").fetchall():
                # group_id неизвестен здесь — пропускаем автоперенос
                pass
    except Exception:
        pass

