# The image Nod's tools run in when the sandbox is on (see sandbox.py). Built
# automatically the first time it's needed, tagged with a hash of this file.
#
# Nod's own source isn't copied in: the host's copy is mounted read-only at
# /opt/nod-src, so the tools here are always the host's tools. The toolhost
# venv holds their one dependency (httpx) and stays off PATH, so the agent's
# `python` is the plain system interpreter.
#
# To give the agent more, build your own image FROM this one and set
# `image` under [sandbox] in config.toml.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates procps \
 && rm -rf /var/lib/apt/lists/* \
 && python -m venv /opt/toolhost \
 && /opt/toolhost/bin/pip install --no-cache-dir httpx==0.28.1
