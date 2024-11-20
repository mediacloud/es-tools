# to create development environment: `make`
# to run pre-commit linting/formatting: `make lint`

VENVDIR=venv
VENVBIN=$(VENVDIR)/bin
VENVDONE=$(VENVDIR)/.done

help:
	@echo Usage:
	@echo "make install -- installs pre-commit hooks"
	@echo "make lint -- runs pre-commit checks"
	@echo "make clean -- remove pre-commit tools"

## run pre-commit checks on all files
lint:	$(VENVDONE)
	$(VENVBIN)/pre-commit run --all-files

# create venv with project dependencies
# --editable skips installing project sources in venv
# pre-commit is in dev optional-requirements
$(VENVDONE): $(VENVDIR) Makefile pyproject.toml
	$(VENVBIN)/python3 -m pip install --editable '.[dev]'
	$(VENVBIN)/pre-commit install
	touch $(VENVDONE)

$(VENVDIR):
	python3 -m venv $(VENVDIR)

## update .pre-commit-config.yaml
update:	$(VENVDONE)
	$(VENVBIN)/pre-commit autoupdate

## clean up development environment
clean:
	-$(VENVBIN)/pre-commit clean
	rm -rf $(VENVDIR) build *.egg-info .pre-commit-run.sh.log \
		__pycache__ .mypy_cache