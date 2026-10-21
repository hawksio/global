"""Production WSGI entry point (cPanel "Setup Python App" / Passenger, gunicorn or waitress).

    gunicorn -w 2 -b 127.0.0.1:8000 wsgi:app        (VPS)
    passenger_wsgi.py imports `application` from here (cPanel)

Background work does NOT run here: schedule `flask --app gm worker --once` with cron (see DEPLOYMENT.md).
"""
from gm import create_app

app = application = create_app()
