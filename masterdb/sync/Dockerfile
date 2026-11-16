FROM python:3.12-slim
RUN pip install --no-cache-dir "oracledb>=2.0" "psycopg[binary]>=3.1"
COPY sync.py /sync.py
COPY identifiers.py /identifiers.py
RUN groupadd --gid 10001 syncworker && useradd --uid 10001 --gid 10001 --no-create-home syncworker
USER 10001:10001
ENTRYPOINT ["python", "/sync.py"]

COPY LICENSE /licenses/Arecore-LICENSE
