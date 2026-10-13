"""
Campus — точка входа.
Создание app, регистрация blueprints, config/setup.
"""
from core import app, setup
from routes import register_blueprints

register_blueprints(app)


if __name__ == "__main__":
    setup()
    rules = list(app.url_map.iter_rules())
    print("Total routes:", len(rules))
    bonus = [r.rule for r in rules if "bonus" in r.rule]
    print("Bonus routes:", bonus if bonus else "NONE")
    app.run(debug=True, use_reloader=True)
