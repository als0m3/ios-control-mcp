# Contributing

Use English for code, documentation, issues, and commit messages. Keep changes
focused and explain the problem, the resulting behavior, and how it was checked.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

## Required checks

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pytest
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
.venv/bin/python -m pip_audit --local
.venv/bin/python scripts/check_secrets.py
```

The secret check scans tracked files and reachable Git history without printing
matched values or verifying possible credentials against external services.
Dependency auditing queries advisory databases with package names and versions.
A successful scan is evidence, not proof that no vulnerability or private data
exists. Inspect staged changes and author metadata before committing.

Tests mock device services and must not operate a connected phone. New device
behavior should have targeted regression coverage, plus the manual checks in
[docs/VALIDATION.md](docs/VALIDATION.md). Do not claim hardware validation unless
it was actually performed; record OS and dependency versions without a UDID or
device name.

## Pull requests

- Use an English title such as `fix: prevent fallback to an unintended device`.
- Include the purpose of the change and relevant test results.
- Update documentation when tool arguments, defaults, or behavior change.
- Keep screenshots, logs, pairing records, local configurations, and credentials
  out of commits. Use synthetic test data.
- Preserve stdout for MCP messages. Do not include private values in errors.
- Do not automatically retry operations that can change the phone's state.

## Release preparation

1. Run the required checks in a fresh environment.
2. Run the hardware checklist for the intended device and transport combinations.
3. Review the wheel and source archive contents, dependency licenses, and the full
   reachable history, including commit author and committer addresses.
4. Update the version in `pyproject.toml`, `__init__.py`, and `server.py` together,
   and record changes in `CHANGELOG.md`.
5. Confirm GitHub CI is green and private vulnerability reporting is enabled
   before making the repository public or tagging a release.

Publishing a package or changing repository visibility is a separate release
step. This repository does not publish artifacts automatically.
