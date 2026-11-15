#!/bin/bash
set -e
# The hub-authority deployment (k3s/hub-authority.yaml) runs the same image as the
# ArcoreHub control plane: only hubauthority.control_urls, TLS terminated here with
# the key whose pin Hubs hold. The portal deployment owns migrations.
if [ "${PORTAL_ROLE:-portal}" = "hub-authority" ]; then
  exec gunicorn portal.wsgi:application     --bind 0.0.0.0:8443     --certfile /etc/hub-tls/tls.crt     --keyfile /etc/hub-tls/tls.key     --workers "${GUNICORN_WORKERS:-2}"     --timeout 30     --access-logfile -
fi
python manage.py migrate --noinput
# /auth/check is called for every request to every gated app: several workers,
# short timeout.
exec gunicorn portal.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-3}" \
  --timeout 30 \
  --access-logfile -
