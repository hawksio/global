"""Local development server (never use it in production; see DEPLOYMENT.md).

    .venv\Scripts\python run.py              http://localhost:4322
    .venv\Scripts\python run.py --port 5000
"""
import argparse
import os

from gm import create_app
from gm.jobs import start_inline_worker

app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 4322)))
    port = parser.parse_args().port
    if app.config.get("START_INLINE_WORKER"):
        start_inline_worker(app)
    app.run(host="127.0.0.1", port=port, debug=False)
