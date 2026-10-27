# Build the official Telegram Local Bot API server. Local mode supports uploads up to 2000 MB.
FROM ubuntu:22.04 AS telegram-api-builder
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake g++ make gperf zlib1g-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --recurse-submodules https://github.com/tdlib/telegram-bot-api.git
RUN cmake -S telegram-bot-api -B telegram-bot-api/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build telegram-bot-api/build --target install -j2

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg libstdc++6 libssl3 zlib1g \
    && curl -fsSL https://pgp.mongodb.com/server-8.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-8.0.gpg \
    && echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main" \
       > /etc/apt/sources.list.d/mongodb-org-8.0.list \
    && apt-get update && apt-get install -y --no-install-recommends mongodb-database-tools \
    && rm -rf /var/lib/apt/lists/*
COPY --from=telegram-api-builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]
