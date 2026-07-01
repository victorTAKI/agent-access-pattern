# Handy targets for local dev.

.PHONY: up down build logs ps rebuild clean smoke tag-latest push

IMAGES = mcp-server mcp-gateway agent ui
REGISTRY ?= ghcr.io/victortaki/agent-access-pattern
TAG      ?= latest

up:
	docker compose up -d

down:
	docker compose down -v

build:
	docker compose build

rebuild:
	docker compose build --no-cache

logs:
	docker compose logs -f

ps:
	docker compose ps

clean:
	docker compose down -v --remove-orphans

smoke:
	@echo "▶ Keycloak"     && curl -fsS http://localhost:8080/realms/agents/.well-known/openid-configuration >/dev/null && echo OK
	@echo "▶ MCP Gateway"  && curl -fsS http://localhost:9000/healthz && echo
	@echo "▶ Agent"        && curl -fsS http://localhost:8000/healthz && echo
	@echo "▶ Denied call:  curl-based end-to-end test"
	@bash scripts/smoke.sh

push:
	@for c in $(IMAGES); do \
		docker tag agent-access-pattern-$$c:latest $(REGISTRY)/$$c:$(TAG) ; \
		docker push $(REGISTRY)/$$c:$(TAG) ; \
	done
