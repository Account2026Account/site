# -*- coding: utf-8 -*-
"""Совместимость: реэкспорт DB-API из core.
Предпочтительный импорт: from core import db, setup, migrate, setting
"""
from core import db, init_db, close_db, setting, migrate, seed_marks, setup  # noqa: F401
