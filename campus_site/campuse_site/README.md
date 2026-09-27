# Campus site

## Запуск

```bash
cd campuse_site/campus
python app.py
```

При старте в консоли должна появиться строка:
```
Bonus routes: ['/api/bonus/set', '/api/bonus/apply', '/api/bonus/cancel', '/api/bonus/ping']
```

Если пишет `NONE` — запущен не тот app.py.

Проверка в браузере (после входа): откройте
`http://127.0.0.1:5000/api/bonus/ping`
Должен вернуться JSON `{"ok":true,"routes":[...]}`.

## Если 404 на /api/bonus/set

1. Удалите кэш: папку `__pycache__` внутри `campus/`
2. Убедитесь, что правите именно тот `app.py`, который запускаете
3. Полностью остановите сервер (Ctrl+C) и запустите снова
4. В консоли при старте проверьте строку `Bonus routes:`

Логины демо: teacher1 / teacher123, student1 / student123, admin / admin123
