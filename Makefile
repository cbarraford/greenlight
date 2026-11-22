PY ?= uv run --no-project python
# An x86_64 make (Intel Homebrew under Rosetta) passes x86_64 to swiftc, which the arm64-only Command Line Tools can't load
NATIVE := $(shell [ "$$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ] && echo arch -arm64)
SWIFTC = DEVELOPER_DIR=/Library/Developer/CommandLineTools $(NATIVE) swiftc
SWIFT_SRCS = $(wildcard app/Sources/*.swift)

.PHONY: help test lint check-diff coverage setup-hooks test-py test-js test-webkit test-ui typecheck build report sync dev-status

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-13s %s\n", $$1, $$2}'

test: lint test-py test-js typecheck ## lint, test-py, test-js and typecheck

lint: ## ruff bug/security lint, static safety checks, and shellcheck
	ruff check scripts/ tests/ app/kokoro.py
	$(PY) scripts/check_safety.py
	shellcheck -S warning app/build.sh scripts/*.sh

check-diff: ## diff-scoped quality ratchet (stricter lint, mypy, function size, Swift evalJS)
	$(PY) scripts/check_diff_quality.py

coverage: ## Python tests with combined coverage.xml report
	$(PY) scripts/run_py_tests.py --coverage coverage.xml

setup-hooks: ## install versioned .githooks for local pre-commit/pre-push checks
	chmod +x .githooks/pre-commit .githooks/pre-push
	git config core.hooksPath .githooks
	@echo "ok  git hooks enabled (.githooks)"

test-py: ## Python tests (what CI runs; auto-routes pytest vs script suites)
	$(PY) scripts/run_py_tests.py

test-js: ## page tests on Chromium (need playwright-core)
	@fail=0; for t in tests/test_*.cjs; do node "$$t" || { echo "FAIL $$t"; fail=1; }; done; exit $$fail

test-webkit: ## page tests on WebKit (matches macOS WKWebView)
	@fail=0; for t in tests/test_*.cjs; do BROWSER=webkit node "$$t" || { echo "FAIL $$t"; fail=1; }; done; exit $$fail

test-ui: ## parallel background macOS --uitest end-to-end suite
	$(PY) tests/test_uitest.py

typecheck: ## compile-check Swift app sources (strict concurrency, warnings as errors)
	$(SWIFTC) -typecheck -warnings-as-errors -strict-concurrency=targeted $(SWIFT_SRCS)

build: ## build and install ~/Applications/Greenlight.app
	$(NATIVE) app/build.sh

report: ## scan and write ~/.greenlight/report.html
	$(PY) scripts/greenlight.py report

sync: ## copy this checkout to GREENLIGHT_HOST
	scripts/sync.sh

dev-status: ## dev-copy agents and apps
	$(PY) ~/.greenlight-dev/dev.py status
