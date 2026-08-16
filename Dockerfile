FROM python:3.12-slim

ARG GHEALTH_VERSION=1.1.1
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sL "https://github.com/rudrankriyam/Google-Health-CLI/releases/download/${GHEALTH_VERSION}/Google-Health-CLI-${GHEALTH_VERSION}-linux-${TARGETARCH}.tar.gz" \
        -o /tmp/ghealth.tar.gz \
    && tar -xzf /tmp/ghealth.tar.gz -C /usr/local/bin ghealth \
    && chmod +x /usr/local/bin/ghealth \
    && rm /tmp/ghealth.tar.gz

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY sync.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
