#!/usr/bin/env bash
# End-to-end smoke test: hits the agent's /chat endpoint after obtaining
# a user token via the password grant. Demonstrates the allow / deny
# behavior enforced at the MCP gateway.
#
# No external tooling required beyond curl + python3.

set -euo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
REALM="${REALM:-agents}"
UI_CLIENT="${UI_CLIENT:-ui-client}"
USERNAME="${USERNAME:-demo}"
PASSWORD="${PASSWORD:-demo}"
AGENT_URL="${AGENT_URL:-http://localhost:8000}"

PY="$(command -v python3 || command -v python || true)"
if [ -z "${PY}" ]; then
  echo "❌ python3 (or python) is required. Install with: sudo apt install python3"
  exit 1
fi

pretty() { "${PY}" -c "import json,sys; print(json.dumps(json.loads(sys.stdin.read()), indent=2, ensure_ascii=False))"; }

echo "▶ Getting user token from Keycloak"
TOKEN_JSON=$(curl -fsS \
  -d "grant_type=password" \
  -d "client_id=${UI_CLIENT}" \
  -d "username=${USERNAME}" \
  -d "password=${PASSWORD}" \
  "${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/token")

TOKEN=$(printf '%s' "${TOKEN_JSON}" | "${PY}" -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
if [ -z "${TOKEN}" ]; then
  echo "❌ Failed to obtain access token. Response was:"
  echo "${TOKEN_JSON}"
  exit 1
fi
echo "   token acquired (${#TOKEN} chars)"

call() {
  local question="$1"
  echo
  echo "▶ ${question}"
  # -f dropped so we can see the body on 4xx/5xx too
  curl -sS -X POST "${AGENT_URL}/chat" \
    -H "content-type: application/json" \
    -d "$("${PY}" -c "import json,sys; print(json.dumps({'question': sys.argv[1], 'user_token': sys.argv[2]}))" "${question}" "${TOKEN}")" \
    | pretty
}

call "what is the weather in Paris?"
call "list all the employees of the company"
