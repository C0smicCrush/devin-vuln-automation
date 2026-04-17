PYTHON ?= python3

.PHONY: simulate discover discover-devin issues launch poll report deploy-aws test invoke-manual invoke-linear

simulate: discover issues poll report

discover:
	$(PYTHON) scripts/scan_or_import_findings.py

discover-devin:
	$(PYTHON) scripts/run_devin_discovery.py

issues:
	$(PYTHON) scripts/create_issues.py

launch:
	@test -n "$(ISSUE_NUMBER)" || (echo "ISSUE_NUMBER is required"; exit 1)
	$(PYTHON) scripts/launch_devin_session.py --issue-number $(ISSUE_NUMBER)

poll:
	$(PYTHON) scripts/poll_devin_sessions.py

report:
	$(PYTHON) scripts/render_metrics.py

deploy-aws:
	bash infra/deploy_aws.sh

test:
	$(PYTHON) -m unittest discover -s tests -p "test_*.py"

invoke-manual:
	curl -sS -X POST "$$INTAKE_URL/manual" -H "Content-Type: application/json" --data @fixtures/manual.sample.json

invoke-linear:
	curl -sS -X POST "$$INTAKE_URL/linear" -H "Content-Type: application/json" --data @fixtures/linear.sample.json
