# loadkit-worker: one small image, role chosen by the first argument (batch | produce | consume | shop | users | calibrate)
FROM python:3.12-slim
RUN pip install --no-cache-dir "redis>=5,<6" && useradd -u 10001 -m worker
COPY worker.py /app/worker.py
USER 10001
ENTRYPOINT ["python", "-u", "/app/worker.py"]
