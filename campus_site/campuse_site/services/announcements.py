# -*- coding: utf-8 -*-
"""Announcements query helper."""



def get_db():
    from core import db
    return db()

def announcements(where="", args=()):
    d = get_db()
    # fallback if migrate not yet applied
    try:
        d.execute("SELECT target_user_id FROM announcements LIMIT 0")
        has_tu = True
    except Exception:
        has_tu = False
    if has_tu:
        q = """SELECT a.*, u.name author, g.name gname, tu.name target_name
          FROM announcements a JOIN users u ON u.id=a.author_id
          LEFT JOIN groups g ON g.id=a.group_id
          LEFT JOIN users tu ON tu.id=a.target_user_id """ + where + " ORDER BY a.id DESC LIMIT 20"
    else:
        # strip target_user_id from where clause for old schema
        where2 = where.replace("a.target_user_id IS NULL AND ", "").replace(" OR a.target_user_id=?", "")
        q = """SELECT a.*, u.name author, g.name gname, NULL as target_name
          FROM announcements a JOIN users u ON u.id=a.author_id
          LEFT JOIN groups g ON g.id=a.group_id """ + where2 + " ORDER BY a.id DESC LIMIT 20"
        if where2 != where:
            # drop last arg if we removed target filter
            args = args[:-1] if args and len(args) >= 2 else args
    return d.execute(q, args).fetchall()

