# glinet-dashboard: GL.iNet router collector + start page
FROM python:3.12-alpine

# openssl computes the crypt(3) hash for the GL.iNet login challenge
RUN apk add --no-cache openssl \
 && pip install --no-cache-dir websocket-client==1.8.0 PyYAML==6.0.2 \
 && adduser -D -H -u 10001 app

WORKDIR /app
COPY collector.py index.html ./
# Example configuration as the default; docker-compose mounts ./config over it (edits need no rebuild)
COPY config.example/ ./config/
# Writable cache (quote of the day); docker-compose mounts a named volume here
RUN mkdir -p /app/data && chown app /app/data

USER app
ENV PYTHONUNBUFFERED=1 PORT=3100
EXPOSE 3100
CMD ["python", "/app/collector.py"]
