FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest \
    && playwright install --with-deps chromium
COPY . .
ENV PYTHONPATH=/app/src
RUN pytest -q
CMD ["python", "main.py"]
