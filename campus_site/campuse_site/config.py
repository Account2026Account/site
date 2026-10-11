# -*- coding: utf-8 -*-
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(_ROOT, "campus.db")
UPLOADS = os.path.join(_ROOT, "uploads")
os.makedirs(UPLOADS, exist_ok=True)
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
               ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".rar"}
SECRET_KEY = os.environ.get("SECRET_KEY", "campus-dev-change-me-before-prod")

WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
WDF = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
WD_RU = {"понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3, "пятница": 4, "суббота": 5, "воскресенье": 6}
WD_SHORT = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5}
