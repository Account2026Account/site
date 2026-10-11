# Структура Campus — завершена

```
campuse_site/
├── app.py                 # register_blueprints + run
├── core.py                # Flask app, init_db/migrate/setup, inject; re-export services
├── config.py              # константы
├── _schemas.py            # SQL SCHEMA
├── db.py                  # реэкспорт db API
├── wsgi.py
├── services/              # бизнес-логика (без HTTP)
│   ├── auth.py            # role()
│   ├── common.py          # setting, minutes, gap_label
│   ├── journal.py         # stats, access, course, load_journal, finals helpers
│   ├── schedule.py        # occurrences, sync, feed, scope, bell_rows
│   ├── bonus.py           # _ensure_bonus_tables
│   ├── attendance.py      # can_fill_attendance, head_group, gen_duty
│   ├── notify.py          # notify, notify_many
│   ├── announcements.py   # announcements()
│   └── importers.py       # DOCX/PDF
├── routes/                # только HTTP (FlatBlueprint)
│   ├── __init__.py
│   ├── auth.py
│   ├── admin.py
│   ├── teacher.py
│   ├── student.py
│   ├── schedule.py
│   ├── journal.py
│   ├── api.py
│   └── notifications.py
├── templates/
└── static/
```

Логика в `services/`, маршруты в `routes/`, endpoint плоские → `url_for` без изменений.
