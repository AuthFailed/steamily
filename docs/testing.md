# Tests and coverage

Use Python 3.13 and install the test dependencies:

```sh
python -m pip install -r requirements-dev.txt
```

Set `TEST_DATABASE_URL` to a disposable Postgres database before running the full
suite. The storage and dialog tests **drop its public schema**. CI provides an
isolated Postgres 18 service. Without this variable those tests are skipped, so
that run does not validate the full suite or its coverage threshold.

```sh
export TEST_DATABASE_URL=postgresql://test:test@localhost:5432/test
python -m coverage run -m unittest -v
python -m coverage report
python -m coverage html --fail-under=0
python -m coverage xml --fail-under=0
```

Coverage measures statements and branches across all of `bot`, including modules
that were never imported. The combined minimum is 90%, configured in
`pyproject.toml`. CI publishes a Markdown job summary and a `coverage` artifact
containing `coverage.xml` and `htmlcov/`, including when a test or the coverage
gate fails. Open `htmlcov/index.html` to inspect uncovered paths locally.

Steam requests in tests use local responses and never require a Steam session or
Telegram token. Fixture provenance is documented alongside the fixtures. Real
recordings must be sanitized before committing: remove tokens, cookies, QR session
identifiers and personal data.

Issue #8 tracks shared test infrastructure. Behavioral regression tests for linked
backlog items belong with those implementations. The shared suite also runs the
regressions added by those issues, including inline store-search transport
recovery (#17) and per-family refresh serialization (#15).
