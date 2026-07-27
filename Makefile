UV ?= uv
RELEASE_RECORD ?= .vibe-loop/release-readiness.json

.PHONY: build bump-major bump-minor bump-patch check doc-budget doc-budget-refresh install-hooks project-binding-linkage-trial release-gate tag test unittest version version-check

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

check: doc-budget test build

project-binding-linkage-trial:
	$(UV) run python scripts/check-project-binding-doc-linkage.py

doc-budget:
	$(UV) run python scripts/check-doc-budgets.py --config doc-budgets.toml $(DOC_BUDGET_ARGS)
	$(UV) run python scripts/check-md-links.py $(DOC_BUDGET_ARGS)

doc-budget-refresh:
	$(UV) run python scripts/check-doc-budgets.py --config doc-budgets.toml --update-baselines

release-gate: doc-budget
	$(UV) run vibe-loop eval release-gate --repo . --overwrite \
	  --record-output $(RELEASE_RECORD)

install-hooks:
	@hooks_dir="$$(git rev-parse --git-common-dir)/hooks"; \
	mkdir -p "$$hooks_dir"; \
	for hook in pre-commit pre-push prepare-commit-msg commit-msg; do \
	  hook_path="$$hooks_dir/$$hook"; \
	  if [ -f "$$hook_path" ] && ! grep -q "scripts/hooks/$$hook" "$$hook_path"; then \
	    if [ "$$hook" = prepare-commit-msg ] || [ "$$hook" = commit-msg ]; then \
	      if [ -x "$$hook_path" ] && grep -q 'VIBE_LOOP_TASK_ID' "$$hook_path" && grep -q 'Plan-Item:' "$$hook_path" && grep -q 'interpret-trailers' "$$hook_path"; then \
	        echo "$$hook_path is a compatible unmanaged provenance hook; keeping it"; \
	        continue; \
	      fi; \
	    fi; \
	    echo "$$hook_path already exists and is not managed by this repo; move or remove it, then rerun make install-hooks" >&2; \
	    exit 1; \
	  fi; \
	  if [ "$$hook" = prepare-commit-msg ] || [ "$$hook" = commit-msg ]; then \
	    printf '%s\n' '#!/bin/sh' 'repo_root=$$(git rev-parse --show-toplevel)' "hook_path=\"\$$repo_root/scripts/hooks/$$hook\"" '[ -x "$$hook_path" ] || exit 0' 'exec "$$hook_path" "$$@"' > "$$hook_path"; \
	  else \
	    printf '%s\n' '#!/bin/sh' 'repo_root=$$(git rev-parse --show-toplevel)' "exec \"\$$repo_root/scripts/hooks/$$hook\" \"\$$@\"" > "$$hook_path"; \
	  fi; \
	  chmod +x "$$hook_path"; \
	  echo "installed $$hook_path"; \
	done

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
