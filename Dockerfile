# A deliberately boring image: system Python, no build step, no compiler.
# Everything this app needs is pure Python or a wheel, so the build is fast and
# reproducible and nothing has to be compiled on the host.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    STOCKBOT_DB=/data/stockbot.db \
    STOCKBOT_BEHIND_PROXY=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them. Installed from
# the lock file: every package pinned with the hashes of its files, so a build
# installs exactly what was tested and refuses anything else. --no-deps because
# the lock already lists every package; nothing unlisted may come in.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock

COPY . .

# The database and the cookie key live on a mounted volume, so a redeploy does
# not wipe the journal or sign everybody out.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# The platform's health check uses this; it touches the database rather than
# only proving the process is alive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'), timeout=4).status == 200 else 1)"

CMD ["python", "serve.py"]
