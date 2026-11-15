import os
from pathlib import Path

from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY') or get_random_secret_key()
DEBUG = os.environ.get('DJANGO_DEBUG', 'false').lower() == 'true'
ALLOWED_HOSTS = [h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get('DJANGO_TRUSTED_ORIGINS', '').split(',') if o.strip()]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'access',
    'website',
    'hubauthority',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'access.cf_access.CloudflareAccessMiddleware',  # SSO via Cloudflare Access (only when CF_ACCESS_* set)
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# The hub-authority deployment serves only the ArcoreHub control plane
# (hubauthority.control_urls); the portal serves everything else.
ROOT_URLCONF = os.environ.get('DJANGO_ROOT_URLCONF', 'portal.urls')
WSGI_APPLICATION = 'portal.wsgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# Postgres from the "portal-db" Secret (phase4-app-template/provision-app.sh);
# SQLite for local development and tests.
if os.environ.get('POSTGRES_DB'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ['POSTGRES_DB'],
            'USER': os.environ.get('POSTGRES_USER', ''),
            'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
            'HOST': os.environ.get('POSTGRES_HOST', ''),
            'PORT': os.environ.get('POSTGRES_PORT', ''),
            'CONN_MAX_AGE': 60,  # /auth/check runs on every request to every gated app
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(BASE_DIR / 'db.sqlite3'),
        }
    }

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = False
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Public URL of this portal - linked from the "no access" page shown on app hostnames.
PORTAL_URL = os.environ.get('PORTAL_URL', 'https://portal.example.com').rstrip('/')

# Behind Cloudflare Access there is no app login page; for local development
# the Django admin login works.
LOGIN_URL = '/admin/login/'

# Optional read-only operations collector. Page requests read stored snapshots only.
FLEET_GITHUB_TOKEN = os.environ.get('FLEET_GITHUB_TOKEN', '')
FLEET_BROWSERFORGE_URL = os.environ.get('FLEET_BROWSERFORGE_URL', '').rstrip('/')
FLEET_KUBERNETES_ENABLED = os.environ.get('FLEET_KUBERNETES_ENABLED', '').lower() == 'true'
FLEET_K8S_TOKEN_FILE = os.environ.get('FLEET_K8S_TOKEN_FILE', '/var/run/secrets/kubernetes.io/serviceaccount/token')
FLEET_K8S_CA_FILE = os.environ.get('FLEET_K8S_CA_FILE', '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')

# Website manager (website/). The GitHub App credentials are mounted only into the
# website worker (k3s/website-worker.yaml); web pods read stored snapshots and never
# call GitHub. See WEBSITE.md.
WEBSITE_REPOSITORY = os.environ.get('WEBSITE_REPOSITORY', 'example-owner/arcore-website')
WEBSITE_GITHUB_APP_ID = os.environ.get('WEBSITE_GITHUB_APP_ID', '')
WEBSITE_GITHUB_INSTALLATION_ID = os.environ.get('WEBSITE_GITHUB_INSTALLATION_ID', '')
WEBSITE_GITHUB_PRIVATE_KEY_FILE = os.environ.get('WEBSITE_GITHUB_PRIVATE_KEY_FILE', '/etc/website-github/private-key.pem')
WEBSITE_SYNC_MINUTES = int(os.environ.get('WEBSITE_SYNC_MINUTES', '10'))

# The Fleet MCP gateway (fleet-mcp/), shown on the API keys page. Tailnet only.
FLEET_MCP_URL = os.environ.get('FLEET_MCP_URL', 'http://fleet-control.example-tailnet.ts.net:8880/mcp')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
}
