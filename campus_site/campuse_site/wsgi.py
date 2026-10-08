# PythonAnywhere WSGI — укажите этот файл в Web → WSGI configuration file
# Путь к коду: /home/<username>/campuse_site/campuse_site/

import sys
import os

# >>> Замените login на ваш username на PythonAnywhere <<<
PROJECT = "/home/login/campuse_site/campuse_site"

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# SECRET_KEY обязателен для сессий. Задайте свой длинный случайный ключ.
# Можно также прописать его в Web → Environment variables на PythonAnywhere.
os.environ.setdefault("SECRET_KEY", "campus-change-me-to-a-long-random-string")

from app import app as application
from app import setup
setup()
