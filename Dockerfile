FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest \
    && playwright install --with-deps chromium
COPY . .
ENV PYTHONPATH=/app/src
RUN python -m compileall -q main.py app_v1.py src luckyjet_password_gate.py \
    && pytest -q
CMD ["python", "app_v1.py"]
