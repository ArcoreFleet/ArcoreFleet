FROM python:3.12-slim
RUN pip install --no-cache-dir "oracledb>=2.0" "psycopg[binary]>=3.1"
COPY sync.py /sync.py
ENTRYPOINT ["python", "/sync.py"]

COPY LICENSE /licenses/Arecore-LICENSE
