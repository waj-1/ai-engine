# Contributing

Thanks for helping. Bug reports, fixes, docs and new providers are all welcome.

## Before you start

- For anything larger than a small fix, open an issue first so we can agree on
  the approach before you write code.
- Security problems go through [SECURITY.md](SECURITY.md), not a public issue.

## Set up

```bash
python3.11 -m venv venv
./venv/bin/pip install -r requirements.lock
docker compose up -d db rest gateway   # the database the tests and the app use
./venv/bin/python -m pytest tests/ -q
```

Python 3.11 is required; it is what the published image runs.

## Making a change

- Keep a pull request to one change. Small pull requests are reviewed sooner.
- Add or update tests. For a bug fix, the test should fail without your fix:
  put the bug back, see the test fail, then restore the fix.
- To change a dependency, edit `requirements.txt`, run `make lock`, and commit
  both files.
- If you change a portal screen, a route or a table, update the matching page
  under `docs/`.
- Prompts that ship in two languages must be changed in both.

## Pull requests

Describe what changed and why, and how you tested it. A maintainer will review
it; please allow a few days.

By contributing you agree that your contribution is licensed under the
Apache License 2.0, the license of this project.
