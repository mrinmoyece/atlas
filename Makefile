.PHONY: install test lint fmt evals gate benchmark memory-ab demo run docker up down security sbom all

install:
	pip install -e ".[dev,mcp]"

test:
	pytest -q

lint:
	ruff check src tests benchmarks evals scripts

fmt:
	ruff check --fix src tests benchmarks evals scripts

evals:
	python -m evals.gate --tier standard

gate: lint test evals

benchmark:
	python -m benchmarks.run_benchmark

memory-ab:
	python -m benchmarks.memory_ab

demo:
	python scripts/demo.py

run:
	uvicorn atlas.api.app:app --reload

docker:
	docker build -t atlas:local .

up:
	docker compose up --build

down:
	docker compose down -v

security:
	pip-audit --strict
	@echo "Run 'make sbom' to produce a CycloneDX SBOM"

sbom:
	cyclonedx-py environment -o sbom.json && echo "wrote sbom.json"

# Everything CI runs, locally.
all: lint test evals benchmark memory-ab
