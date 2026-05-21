from pathlib import Path
import os
from urllib.parse import urlparse

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
IS_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PUBLIC_DOMAIN"))
DEBUG = env_bool("DEBUG", not IS_RAILWAY)

railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
default_allowed_hosts = "127.0.0.1,localhost"
if railway_domain:
    default_allowed_hosts = f"{default_allowed_hosts},{railway_domain}"
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", default_allowed_hosts)

default_csrf_origins = f"https://{railway_domain}" if railway_domain else ""
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", default_csrf_origins)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", IS_RAILWAY)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", IS_RAILWAY)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", IS_RAILWAY)
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000" if IS_RAILWAY else "0"))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "pipeline",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "feed_on.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "feed_on.wsgi.application"


def mysql_database_config(
    name: str,
    user: str,
    password: str,
    host: str,
    port: str,
) -> dict[str, object]:
    return {
        "ENGINE": "django.db.backends.mysql",
        "NAME": name,
        "USER": user,
        "PASSWORD": password,
        "HOST": host,
        "PORT": port,
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }


def mysql_url_config(url: str) -> dict[str, object]:
    parsed = urlparse(url)
    return mysql_database_config(
        name=parsed.path.lstrip("/") or "feed_on",
        user=parsed.username or "",
        password=parsed.password or "",
        host=parsed.hostname or "127.0.0.1",
        port=str(parsed.port or 3306),
    )


db_engine = os.getenv("DB_ENGINE", "mysql").strip().lower()
mysql_url = os.getenv("MYSQL_URL") or os.getenv("DATABASE_URL", "")
if mysql_url.startswith(("mysql://", "mysql2://")):
    DATABASES = {"default": mysql_url_config(mysql_url)}
elif os.getenv("MYSQLHOST"):
    DATABASES = {
        "default": mysql_database_config(
            name=os.getenv("MYSQLDATABASE", "feed_on"),
            user=os.getenv("MYSQLUSER", "feed_on"),
            password=os.getenv("MYSQLPASSWORD", ""),
            host=os.getenv("MYSQLHOST", "127.0.0.1"),
            port=os.getenv("MYSQLPORT", "3306"),
        )
    }
elif db_engine == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / os.getenv("SQLITE_NAME", "db.sqlite3"),
        }
    }
else:
    DATABASES = {
        "default": mysql_database_config(
            name=os.getenv("DB_NAME", "feed_on"),
            user=os.getenv("DB_USER", "feed_on"),
            password=os.getenv("DB_PASSWORD", ""),
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=os.getenv("DB_PORT", "3306"),
        )
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

redis_url = os.getenv("REDIS_URL")
CELERY_BROKER_URL = os.getenv(
    "CELERY_BROKER_URL",
    redis_url or "redis://127.0.0.1:6379/0",
)
CELERY_RESULT_BACKEND = os.getenv(
    "CELERY_RESULT_BACKEND",
    redis_url or "redis://127.0.0.1:6379/1",
)
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "7200"))

FEED_ON_ONTOLOGY_PATH = os.getenv("FEED_ON_ONTOLOGY_PATH", "ontology/FEED-ON.ofn")
FEED_ON_RUN_REASONER = env_bool("FEED_ON_RUN_REASONER", True)
FEED_ON_REASONER = os.getenv("FEED_ON_REASONER", "pellet")
FEEDBACK_CHUNK_SIZE = int(os.getenv("FEEDBACK_CHUNK_SIZE", "500"))
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "200"))

JIRA_SERVER = os.getenv("JIRA_SERVER", os.getenv("JIRA_URL", ""))
JIRA_URL = JIRA_SERVER
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "")
JIRA_ISSUE_TYPE = os.getenv("JIRA_ISSUE_TYPE", "Task")
JIRA_BUG_ISSUE_TYPE = os.getenv("JIRA_BUG_ISSUE_TYPE", "Bug")
JIRA_IMPROVEMENT_ISSUE_TYPE = os.getenv("JIRA_IMPROVEMENT_ISSUE_TYPE", "Task")
JIRA_DRY_RUN = env_bool("JIRA_DRY_RUN", True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT_SECONDS = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
OPENAI_BATCH_SIZE = int(os.getenv("OPENAI_BATCH_SIZE", "50"))
OPENAI_ENABLE_ANALYSIS = env_bool("OPENAI_ENABLE_ANALYSIS", True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}




