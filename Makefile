.PHONY: develop test lint fmt

develop:
	pip install -e .

test:
	cargo test --workspace
	pytest

lint:
	black --check python/beanpicker tests
	flake8 python/beanpicker tests
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings

fmt:
	black python/beanpicker tests
	cargo fmt --all
