#!/bin/sh
# Starts MadPlan: brings the database up to date, then serves the app on the ingress port.
# No --proxy-headers: the app must see that a request comes from the Supervisor's address, not
# the browser's, as only the Supervisor may say who the user is.
set -e
mkdir -p /data
cd /app
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8099 --no-server-header
