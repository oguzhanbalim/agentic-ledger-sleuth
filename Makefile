PY ?= .venv/bin/python
TRAIN_SEED ?= 1729
HELDOUT_SEED ?= 271828

.PHONY: venv generate eval-baseline eval-agent investigate mcp test lint

venv:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"

generate:
	$(PY) -m ledger_sleuth.cli generate --seed $(TRAIN_SEED)
	$(PY) -m ledger_sleuth.cli generate --seed $(HELDOUT_SEED)

eval-baseline:
	$(PY) -m ledger_sleuth.cli eval-baseline --seeds $(TRAIN_SEED) $(HELDOUT_SEED)

# Needs ANTHROPIC_API_KEY. Spends real tokens; MAX_CLUSTERS bounds the run.
MAX_CLUSTERS ?= 25
eval-agent:
	$(PY) -m ledger_sleuth.cli eval-agent --seed $(HELDOUT_SEED) --max-clusters $(MAX_CLUSTERS)

investigate:
	$(PY) -m ledger_sleuth.cli investigate --seed $(TRAIN_SEED) --max-clusters $(MAX_CLUSTERS)

mcp:
	$(PY) -m ledger_sleuth.cli mcp --seed $(TRAIN_SEED)

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy
