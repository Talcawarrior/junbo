# JunboBot dev tooling. Run `make check` to lint, format, type-check and test.
# (pre-commit handles ruff + ruff-format + mypy automatically on every commit;
#  pytest lives here, not in pre-commit, because it is slow and needs the DB.)

.PHONY: install-hooks lint format type test check

install-hooks:
	pre-commit install

lint:
	ruff check . --fix

format:
	ruff format .

type:
	mypy . --config-file=mypy.ini --exclude=scripts/

test:
	python -m pytest -q

check: lint format type test
