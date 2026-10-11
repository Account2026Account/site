# Campus

Журнал, посещаемость, расписание, дежурства, доп. баллы.

```bash
pip install -r requirements.txt
python app.py
```

Логин: `admin` / `admin123`

## Архитектура (готова)

| Слой | Папка | Содержание |
|------|--------|------------|
| HTTP | `routes/` | Blueprints (auth, journal, api, …) |
| Логика | `services/` | stats, schedule, bonus, notify, importers… |
| Ядро | `core.py` | Flask app, БД setup, re-export |
| Конфиг | `config.py`, `_schemas.py` | константы, SQL |

См. `STRUCTURE.md`. UI-фиксы — в `static/extra.css`.
