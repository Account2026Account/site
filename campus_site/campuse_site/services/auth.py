# -*- coding: utf-8 -*-
"""Auth helpers."""
from functools import wraps
from flask import session, redirect, url_for


def role(*rs):
    def deco(f):
        @wraps(f)
        def wrap(*a, **k):
            if session.get("role") not in rs:
                return redirect(url_for("login"))
            return f(*a, **k)
        return wrap
    return deco

