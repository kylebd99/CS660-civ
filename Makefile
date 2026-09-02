# make up      start postgres
# make reset   (re)load the schema and deal a new world
# make play    attach the terminal client
# make sql     open a psql prompt on the live game
# make test    run the tests (needs neither docker nor a running postgres)

PSQL := docker compose exec -T db psql -U civ -d civ -v ON_ERROR_STOP=1 -q

.PHONY: up down reset play sql deps dev-deps test demo-economy demo-scale

up:
	docker compose up -d
	@until docker compose exec -T db pg_isready -U civ >/dev/null 2>&1; do sleep 0.5; done
	@echo "postgres ready on localhost:5432"

# Files are piped in rather than passed with -f: they live on your machine,
# not inside the container.
reset: up
	@for f in sql/*.sql; do echo "  loading $$f"; $(PSQL) < $$f || exit 1; done
	@$(PSQL) -c "SELECT game.new_game(42, 42, 3)" >/dev/null
	@echo "new world dealt -- run 'make play'"

play:
	python3 client/civ.py

sql:
	docker compose exec db psql -U civ -d civ

deps:
	pip install -r requirements.txt

dev-deps:
	pip install -r requirements-dev.txt

# The tests start their own PostgreSQL from a pip wheel, so this works on a
# machine with no docker and no postgres installed, and it does not disturb
# whatever world `make reset` last dealt.
test:
	python3 -m pytest tests/ -q

# After reviewing a deliberate change to what the client draws.
update-golden:
	UPDATE_GOLDEN=1 python3 -m pytest tests/test_terminal.py -q

demo-economy:
	$(PSQL) < demo/01-economy.sql

demo-scale:
	$(PSQL) < demo/02-scale.sql

down:
	docker compose down
