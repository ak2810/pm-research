# Makefile for reverse-engineering project
# All targets run locally — never on EC2

PYTHON := C:/Users/avych/AppData/Local/Programs/Python/Python312/python.exe
SRC    := src
TESTS  := tests

.PHONY: help install precommit lint typecheck test sync gpu-check \
        phase1 phase2 phase3 phase4 phase5 phase6 phase7

help:
	@echo "Targets:"
	@echo "  install     Install package + dependencies"
	@echo "  precommit   Run all pre-commit gates (mypy + ruff + pytest)"
	@echo "  lint        Run ruff check + format --check"
	@echo "  typecheck   Run mypy --strict"
	@echo "  test        Run pytest"
	@echo "  sync        Sync S3 Parquet to local cache (one partition per feed)"
	@echo "  gpu-check   Verify CUDA is available and RTX 3060 detected"
	@echo "  phase1      Run Phase 1 data validation pipeline"
	@echo "  phase2      Run Phase 2 maker/taker decomposition"
	@echo "  phase3      Run Phase 3 order lifecycle reconstruction"
	@echo "  phase4      Run Phase 4 fair value modeling (Layers 1+2)"
	@echo "  phase5      Run Phase 5 pricing adjustments (Layer 3)"
	@echo "  phase6      Run Phase 6 microstructure alpha (Layers 4+5)"
	@echo "  phase7      Run Phase 7 replication + validation (Layers 6+7)"

install:
	$(PYTHON) -m pip install -e .
	$(PYTHON) -m pip install -r requirements.txt

precommit: typecheck lint test
	@echo "All pre-commit gates passed."

typecheck:
	$(PYTHON) -m mypy --strict $(SRC)/

lint:
	$(PYTHON) -m ruff check $(SRC)/ $(TESTS)/
	$(PYTHON) -m ruff format --check $(SRC)/ $(TESTS)/

test:
	$(PYTHON) -m pytest -x $(TESTS)/

sync:
	$(PYTHON) -m reverse_engineering.cli sync --partitions 1

gpu-check:
	$(PYTHON) -c "import torch; ok=torch.cuda.is_available(); dev=torch.cuda.get_device_name(0) if ok else 'NONE'; print(f'CUDA={ok} device={dev}'); assert ok, 'CUDA not available — see BLOCKERS.md'"

phase1:
	$(PYTHON) -m reverse_engineering.cli phase1

phase2:
	$(PYTHON) -m reverse_engineering.cli phase2

phase3:
	$(PYTHON) -m reverse_engineering.cli phase3

phase4:
	$(PYTHON) -m reverse_engineering.cli phase4

phase5:
	$(PYTHON) -m reverse_engineering.cli phase5

phase6:
	$(PYTHON) -m reverse_engineering.cli phase6

phase7:
	$(PYTHON) -m reverse_engineering.cli phase7
