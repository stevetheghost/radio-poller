FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    STATIONS_FILE=/config/stations.json \
    TZ=America/New_York

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# The starting stations (read on the first run only); docker-compose can mount ./config over this.
COPY config /config
COPY LICENSE THIRD_PARTY_NOTICES.md /licenses/

RUN adduser -D -u 10001 radio && mkdir /data && chown radio /data
USER radio
VOLUME /data
EXPOSE 5555

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
    CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '5555'), timeout=4)" || exit 1

CMD ["python", "-m", "app.main"]
