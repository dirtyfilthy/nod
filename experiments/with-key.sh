#!/bin/sh
# Run a command with the OpenRouter key set, without leaving it in your shell:
#   ./experiments/with-key.sh uv run python experiments/<exp>/run.py
set -eu
OPENROUTER_API_KEY=$(
  SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt" \
    sops -d --extract '["secrets"]["OPENROUTER_API_KEY"]' \
    "$HOME/src/mine/myinfra/secrets/projects/nodgrader.sops.yaml"
)
export OPENROUTER_API_KEY
exec "$@"
