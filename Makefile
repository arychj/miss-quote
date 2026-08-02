# Tests run inside the image rather than against whatever interpreter happens to
# be on the machine. The dependency set is pinned there, one of them is a system
# library rather than a wheel, and CI and a laptop then run the same thing.

DOCKER ?= docker

# The validator needs PyYAML, so prefer the checkout's virtualenv over whatever
# python3 is first on PATH. Overridable for a machine that installed it another
# way, and used by nothing else here.
PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

IMAGE ?= miss-quote
RUNTIME_TAG ?= local
TEST_TAG ?= test
TEST_STAGE := test

RUNTIME_IMAGE := $(IMAGE):$(RUNTIME_TAG)
TEST_IMAGE := $(IMAGE):$(TEST_TAG)

# Overridable so that a narrower run does not need a different target:
#   make test PYTEST_ARGS="-k config -vv"
PYTEST_ARGS ?= -q

VALIDATOR := scripts/validate_quotes.py
QUOTES_FILE ?= src/miss_quote/resources/quotes.yaml
CONFIG_FILE ?= config.yaml

.DEFAULT_GOAL := help

.PHONY: help build test test-image shell validate-quotes clean

help: ## List the available targets
	@printf 'Targets:\n'
	@grep -hE '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN { FS = ":.*## " } { printf "  %-16s %s\n", $$1, $$2 }'

build: ## Build the image that gets published
	$(DOCKER) build --tag $(RUNTIME_IMAGE) .

test-image: ## Build the image the tests run in
	$(DOCKER) build --target $(TEST_STAGE) --tag $(TEST_IMAGE) .

test: test-image ## Run the test suite in the container
	$(DOCKER) run --rm $(TEST_IMAGE) $(PYTEST_ARGS)

shell: test-image ## Open a shell in the test image
	$(DOCKER) run --rm -it --entrypoint bash $(TEST_IMAGE)

# Not containerized, on purpose. The validator needs PyYAML and nothing else, so
# there is no image to build, and the workflow that calls it exists to answer a
# quote-list change in seconds rather than after one.
#
# Both lists, because both reach a channel: the deployment's file, and whatever
# a server added for itself under `additional_quotes` in the config.
validate-quotes: ## Check the quote file and the config's additions against the validator
	$(PYTHON) $(VALIDATOR) "$(QUOTES_FILE)" --config "$(CONFIG_FILE)"

clean: ## Remove the images this Makefile builds
	-$(DOCKER) image rm $(TEST_IMAGE) $(RUNTIME_IMAGE)
