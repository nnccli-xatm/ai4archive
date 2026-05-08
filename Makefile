.PHONY: test compile validate-release

test:
	PYTHONPATH=src python3 -m unittest discover -s tests

compile:
	PYTHONPATH=src python3 -m compileall -q src tests

validate-release:
	python3 scripts/validate_release.py
