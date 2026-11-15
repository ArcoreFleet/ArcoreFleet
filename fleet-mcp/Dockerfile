FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fleet_mcp ./fleet_mcp
RUN useradd --system --uid 10001 fleet
USER 10001

EXPOSE 8000
# Stateless: any replica can answer any request. Forwarded headers come from Traefik.
CMD ["uvicorn", "fleet_mcp.server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

COPY LICENSE /licenses/Arecore-LICENSE
