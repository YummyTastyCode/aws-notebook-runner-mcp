# Contributing

Contributions are welcome, especially around safer AWS policies, better
observability, additional notebook backends, and documentation.

## Local Setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[aws,test]"
.venv/bin/python -m pytest
```

## Guidelines

- Keep paid-compute execution guarded by environment policy and confirmation
  tokens.
- Do not add code that broadens IAM permissions automatically.
- Do not commit credentials, account IDs, private bucket names, run manifests,
  notebook outputs, or local research notes.
- Prefer dry-run tools and explicit plans for risky operations.
- Add focused tests for guardrails and policy validation.

## Release Checklist

```bash
find . -maxdepth 1 \( -name dist -o -name build -o -name "*.egg-info" \) -exec rm -rf {} +
.venv/bin/python -m pytest -q
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```
