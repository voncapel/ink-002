.PHONY: install-dev run test check

install-dev:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

run:
	.venv/bin/flask --app app run --port 8092

test:
	.venv/bin/pytest -q

check: test
	node --check static/app.js
