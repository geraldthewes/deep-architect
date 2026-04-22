install:
    uv sync
    uv pip install -e .

sync:
    uv sync

lint:
    uv run ruff check deep_architect/ tests/

typecheck:
    uv run mypy deep_architect/

test:
    uv run python -m pytest tests/ -v

security:
    uv run bandit -r deep_architect/ -ll

check: lint typecheck test security
