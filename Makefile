.PHONY: test capture capture-keep capture-all capture-all-keep browse

ISSUE ?= fix_math_utils
OPENCODE_BIN ?= opencode
BROWSER_HOST ?= 127.0.0.1
BROWSER_PORT ?= 8765

test:
	python3 -m unittest discover -s tests -p 'test_*.py'

capture:
	./scripts/run_opencode_capture.sh --issue "$(ISSUE)" --opencode-bin "$(OPENCODE_BIN)"

capture-keep:
	./scripts/run_opencode_capture.sh --issue "$(ISSUE)" --keep-workspace --opencode-bin "$(OPENCODE_BIN)"

capture-all:
	./scripts/run_all_captures.sh --opencode-bin "$(OPENCODE_BIN)"

capture-all-keep:
	./scripts/run_all_captures.sh --keep-workspace --opencode-bin "$(OPENCODE_BIN)"

browse:
	./scripts/run_capture_browser.sh --host "$(BROWSER_HOST)" --port "$(BROWSER_PORT)"
