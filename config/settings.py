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
DATABASE_URL = os.environ.get('DATABASE_URL')
IS_PRODUCTION = bool(DATABASE_URL)

if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=600,
            ssl_require=True,  # Neon requires SSL
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": config("DB_NAME", default="faruq_db"),
            "USER": config("DB_USER", default="faruq_user"),
            "PASSWORD": config("DB_PASSWORD", default=""),
            "HOST": config("DB_HOST", default="127.0.0.1"),
            "PORT": config("DB_PORT", default="5432"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {},
        }
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
# Uploaded receipts and avatars go to Cloudinary rather than local disk.
#
# WHY: the app runs on hosts with an ephemeral filesystem (Render, Railway,
# Fly), where anything written to MEDIA_ROOT vanishes on the next deploy or
# restart. Receipts are the evidence behind a payment - losing them silently
# is not survivable for a credit business.
#
# The switch is driven by whether credentials are present, NOT by DEBUG. That
# way a developer with no Cloudinary account still gets a working local setup,
# and a production box that is missing its keys fails loudly at upload time
# rather than quietly writing files that will be deleted later.
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
        # HTTPS delivery URLs. Note this does NOT make the files private -
        # a Cloudinary media URL is unguessable but publicly fetchable by
        # anyone who has it. Receipts are therefore protected by the app not
        # showing the URL to the wrong person, not by the storage layer.
        "SECURE": True,
        "MEDIA_TAG": "faruq",  # <--- Update this tag per app to separate media
        "PREFIX": "faruq_management",     # <--- Add this to isolate uploads into an explicit folder
        "INVALID_VIDEO_ERROR_MESSAGE": "Please upload a valid image or PDF file.",
    }

    # RawMediaCloudinaryStorage handles PDFs as well as images. The plain
    # MediaCloudinaryStorage rejects non-image uploads, which would break
    # receipt capture the first time somebody attaches a bank statement PDF.
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

# The phone is not a browser and sends no Origin we control, but the Django
# admin and any future web client do. Keep this tight in production.
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
# Instead of `manage.py createsuperuser`, staff register themselves and prove
# which role they are entitled to with a shared passcode.
#
# SECURITY NOTE, read before deploying:
# A passcode is a SHARED secret. Anyone who learns it can create an account of
# that role, and it cannot be revoked for one person without rotating it for
# everyone. That is an acceptable trade for a small shop where the owner hands
# the code to a new hire in person; it is NOT acceptable for a large team.
# Treat these as you would the shop keys.
#
# Leaving either blank DISABLES registration for that role - deliberately, so
# an unconfigured deployment cannot be signed up to by strangers.
REGISTRATION_PASSCODE_ADMIN = config("PASSCODE_ADMIN", default="")
REGISTRATION_PASSCODE_MANAGER = config("PASSCODE_MANAGER", default="")
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
