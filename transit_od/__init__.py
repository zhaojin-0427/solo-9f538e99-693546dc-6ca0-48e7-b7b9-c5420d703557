"""Flask 应用工厂。"""
import os

from flask import Flask, g

from .db import connect, ensure_parent_dir, init_db

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")


def create_app(db_path=None):
    db_path = db_path or os.environ.get("APP_DB") or DEFAULT_DB
    db_path = os.path.abspath(db_path)
    ensure_parent_dir(db_path)

    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 上传上限 16MB
    app.secret_key = os.environ.get("SECRET_KEY", "transit-od-dev-secret")

    conn = connect(db_path)
    init_db(conn)
    conn.close()

    from .routes import bp
    app.register_blueprint(bp)

    @app.teardown_appcontext
    def _close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            if db.in_transaction:
                db.rollback()  # 结束页面读事务，释放快照，避免长期占住 WAL
            db.close()

    return app
