.PHONY: install test lint fmt evals evals-smoke generated-check gate perf perf-report load benchmark memory-ab demo run docker up down clean security sbom runtime-lock all

PYTHON ?= python3

install:
	$(PYTHON) -m pip install --require-hashes -r scripts/requirements-runtime.txt
	$(PYTHON) -m pip install --no-deps -e .
	$(PYTHON) -m pip install "pytest>=8.0" "pytest-asyncio>=0.23" "ruff>=0.15.17,<0.16" \
		"pip-audit>=2.7" "uv>=0.8"

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src tests perf benchmarks evals scripts
	$(PYTHON) -m ruff format --check src tests perf benchmarks evals scripts

fmt:
	$(PYTHON) -m ruff check --fix src tests perf benchmarks evals scripts
	$(PYTHON) -m ruff format src tests perf benchmarks evals scripts

evals:
	$(PYTHON) -m evals.gate --tier standard

evals-smoke:
	$(PYTHON) -m evals.gate --tier smoke

perf:
	$(PYTHON) -m perf.benchmark --concurrency 6 --iterations 12

perf-report:
	$(PYTHON) -m perf.benchmark --concurrency 6 --iterations 12 --write

load:
	@echo "Against a DEPLOYED instance (not CI):"
	@echo "  locust -f perf/locustfile.py --host http://localhost:8000"

generated-check: benchmark memory-ab evals
	git diff --exit-code -- benchmarks/RESULTS.md docs/MEMORY.md evals/last_run.md

gate: lint test evals generated-check perf

benchmark:
	$(PYTHON) -m benchmarks.run_benchmark

memory-ab:
	$(PYTHON) -m benchmarks.memory_ab

demo:
	$(PYTHON) scripts/demo.py

run:
	$(PYTHON) -m uvicorn atlas.api.app:app --reload

docker:
	docker build -t atlas:local .

up:
	docker compose up --build

down:
	docker compose down

clean:
	docker compose down -v

security:
	$(PYTHON) -m pip_audit --strict --requirement scripts/requirements-runtime.txt
	@echo "This audits the locked app dependencies; CI additionally audits the built image."

sbom:
	$(PYTHON) scripts/generate_runtime_sbom.py > sbom.json
	@echo "wrote sbom.json for the current environment (CI generates it from the image)"

runtime-lock:
	$(PYTHON) scripts/update_runtime_lock.py

# Deterministic software gates run by CI. Image and supply-chain jobs additionally
# require Docker and pip-audit and remain available as `make docker security`.
all: lint test evals generated-check perf
