PYTHON ?= python3

.PHONY: discover-devin deploy-aws test invoke-manual invoke-linear

discover-devin:
	$(PYTHON) scripts/run_devin_discovery.py

deploy-aws:
	bash infra/deploy_aws.sh

test:
	$(PYTHON) -m unittest discover -s tests -p "test_*.py"

invoke-manual:
	curl -sS -X POST "$$INTAKE_URL/manual" -H "Content-Type: application/json" --data @fixtures/manual.sample.json

invoke-linear:
	curl -sS -X POST "$$INTAKE_URL/linear" -H "Content-Type: application/json" --data @fixtures/linear.sample.json
