# Glama uses this Dockerfile to run thread-keeper in its sandbox for
# Quality-score evaluation: it builds the image, speaks MCP over stdio,
# and inspects tool definitions. End users do NOT need Docker for normal
# installs — see Quickstart in README.md (pipx / uvx / install.sh).

FROM python:3.12-slim

WORKDIR /app

# Install threadkeeper from PyPI. Skip the [semantic] extra (~700 MB
# fastembed ONNX model) — Glama only inspects the MCP tool schema, not
# embedding quality.
RUN pip install --no-cache-dir threadkeeper

# Don't spawn shadow-review / curator / probe / dialectic daemons in
# the Glama sandbox — they would try to invoke claude / codex / agy
# CLIs that aren't installed here.
ENV THREADKEEPER_DISABLE_BG_DAEMONS=1

# Skip embedding paths so nothing tries to import fastembed / numpy at
# runtime when [semantic] is not installed.
ENV THREADKEEPER_NO_EMBEDDINGS=1

# Predictable writable location for SQLite. threadkeeper.config
# auto-creates the parent directory on first connect.
ENV THREADKEEPER_DB=/data/db.sqlite
VOLUME ["/data"]

CMD ["python", "-m", "threadkeeper.server"]
