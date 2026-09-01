"""
Django settings for the Faruq Inventory, Sales & Credit Management System.
"""
from pathlib import Path

from decouple import Csv, config
import os
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
SECRET_KEY = config("SECRET_KEY", default="dev-only-insecure-key-change-me")
DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*", cast=Csv())
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Third party
    "widget_tweaks",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    # Local
    "core.apps.CoreConfig",
    "accounts.apps.AccountsConfig",
    "inventory.apps.InventoryConfig",
    "sales.apps.SalesConfig",
    "credit.apps.CreditConfig",
    "reports.apps.ReportsConfig",
    "api.apps.ApiConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.CurrentUserMiddleware",
]

ROOT_URLCONF = "config.urls"

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
                "core.context_processors.business_settings",
                "core.context_processors.sidebar_badges",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database - PostgreSQL
# ---------------------------------------------------------------------------
NEON_DATABASE_URL = "postgresql://neondb_owner:npg_MIvUC3z4xPwZ@ep-solitary-salad-aq5rll9w-pooler.c-8.us-east-1.aws.neon.tech/farquDB?sslmode=require"

DATABASE_URL = os.environ.get('DATABASE_URL', NEON_DATABASE_URL)
IS_PRODUCTION = bool(DATABASE_URL)

DATABASES = {
    'default': dj_database_url.config(
        default=DATABASE_URL,
        conn_max_age=600,
        ssl_require=True,  # Neon requires SSL
    )
}

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/reports/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = 60 * 60 * 8  # 8 hour working day

# ---------------------------------------------------------------------------
# I18N
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = config("TIME_ZONE", default="Africa/Addis_Ababa")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & Media
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# The manifest storage requires `collectstatic` to have been run first and
# raises "Missing staticfiles manifest entry" if it hasn't. Dev uses plain
# storage; production gets compression + cache-busting hashes.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# ---------------------------------------------------------------------------
# Cloudinary media storage
# ---------------------------------------------------------------------------
CLOUDINARY_CLOUD_NAME = config("CLOUDINARY_CLOUD_NAME", default="")
CLOUDINARY_API_KEY = config("CLOUDINARY_API_KEY", default="")
CLOUDINARY_API_SECRET = config("CLOUDINARY_API_SECRET", default="")

USE_CLOUDINARY = all(
    [CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]
)

if USE_CLOUDINARY:
    INSTALLED_APPS += ["cloudinary", "cloudinary_storage"]

    CLOUDINARY_STORAGE = {
        "CLOUD_NAME": CLOUDINARY_CLOUD_NAME,
        "API_KEY": CLOUDINARY_API_KEY,
        "API_SECRET": CLOUDINARY_API_SECRET,
        "SECURE": True,
        "MEDIA_TAG": "faruq",  
        "PREFIX": "faruq_management",     
        "INVALID_VIDEO_ERROR_MESSAGE": "Please upload a valid image or PDF file.",
    }

    STORAGES["default"] = {
        "BACKEND": "cloudinary_storage.storage.RawMediaCloudinaryStorage"
    }

# Receipt upload guard rails
MAX_RECEIPT_SIZE_MB = config("MAX_RECEIPT_SIZE_MB", default=5, cast=int)
ALLOWED_RECEIPT_EXTENSIONS = ["jpg", "jpeg", "png", "webp", "pdf"]

# Refuse absurd uploads before they reach disk.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000  # a large sale has many cart rows

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# REST API (consumed by the React Native app in ../mobile)
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
    },
}

if DEBUG:
    # Browsable API is convenient while developing, off in production.
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"].append(
        "rest_framework.renderers.BrowsableAPIRenderer"
    )

CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())
CORS_ALLOW_CREDENTIALS = True

# ---------------------------------------------------------------------------
# Firebase Cloud Messaging
# ---------------------------------------------------------------------------
FCM_ENABLED = config("FCM_ENABLED", default=False, cast=bool)
FIREBASE_CREDENTIALS = config("FIREBASE_CREDENTIALS", default="firebase-service-account.json")

# A credit sale at or above this amount notifies administrators. 0 disables it.
LARGE_CREDIT_ALERT = config("LARGE_CREDIT_ALERT", default=0, cast=int)

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# ---------------------------------------------------------------------------
# Business configuration
# ---------------------------------------------------------------------------
BUSINESS_NAME = config("BUSINESS_NAME", default="Faruq Trading")
BUSINESS_PHONE = config("BUSINESS_PHONE", default="")
BUSINESS_ADDRESS = config("BUSINESS_ADDRESS", default="")
CURRENCY_SYMBOL = config("CURRENCY_SYMBOL", default="ETB")
DEFAULT_CREDIT_DUE_DAYS = config("DEFAULT_CREDIT_DUE_DAYS", default=30, cast=int)

# ---------------------------------------------------------------------------
# Self-service registration
# ---------------------------------------------------------------------------
REGISTRATION_PASSCODE_ADMIN = config("PASSCODE_ADMIN", default="")
REGISTRATION_PASSCODE_MANAGER = config("PASSCODE_MANAGER", default="")
REGISTRATION_PASSCODE_SALES = config("PASSCODE_SALES", default="")
REGISTRATION_ENABLED = config("REGISTRATION_ENABLED", default=True, cast=bool)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[{asctime}] {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "credit": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "sales": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "inventory": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}