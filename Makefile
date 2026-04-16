PYTHON ?= python3

.PHONY: simulate discover issues launch poll report

simulate: discover issues poll report

discover:
	$(PYTHON) scripts/scan_or_import_findings.py

issues:
	$(PYTHON) scripts/create_issues.py

launch:
	@test -n "$(ISSUE_NUMBER)" || (echo "ISSUE_NUMBER is required"; exit 1)
	$(PYTHON) scripts/launch_devin_session.py --issue-number $(ISSUE_NUMBER)

poll:
	$(PYTHON) scripts/poll_devin_sessions.py

report:
	$(PYTHON) scripts/render_metrics.py
