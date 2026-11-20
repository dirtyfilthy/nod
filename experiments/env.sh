# Source me: puts the nodgrader OpenRouter key in this shell.
#   source experiments/env.sh
# On macOS sops won't find the age identity without SOPS_AGE_KEY_FILE.

OPENROUTER_API_KEY=$(
  SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt" \
    sops -d --extract '["secrets"]["OPENROUTER_API_KEY"]' \
    "$HOME/src/mine/myinfra/secrets/projects/nodgrader.sops.yaml"
) && export OPENROUTER_API_KEY && echo "OPENROUTER_API_KEY set"
