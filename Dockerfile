# Nod in a container: the container is the sandbox instead of the host.
#
# The agent's tools (run_shell, cud_file, http_get) still have the full reach
# of the process user, but that user is `nod` inside this container, not a
# user on the box. Nod's own code and venv are installed as root, so the agent
# can't edit its runtime; its data lives in /var/lib/nod (a volume).
#
#   docker build -t nod .
#   docker build -t nod --build-context extras=../nod-extras .   # with job tools
#
# deploy/docker/nod-docker.sh wraps building and running it.

# nod-extras is optional. This empty stage stands in for it; passing
# `--build-context extras=<path>` replaces it with a real checkout.
FROM scratch AS extras

FROM python:3.12-slim

# What the agent's run_shell can use. Add anything you expect it to need.
# The slim image already has `python` on PATH (the system interpreter, not
# Nod's venv), so no shim is needed as on the systemd host.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates procps \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --shell /bin/bash nod

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

COPY . /opt/nod
RUN cd /opt/nod && UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev
COPY --from=extras / /opt/nod-extras/
COPY deploy/docker/container.toml /etc/nod/config.toml

# Created in the image so a fresh named volume starts out owned by `nod`.
RUN mkdir -p /var/lib/nod/memory /var/lib/nod/jobs /var/lib/nod/workspace \
 && chown -R nod:nod /var/lib/nod

# The venv stays off PATH so the agent's `python` is the system interpreter.
ENV PYTHONPATH=/opt/nod-extras/src \
    PYTHONUNBUFFERED=1

USER nod
# The agent's shell starts here.
WORKDIR /var/lib/nod/workspace
VOLUME /var/lib/nod
EXPOSE 8080

ENTRYPOINT ["/opt/nod/.venv/bin/nod", "-c", "/etc/nod/config.toml"]
CMD ["serve"]
