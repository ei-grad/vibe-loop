UV ?= uv
RELEASE_RECORD ?= .vibe-loop/release-readiness.json

.PHONY: build bump-major bump-minor bump-patch check install-hooks release-gate tag test unittest version version-check

version:
	$(UV) version --short

bump-patch:
	$(UV) version --bump patch

bump-minor:
	$(UV) version --bump minor

bump-major:
	$(UV) version --bump major

test:
	$(UV) run -m pytest tests

unittest:
	$(UV) run python -m unittest discover

build:
	$(UV) build
	$(UV) run --with twine --no-project -m twine check dist/*

check: test build

release-gate:
	$(UV) run vibe-loop eval release-gate --repo . --overwrite \
	  --record-output $(RELEASE_RECORD)

install-hooks:
	@mkdir -p .git/hooks
	@if [ -f .git/hooks/pre-push ] && ! grep -q 'scripts/hooks/pre-push' .git/hooks/pre-push; then \
	  echo ".git/hooks/pre-push already exists and is not managed by this repo" >&2; \
	  exit 1; \
	fi
	@printf '%s\n' '#!/bin/sh' 'repo_root=$$(git rev-parse --show-toplevel)' 'exec "$$repo_root/scripts/hooks/pre-push" "$$@"' > .git/hooks/pre-push
	@chmod +x .git/hooks/pre-push
	@echo "installed .git/hooks/pre-push"

version-check:
	@version="$(VERSION)"; \
	if [ -z "$$version" ]; then version="$$($(UV) version --short)"; fi; \
	if [ -n "$$(git status --short)" ]; then \
	  git status --short; \
	  echo "working tree must be clean before tagging" >&2; \
	  exit 1; \
	fi; \
	head="$$(git rev-parse --verify HEAD)"; \
	printf 'refs/tags/v%s %s refs/tags/v%s 0000000000000000000000000000000000000000\n' "$$version" "$$head" "$$version" | scripts/hooks/pre-push

tag: version-check
	@version="$(VERSION)"; \
	if [ -z "$$version" ]; then version="$$($(UV) version --short)"; fi; \
	git tag "v$$version"
