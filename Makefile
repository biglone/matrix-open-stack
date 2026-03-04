SHELL := /bin/bash

bootstrap:
	./scripts/bootstrap.sh

up:
	docker compose up -d --build

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs --tail=150 -f

health:
	curl -fsS http://127.0.0.1:6167/_matrix/client/versions >/dev/null && echo "matrix ok"
	curl -fsS http://127.0.0.1:6180/health >/dev/null && echo "control api ok"

create-bot:
	@if [ -z "$(USERNAME)" ]; then echo "Usage: make create-bot USERNAME=opsbot"; exit 1; fi
	./scripts/create_bot_secure.sh --username "$(USERNAME)"
