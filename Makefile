.DEFAULT_GOAL := all

.PHONY: install lint format typecheck test test-ci test-release build clean all docs docs-serve

install:
	uv sync --group dev

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy src/

test:
	uv run pytest --cov --cov-report=term-missing
	$(MAKE) test-release

test-ci:
	uv run pytest --cov --cov-report=xml --junitxml=junit.xml
	$(MAKE) test-release

# Builds distributions and runs the release scripts; deselected by default.
test-release:
	uv run pytest -m release

build:
	uv build

clean:
	rm -rf dist/ build/ .mypy_cache/ .pytest_cache/ .ruff_cache/ .coverage htmlcov/ site/ junit.xml coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +

docs:
	uv run mkdocs build --strict

docs-serve:
	uv run mkdocs serve

all: lint typecheck test build
