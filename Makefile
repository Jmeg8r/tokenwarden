.PHONY: install install-forecast test serve status forecast smoke

PY := ./.venv/bin/python
BIN := ./.venv/bin/tokenwarden

install:                ## create the venv (Python 3.13) and install with dev deps
	python3.13 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"

install-forecast:       ## add the optional forecasting extra (torch + timesfm)
	./.venv/bin/pip install -e ".[dev,forecast]"

test:                   ## run the test suite
	$(PY) -m pytest -q

serve:                  ## run the metering gateway
	$(BIN) serve

status:                 ## show today's estimated spend by agent
	$(BIN) status

forecast:               ## project end-of-day spend and flag likely overruns
	$(BIN) forecast

smoke:                  ## live Part-A dogfood (needs ANTHROPIC_API_KEY)
	./scripts/smoke.sh
