FROM ghcr.io/xtls/xray-core:26.9.9 AS xray

FROM python:3.12-slim
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
WORKDIR /app
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest==9.2.0 'fakeredis[lua]' \
    && playwright install --with-deps chromium
COPY . .
RUN python -m compileall -q \
        main.py app_v1.py xray_runtime.py luckyjet_password_gate.py luckyjet_source_guard.py luckyjet_reliable_gate.py src \
    && pytest -q
CMD ["python", "app_v1.py"]
