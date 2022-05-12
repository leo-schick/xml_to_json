.PHONY: all
all: test

.PHONY: clean
clean:
	find -name "__pycache__" | xargs rm -rf

.PHONY: build
build:
	.venv/bin/python3 -m build

.PHONY: test
test:
	.venv/bin/python3 -m unittest


INSTALL_HOME := /opt/xml_to_json

.PHONY: install
install:
	@echo "Install xml_to_json"
	@sudo mkdir -p "$(INSTALL_HOME)"
	@sudo python3 -m venv "$(INSTALL_HOME)/.venv"
	@sudo "$(INSTALL_HOME)/.venv/bin/pip" install --upgrade pip wheel
	@sudo "$(INSTALL_HOME)/.venv/bin/pip" install dist/xml_to_json-*.tar.gz
	@sudo ln -s "$(INSTALL_HOME)/.venv/bin/xml_to_json" /usr/bin/xml_to_json

.PHONY: remove
remove:
	@echo "Uninstall xml_to_json"
	@sudo rm -rf "$(INSTALL_HOME)"
	@sudo rm -f /usr/bin/xml_to_json
