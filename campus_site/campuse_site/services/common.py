# -*- coding: utf-8 -*-
"""Small shared helpers."""


def setting(c, key):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else None

def minutes(t):
    h, m = map(int, t.split(":"))
    return h * 60 + m

def gap_label(m):
    return f"перерыв {m} мин" if m <= 40 else f"окно {m // 60} ч {m % 60} мин" if m % 60 else f"окно {m // 60} ч"

