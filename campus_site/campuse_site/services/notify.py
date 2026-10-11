# -*- coding: utf-8 -*-
"""Notifications."""
from datetime import datetime



def get_db():
    from core import db
    return db()

def notify(user_id, title, body="", link=""):
    """Создаёт внутрисайтовое уведомление пользователю."""
    if not user_id:
        return
    d = get_db()
    d.execute(
        "INSERT INTO notifications(user_id, title, body, link, created, read) VALUES(?,?,?,?,?,0)",
        (user_id, title[:200], (body or "")[:500], (link or "")[:300],
         datetime.now().strftime("%d.%m.%Y %H:%M")))
    d.commit()

def notify_many(user_ids, title, body="", link=""):
    for uid in set(user_ids or []):
        notify(uid, title, body, link)

