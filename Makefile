PY ?= uv run python
# An x86_64 make (Intel Homebrew under Rosetta) passes x86_64 to swiftc, which the arm64-only Command Line Tools can't load
NATIVE := $(shell [ "$$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ] && echo arch -arm64)
SWIFTC = DEVELOPER_DIR=/Library/Developer/CommandLineTools $(NATIVE) swiftc
SWIFT_SRCS = $(wildcard app/Sources/*.swift)

.PHONY: help test lint test-py test-js test-webkit test-ui typecheck build report sync dev-status

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-13s %s\n", $$1, $$2}'

test: lint test-py test-js typecheck ## lint, test-py, test-js and typecheck

lint: ## ruff bug/security lint and static safety checks
	ruff check scripts/ tests/
	$(PY) scripts/check_safety.py

test-py: ## Python tests (what CI runs)
	@fail=0; for t in tests/test_*.py; do [ "$$(basename "$$t")" = "test_uitest.py" ] && continue; $(PY) "$$t" || { echo "FAIL $$t"; fail=1; }; done; \
	uv run --with pytest python -m pytest -q tests/test_chat.py || fail=1; exit $$fail

test-js: ## page tests on Chromium (need playwright-core)
	@fail=0; for t in tests/test_*.cjs; do node "$$t" || { echo "FAIL $$t"; fail=1; }; done; exit $$fail

test-webkit: ## page tests on WebKit (matches macOS WKWebView)
	@fail=0; for t in tests/test_*.cjs; do BROWSER=webkit node "$$t" || { echo "FAIL $$t"; fail=1; }; done; exit $$fail

test-ui: ## parallel background macOS --uitest end-to-end suite
	$(PY) tests/test_uitest.py

typecheck: ## compile-check Swift app sources
	$(SWIFTC) -typecheck $(SWIFT_SRCS)

build: ## build and install ~/Applications/Greenlight.app
	$(NATIVE) app/build.sh

report: ## scan and write ~/.greenlight/report.html
	$(PY) scripts/greenlight.py report

sync: ## copy this checkout to GREENLIGHT_HOST
	scripts/sync.sh

dev-status: ## dev-copy agents and apps
	$(PY) ~/.greenlight-dev/dev.py status
