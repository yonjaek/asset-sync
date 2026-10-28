FROM docker.io/library/python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "cryptography>=42,<47"
WORKDIR /app
COPY server.py /app/server.py
CMD ["python", "-u", "/app/server.py"]
