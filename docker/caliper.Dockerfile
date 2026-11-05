FROM node:24-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 python3-venv util-linux \
    && rm -r /var/lib/apt/lists/* \
    && python3 -m venv /opt/caliper \
    && /opt/caliper/bin/pip install --no-cache-dir caliper-eval==0.22.0 \
    && npm install --global @openai/codex@0.164.0 \
    && install -d -m 0700 /run/caliper-private

COPY --chmod=0755 docker/caliper-codex /usr/local/bin/caliper-codex
ENV CODEX_CLI_PATH=/usr/local/bin/caliper-codex PYTHONSAFEPATH=1
WORKDIR /tmp
ENTRYPOINT ["timeout", "--kill-after=5s", "175s", "/opt/caliper/bin/caliper"]
