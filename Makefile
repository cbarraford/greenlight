PY ?= uv run --no-project python
# An x86_64 make (Intel Homebrew under Rosetta) passes x86_64 to swiftc, which the arm64-only Command Line Tools can't load
NATIVE := $(shell [ "$$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ] && echo arch -arm64)
SWIFTC = DEVELOPER_DIR=/Library/Developer/CommandLineTools $(NATIVE) swiftc
SWIFT_SRCS = $(wildcard app/Sources/*.swift)
SWIFT_TEST_SRCS = $(wildcard app/Tests/*.swift)

.PHONY: help test lint check-diff coverage coverage-py coverage-js coverage-swift coverage-all setup-hooks test-py test-js test-webkit test-swift test-ui bughunt typecheck build restart report sync dev-status

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

test: lint test-py test-js typecheck ## lint, test-py, test-js and typecheck

lint: ## ruff bug/security lint, static safety checks, JS static lint, and shellcheck
	ruff check scripts/ tests/ app/kokoro.py
	$(PY) scripts/check_safety.py
	$(PY) scripts/js_lint.py
	shellcheck -S warning app/build.sh scripts/*.sh .githooks/pre-commit .githooks/pre-push

check-diff: ## diff-scoped quality ratchet (stricter lint, mypy, function size, Swift evalJS)
	$(PY) scripts/check_diff_quality.py

coverage: coverage-py ## Python tests with combined coverage.xml report (>= 80% gate)

coverage-py: ## Python coverage with subprocess tracking and >= 85% total / >= 80% per-file floor
	$(PY) scripts/run_py_tests.py --coverage coverage.xml --fail-under 85 --min-file 80

coverage-js: ## Playwright Chromium V8 JS coverage for app/*.html (>= 80% floor)
	$(PY) scripts/js_coverage.py --fail-under 80 --min-file 80

coverage-swift: ## macOS Swift llvm-cov coverage (--unittest, --vimtest, --attachtest)
	$(PY) scripts/swift_coverage.py

coverage-all: ## Run Python, JS, and Swift coverage harnesses and print unified summary
	$(PY) scripts/coverage_summary.py

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

test-swift: ## compile and run offline Swift unit + vim tests (macOS)
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	$(SWIFTC) -Onone -DGREENLIGHT_TESTS $(SWIFT_SRCS) $(SWIFT_TEST_SRCS) -o "$$tmp/Greenlight-test" && \
	"$$tmp/Greenlight-test" --unittest && \
	"$$tmp/Greenlight-test" --vimtest

test-ui: ## parallel background macOS --uitest end-to-end suite
	$(PY) tests/test_uitest.py
	$(PY) tests/test_uitest_l1.py

bughunt: ## dry-run nightly bug hunter locally (fast Track A fuzz + ranking)
	$(PY) scripts/bughunt.py run --dry-run --skip-baseline --skip-llm --soak-secs 15 --fuzz-steps 25

typecheck: ## compile-check Swift app and test sources (strict concurrency, warnings as errors)
	$(SWIFTC) -typecheck -warnings-as-errors -strict-concurrency=targeted $(SWIFT_SRCS)
	$(SWIFTC) -typecheck -warnings-as-errors -strict-concurrency=targeted -DGREENLIGHT_TESTS $(SWIFT_SRCS) $(SWIFT_TEST_SRCS)

build: ## build and install ~/Applications/Greenlight.app
	$(NATIVE) app/build.sh

# -n on open: ssh ControlPersist muxes keep the exited app's LaunchServices ASN in (exited-with-subordinates), making plain `open` fail with -600
restart: ## quit and reopen ~/Applications/Greenlight.app
	@bin="$$HOME/Applications/Greenlight.app/Contents/MacOS/Greenlight"; \
	if pgrep -fx "$$bin" >/dev/null; then \
		osascript -e 'tell application id "dev.greenlight.app" to quit' >/dev/null 2>&1 || true; \
		for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25; do \
			pgrep -fx "$$bin" >/dev/null || break; \
			sleep 0.2; \
		done; \
		if pgrep -fx "$$bin" >/dev/null; then \
			pkill -TERM -fx "$$bin" 2>/dev/null || true; \
			for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -fx "$$bin" >/dev/null || break; sleep 0.2; done; \
		fi; \
		if pgrep -fx "$$bin" >/dev/null; then \
			pkill -KILL -fx "$$bin" 2>/dev/null || true; \
			for _ in 1 2 3 4 5; do pgrep -fx "$$bin" >/dev/null || break; sleep 0.2; done; \
		fi; \
	fi
	open -n "$$HOME/Applications/Greenlight.app"

report: ## scan and write ~/.greenlight/report.html
	$(PY) scripts/greenlight.py report

sync: ## copy this checkout to GREENLIGHT_HOST
	scripts/sync.sh

dev-status: ## dev-copy agents and apps
	$(PY) ~/.greenlight-dev/dev.py status
