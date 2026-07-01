#!/usr/bin/env sh
# One-liner installer:
#   curl -fsSL https://raw.githubusercontent.com/<you>/agent-access-pattern/main/install.sh | sh
#
# Downloads the compose file + assets and starts the stack from prebuilt
# images published on ghcr.io. No git clone required.

set -eu

REPO="${AAP_REPO:-victortaki/agent-access-pattern}"
REF="${AAP_REF:-main}"
DEST="${AAP_DIR:-agent-access-pattern}"
BASE="https://raw.githubusercontent.com/${REPO}/${REF}"

echo "▶ Fetching agent-access-pattern from ${REPO}@${REF}"
mkdir -p "${DEST}/keycloak"

curl -fsSL "${BASE}/docker-compose.yml"                  -o "${DEST}/docker-compose.yml"
curl -fsSL "${BASE}/.env.example"                        -o "${DEST}/.env"
curl -fsSL "${BASE}/keycloak/realm-agents.json"          -o "${DEST}/keycloak/realm-agents.json"

# Rewrite compose to use prebuilt images (no local build context).
sed -i.bak -E 's|^\s+build:.*$||g' "${DEST}/docker-compose.yml"
rm -f "${DEST}/docker-compose.yml.bak"

echo "▶ Pulling images and starting the stack"
( cd "${DEST}" && docker compose pull && docker compose up -d )

echo
echo "✅ Up and running:"
echo "   • Streamlit UI    http://localhost:8501   (login: demo / demo)"
echo "   • Keycloak admin  http://localhost:8080   (login: admin / admin)"
echo "   • Agent API       http://localhost:8000/healthz"
echo "   • MCP Gateway     http://localhost:9000/healthz"
echo
echo "Try:  curl http://localhost:9000/healthz"
