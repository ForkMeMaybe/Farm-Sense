from .common import *
import dj_database_url
import os

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "").split(" ")

CORS_ALLOWED_ORIGINS = [
    "https://farm-frontend-v2.onrender.com",
]

DEFAULT_FROM_EMAIL = "beast41514@gmail.com"

DATABASES = {"default": dj_database_url.config()}


DEBUG = False

EMAIL_BACKEND = "sendgrid_backend.SendgridBackend"

SENDGRID_SANDBOX_MODE_IN_DEBUG = False
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")

SECRET_KEY = os.environ.get("SECRET_KEY")
