.PHONY: install test lint fmt evals gate perf perf-report load benchmark memory-ab demo run docker up down security sbom all

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

perf:
	python -m perf.benchmark --concurrency 6 --iterations 12

perf-report:
	python -m perf.benchmark --concurrency 6 --iterations 12 --write

load:
	@echo "Against a DEPLOYED instance (not CI):"
	@echo "  locust -f perf/locustfile.py --host http://localhost:8000"

gate: lint test evals perf

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
