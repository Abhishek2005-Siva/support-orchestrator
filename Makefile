PY=.venv/bin/python
.PHONY: setup seed test live-test eval eval-mock load run report guardrails
setup:      ; python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
seed:       ; $(PY) -m app.db.seed && $(PY) evals/build_golden.py
test:       ; .venv/bin/pytest -q                       # offline: unit + guardrail + graph(mock) + API + Slack
live-test:  ; .venv/bin/pytest -m live -v               # real NVIDIA calls: per-agent acceptance tests
eval:       ; $(PY) evals/run_eval.py --name full --judge   # full live evaluation -> reports/04_eval_full.md
eval-mock:  ; $(PY) evals/run_eval.py --mock --name mock
guardrails: ; $(PY) evals/guardrail_report.py           # add --llm for the gray-zone classifier
load:       ; $(PY) loadtests/traffic_sim.py            # -> reports/06_load_test.md
run:        ; .venv/bin/uvicorn app.main:app --port 8000
report:     ; $(PY) scripts/report.py --dir logs        # observability aggregate from local traces
