# Pressure game microservice (HTTP). Run with the port bound to localhost only:
#   docker build -t strata-pressure services/pressure
#   docker run --rm -p 127.0.0.1:8710:8710 -e SEED=42 -e MUTATIONS=2 strata-pressure
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY blackbox/ blackbox/
RUN useradd --create-home --uid 10001 pressure
USER pressure

ENV SEED=0 \
    MUTATIONS=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8710

# 0.0.0.0 is needed inside the container; publish the port on 127.0.0.1 only.
CMD ["sh", "-c", "exec python -m blackbox.http_server --host 0.0.0.0 --port 8710 --seed \"$SEED\" --mutations \"$MUTATIONS\""]
