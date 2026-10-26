"""
Savithiri Clinic - Django settings.
All environment-specific values come from the .env file (see .env.example).
"""
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")


def env_str(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(
            f"Environment variable {name} is not set. Create the .env file "
            "(run scripts\\windows\\init_env.ps1)."
        )
    return value


def env_bool(name, default=False):
    return str(os.environ.get(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be a whole number.")


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# --- Core ---------------------------------------------------------------
SECRET_KEY = env_str("DJANGO_SECRET_KEY", required=True)
DEBUG = env_bool("DJANGO_DEBUG", False)

SERVER_HOST = env_str("SERVER_HOST", "0.0.0.0")
SERVER_PORT = env_int("SERVER_PORT", 8000)
SERVER_THREADS = env_int("SERVER_THREADS", 8)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
if "*" in ALLOWED_HOSTS and not DEBUG:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS may not contain '*' unless DJANGO_DEBUG=True.")
# Browsers on the LAN post forms from http://<ip>:<port>
CSRF_TRUSTED_ORIGINS = [f"http://{h}:{SERVER_PORT}" for h in ALLOWED_HOSTS if h != "*"]

ADMIN_URL = env_str("ADMIN_URL", "admin").strip("/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts",
    "apps.core",
    "apps.patients",
    "apps.queueing",
    "apps.pharmacy",
    "apps.clinical",
    "apps.sales",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.SessionActivityMiddleware",
    "apps.accounts.middleware.MustChangePasswordMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.clinic",
            ],
        },
    },
]

# --- Database -----------------------------------------------------------
DB_ENGINE = env_str("DB_ENGINE", "postgres").lower()
if DB_ENGINE == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env_str("DB_NAME", "clinic_db"),
            "USER": env_str("DB_USER", "clinic_app"),
            "PASSWORD": env_str("DB_PASSWORD", required=True),
            "HOST": env_str("DB_HOST", "127.0.0.1"),
            "PORT": env_str("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "ATOMIC_REQUESTS": False,  # critical operations use explicit transaction.atomic()
        }
    }
elif DB_ENGINE == "sqlite":
    # ONLY for quick isolated tests. Never use for the live clinic.
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "test_only.sqlite3"}}
else:
    raise ImproperlyConfigured("DB_ENGINE must be 'postgres' or 'sqlite'.")

# --- Auth ---------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.LockoutBackend"]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "accounts:login"
LOGIN_MAX_FAILED_ATTEMPTS = env_int("LOGIN_MAX_FAILED_ATTEMPTS", 5)
LOGIN_LOCK_MINUTES = env_int("LOGIN_LOCK_MINUTES", 15)
# Keep flash messages out of cookies (session storage only).
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
if sys.argv[1:2] == ["test"]:
    # Fast hashing for the automated tests ONLY. Real passwords use Django's strong default.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Sessions / cookies -------------------------------------------------
# Idle timeout slides forward on real user actions only. Background live-update polls
# (header X-Live-Poll) do NOT extend it, so an unattended screen still times out.
# See apps.accounts.middleware.SessionActivityMiddleware.
SESSION_COOKIE_AGE = env_int("SESSION_TIMEOUT_MINUTES", 15) * 60
SESSION_SAVE_EVERY_REQUEST = False
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"

# --- Localisation (India) -------------------------------------------------
LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
FORMAT_MODULE_PATH = ["config.formats"]
CURRENCY_CODE = "INR"
CURRENCY_SYMBOL = "\u20b9"

# --- Static files (served locally by WhiteNoise; no CDN) -------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Logging (never log patient data) ---------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"standard": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "clinic.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 10,
            "formatter": "standard",
            "encoding": "utf-8",
        },
    },
    "root": {"handlers": ["console", "file"], "level": "INFO"},
}
