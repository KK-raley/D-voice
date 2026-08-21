.PHONY: install dev test lint fmt serve hud clean

install:            ## Install all extras + dev tooling
	pip install -e ".[all,dev]"

dev:                ## Watch mode: pytest on change (requires ptw)
	pytest-watch -q

test:               ## Run the offline test suite
	pytest -q

lint:               ## Ruff lint
	ruff check vocalis tests examples

fmt:                ## Ruff auto-fix + import sort
	ruff check --fix vocalis tests examples

serve:              ## Start backend on :8642
	python -m uvicorn vocalis.server.app:app --reload --port 8642

hud:                ## Start HUD dev server on :5173
	cd ui && npm run dev

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} +
