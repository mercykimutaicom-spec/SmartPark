"""WSGI entrypoint for production servers (gunicorn, Render, Docker).

Imports the Flask ``app`` from app.py. Importing app.py already runs
init_db() + heap warm-up, so no extra boot logic is needed here.
"""
from app import app  # noqa: F401

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
