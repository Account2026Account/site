# -*- coding: utf-8 -*-
"""Blueprints registration. Endpoints stay flat (url_for("login") as before)."""
from flask import Blueprint


class FlatBlueprint(Blueprint):
    """Blueprint that does not prefix endpoint names with blueprint name."""

    def add_url_rule(self, rule, endpoint=None, view_func=None, provide_automatic_options=None, **options):
        if endpoint is None and view_func is not None:
            endpoint = view_func.__name__
        # Register directly on app at blueprint-register time without name prefix
        self.deferred_functions.append(
            lambda s, r=rule, e=endpoint, v=view_func, p=provide_automatic_options, o=options: s.app.add_url_rule(
                s.url_prefix + r if s.url_prefix else r,
                endpoint=e,
                view_func=v,
                provide_automatic_options=p,
                **o,
            )
        )


def register_blueprints(app):
    from routes import auth, admin, teacher, student, schedule, journal, api, notifications
    for mod in (auth, admin, teacher, student, schedule, journal, api, notifications):
        app.register_blueprint(mod.bp)
