.PHONY: develop test lint fmt

develop:
	pip install -e .

test:
	cargo test --workspace
	pytest

lint:
	black --check python/transitio tests
	flake8 python/transitio tests
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings

fmt:
	black python/transitio tests
	cargo fmt --all
