PY ?= uv run python
# An x86_64 make (Intel Homebrew under Rosetta) passes x86_64 to swiftc, which the arm64-only Command Line Tools can't load
NATIVE := $(shell [ "$$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ] && echo arch -arm64)
SWIFTC = DEVELOPER_DIR=/Library/Developer/CommandLineTools $(NATIVE) swiftc

.PHONY: help test test-py test-js typecheck build report sync dev-status

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-11s %s\n", $$1, $$2}'

test: test-py test-js typecheck ## test-py, test-js and typecheck

test-py: ## Python tests (what CI runs)
	@fail=0; for t in tests/test_*.py; do $(PY) "$$t" || { echo "FAIL $$t"; fail=1; }; done; \
	uv run --with pytest python -m pytest -q tests/test_chat.py || fail=1; exit $$fail

test-js: ## page tests (need playwright-core and Chrome)
	@fail=0; for t in tests/test_*.cjs; do node "$$t" || { echo "FAIL $$t"; fail=1; }; done; exit $$fail

typecheck: ## compile-check Greenlight.swift (CI doesn't)
	$(SWIFTC) -typecheck app/Greenlight.swift

build: ## build and install ~/Applications/Greenlight.app
	$(NATIVE) app/build.sh

report: ## scan and write ~/.greenlight/report.html
	$(PY) scripts/greenlight.py report

sync: ## copy this checkout to GREENLIGHT_HOST
	scripts/sync.sh

dev-status: ## dev-copy agents and apps
	$(PY) ~/.greenlight-dev/dev.py status
