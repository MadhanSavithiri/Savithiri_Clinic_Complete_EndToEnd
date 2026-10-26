"""Production-style server for Windows (Waitress). Usage: python serve.py"""
from config.wsgi import application  # sets up Django first

from django.conf import settings
from waitress import serve

if __name__ == "__main__":
    print(f"Savithiri Clinic server listening on http://{settings.SERVER_HOST}:{settings.SERVER_PORT}")
    print("Press Ctrl+C to stop.")
    serve(
        application,
        host=settings.SERVER_HOST,
        port=settings.SERVER_PORT,
        threads=settings.SERVER_THREADS,
        url_scheme="http",
    )
