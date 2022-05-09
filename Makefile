.PHONY: all
all: test

.PHONY: clean
clean:
	find -name "__pycache__" | xargs rm -rf

.PHONY: build
build:
	python3 -m build

.PHONY: test
test:
	python3 -m unittest
