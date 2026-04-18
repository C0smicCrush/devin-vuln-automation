PYTHON ?= python3

.PHONY: discover-devin deploy-aws terraform-build terraform-render terraform-import test invoke-manual invoke-linear

discover-devin:
	$(PYTHON) scripts/run_devin_discovery.py

deploy-aws:
	bash infra/deploy_aws.sh

terraform-build:
	bash terraform/scripts/build_lambda_bundle.sh "$(CURDIR)" "$(CURDIR)/build/devin-vuln-automation.zip"

terraform-render:
	$(PYTHON) terraform/scripts/render_live_tfvars.py > terraform/live.auto.tfvars.json

terraform-import:
	bash terraform/scripts/import_live_stack.sh

test:
	$(PYTHON) -m unittest discover -s tests -p "test_*.py"

invoke-manual:
	curl -sS -X POST "$$INTAKE_URL/manual" -H "Content-Type: application/json" --data @fixtures/manual.sample.json

invoke-linear:
	curl -sS -X POST "$$INTAKE_URL/linear" -H "Content-Type: application/json" --data @fixtures/linear.sample.json
