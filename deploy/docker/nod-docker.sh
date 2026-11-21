#!/usr/bin/env bash
# Build and run Nod in a Docker container, with its data on a named volume.
#
#   nod-docker.sh build            build the image (adds nod-extras if found)
#   nod-docker.sh serve            run `nod serve` in the background
#   nod-docker.sh chat [session]   interactive CLI chat in a throwaway container
#   nod-docker.sh logs | stop      follow the service's logs / stop and remove it
#   nod-docker.sh shell            a shell in the running service, as the agent's user
#   nod-docker.sh reset            remove the container and the volume (history too)
#
# Settings come from the environment:
#   NOD_ENV_FILE  env file with OPENROUTER_API_KEY and NOD_API_TOKEN
#                 (default /etc/nod/nod.env, then ./.env)
#   NOD_EXTRAS    nod-extras checkout to build in (default: ../nod-extras next to
#                 the repo, if it exists; set to "" to leave it out)
#   NOD_CONFIG    a config.toml to mount over the image's (optional)
#   NOD_PORT      host port, published on 127.0.0.1 only (default 8080)
#   NOD_IMAGE, NOD_NAME, NOD_VOLUME  (default nod, nod, nod-data)

set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
image=${NOD_IMAGE:-nod}
name=${NOD_NAME:-nod}
volume=${NOD_VOLUME:-nod-data}
port=${NOD_PORT:-8080}

env_file=${NOD_ENV_FILE:-}
if [ -z "$env_file" ]; then
    if [ -r /etc/nod/nod.env ]; then env_file=/etc/nod/nod.env
    elif [ -r .env ]; then env_file=.env
    fi
fi

# The same limits for every container: no capabilities, no privilege gain,
# bounded memory and process count, and an init to reap the agent's orphans.
flags=(--init --cap-drop ALL --security-opt no-new-privileges
    --memory 2g --pids-limit 512 -v "$volume:/var/lib/nod")
if [ -n "$env_file" ]; then flags+=(--env-file "$env_file"); fi
if [ -n "${NOD_CONFIG:-}" ]; then
    config=$(cd "$(dirname "$NOD_CONFIG")" && pwd)/$(basename "$NOD_CONFIG")
    flags+=(-v "$config:/etc/nod/config.toml:ro")
fi

need_env() {
    if [ -z "$env_file" ]; then
        echo "no env file: set NOD_ENV_FILE, or create /etc/nod/nod.env or ./.env" >&2
        exit 2
    fi
}

cmd=${1:-}
[ $# -gt 0 ] && shift

case "$cmd" in
build)
    extras=${NOD_EXTRAS-$repo/../nod-extras}
    if [ -n "$extras" ] && [ -d "$extras" ]; then
        echo "building with nod-extras from $extras"
        docker build -t "$image" --build-context "extras=$extras" "$repo"
    else
        docker build -t "$image" "$repo"
    fi
    ;;
serve)
    need_env
    docker run -d --name "$name" --restart unless-stopped \
        -p "127.0.0.1:$port:8080" "${flags[@]}" "$image" serve
    echo "Nod serving on http://127.0.0.1:$port (logs: $0 logs)"
    ;;
chat)
    need_env
    docker run --rm -it "${flags[@]}" "$image" chat --session "${1:-cli}"
    ;;
logs)
    docker logs -f "$name"
    ;;
stop)
    docker rm -f "$name"
    ;;
shell)
    docker exec -it "$name" bash
    ;;
reset)
    docker rm -f "$name" 2>/dev/null || true
    docker volume rm "$volume"
    ;;
*)
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
