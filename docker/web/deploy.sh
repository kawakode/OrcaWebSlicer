#!/usr/bin/env bash
# Install, update, and roll back the production deployment of compose.prod.yml
# on one Linux host. The procedures it automates are described in
# docs/web/deploy.md, which also covers doing each step by hand.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

ENV_FILE=.env
# One previously deployed tag per line, newest last; rollback pops from it.
HISTORY_FILE=.deploy-history
WAIT_SECONDS=${ORCA_WEB_DEPLOY_WAIT_SECONDS:-300}
compose=(docker compose -f compose.prod.yml)

usage() {
    cat <<'EOF'
usage: deploy.sh <command> [argument]

  init            create .env from .env.example and ask for the site settings
  password        set a new site password
  up              pull the configured images and start or restart the stack
  update <tag>    deploy another image tag; restores the current one on failure
  rollback [tag]  go back to the previous tag (or to the one given)
  status          show the running tag, revision, and container health
  logs [service]  follow the logs (proxy or api)
EOF
}

require_env() {
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "No $ENV_FILE yet: run ./deploy.sh init first." >&2
        exit 1
    fi
}

get_env() {
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

# Replaces KEY's line, or appends it. The value is written verbatim.
set_env() {
    local key=$1 value=$2 tmp
    tmp=$(mktemp "$ENV_FILE.XXXXXX")
    awk -v key="$key" -v line="$key=$value" '
        index($0, key "=") == 1 { if (!done) print line; done = 1; next }
        { print }
        END { if (!done) print line }
    ' "$ENV_FILE" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
}

prompt() {
    local label=$1 default=$2 answer
    read -r -p "$label [$default]: " answer
    printf '%s' "${answer:-$default}"
}

cmd_password() {
    require_env
    local password confirm hash proxy_image
    read -r -s -p "Site password: " password; echo
    read -r -s -p "Repeat it: " confirm; echo
    if [[ -z "$password" || "$password" != "$confirm" ]]; then
        echo "The passwords are empty or differ." >&2
        exit 1
    fi
    proxy_image=$(get_env ORCA_WEB_PROXY_IMAGE)
    # Piped on stdin so the password never appears in a process list.
    hash=$(printf '%s\n' "$password" \
        | docker run --rm -i "${proxy_image:-caddy:2.10.2-alpine}" caddy hash-password)
    # Single quotes stop Compose from expanding the hash's "$" signs.
    set_env ORCA_WEB_PASSWORD_HASH "'$hash'"
    echo "Password saved. Run ./deploy.sh up to apply it."
}

cmd_init() {
    if [[ -f "$ENV_FILE" ]]; then
        echo "$ENV_FILE already exists; edit it directly or delete it to start over." >&2
        exit 1
    fi
    install -m 600 .env.example "$ENV_FILE"
    set_env ORCA_WEB_DOMAIN "$(prompt "Domain name" "$(get_env ORCA_WEB_DOMAIN)")"
    set_env ORCA_WEB_ACME_EMAIL "$(prompt "Let's Encrypt e-mail" "$(get_env ORCA_WEB_ACME_EMAIL)")"
    set_env ORCA_WEB_USER "$(prompt "Site login" "$(get_env ORCA_WEB_USER)")"
    cmd_password
}

start() {
    "${compose[@]}" pull
    "${compose[@]}" up -d --remove-orphans --wait --wait-timeout "$WAIT_SECONDS"
}

cmd_up() {
    require_env
    start
    cmd_status
}

cmd_update() {
    require_env
    local target=${1:?usage: deploy.sh update <tag>} previous
    previous=$(get_env ORCA_WEB_TAG)
    set_env ORCA_WEB_TAG "$target"
    if start; then
        if [[ -n "$previous" && "$previous" != "$target" ]]; then
            echo "$previous" >> "$HISTORY_FILE"
        fi
        echo "Now running $target (previous: ${previous:-none})."
        return
    fi
    echo "Tag $target did not become healthy; restoring $previous." >&2
    "${compose[@]}" logs --tail 100 api >&2 || true
    set_env ORCA_WEB_TAG "$previous"
    start
    exit 1
}

cmd_rollback() {
    require_env
    local target=${1:-}
    if [[ -z "$target" ]]; then
        if [[ ! -s "$HISTORY_FILE" ]]; then
            echo "No earlier tag recorded; pass one: deploy.sh rollback <tag>." >&2
            exit 1
        fi
        target=$(tail -n 1 "$HISTORY_FILE")
        sed -i '$d' "$HISTORY_FILE"
    fi
    set_env ORCA_WEB_TAG "$target"
    start
    echo "Rolled back to $target."
}

cmd_status() {
    require_env
    local image
    image="$(get_env ORCA_WEB_IMAGE):$(get_env ORCA_WEB_TAG)"
    echo "Configured image: $image"
    docker image inspect --format 'Revision: {{index .Config.Labels "org.opencontainers.image.revision"}}' \
        "$image" 2>/dev/null || true
    "${compose[@]}" ps
}

case "${1:-}" in
    init) cmd_init ;;
    password) cmd_password ;;
    up) cmd_up ;;
    update) shift; cmd_update "$@" ;;
    rollback) shift; cmd_rollback "$@" ;;
    status) cmd_status ;;
    logs) shift; require_env; "${compose[@]}" logs -f --tail 200 "$@" ;;
    *) usage; exit 2 ;;
esac
