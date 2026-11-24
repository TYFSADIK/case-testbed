# One image for the Python services: site A sim, site B, gateway, backend, tools.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest==8.3.3
COPY case case
COPY world world
COPY scenarios scenarios
COPY services services
COPY tools tools
COPY tests tests
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
