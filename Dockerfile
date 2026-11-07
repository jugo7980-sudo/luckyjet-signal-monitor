FROM ghcr.io/xtls/xray-core:26.9.9 AS xray
FROM python:3.12-slim
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
WORKDIR /app
RUN pip install --no-cache-dir 'httpx[socks]==0.28.1'
COPY xray_runtime.py probe_happ.py ./
CMD ["python", "-u", "probe_happ.py"]
