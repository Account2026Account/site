# PythonAnywhere / production WSGI
import sys, os
PROJECT = "/home/login/campuse_site/campuse_site"
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.environ.setdefault("SECRET_KEY", "campus-change-me-to-a-long-random-string")

from app import app as application
from core import setup
setup()
