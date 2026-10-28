FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest \
    && playwright install --with-deps chromium
COPY . .
RUN python -m compileall -q \
        main.py app_v1.py luckyjet_password_gate.py luckyjet_source_guard.py luckyjet_reliable_gate.py src \
    && pytest -q
CMD ["python", "app_v1.py"]
