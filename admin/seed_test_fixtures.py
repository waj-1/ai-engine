"""Placeholder seeder for `public.test_fixtures`.

Unlike the other seed scripts in this directory, this one deliberately
does *not* insert any rows. Test fixtures are real secrets (live
Supabase JWTs, real tenant UUIDs from operator-controlled test
businesses) and must never be committed to git or shipped in seed data.

Instead, this file documents the conventional fixture names the test
suite and eval runner expect. The operator fills them in once per
environment at:

    /admin/test-fixtures

…then downloads `.env.test` and sources it before running pytest:

    set -a && source .env.test && pytest

Conventional names (kind in parentheses — match these to get the right
UI affordances):

    business_jwt         (jwt)     Supabase access token for a business-owner test user
    test_business_id     (uuid)    business.id for the test tenant
    test_branch_id       (uuid)    branch.id under test_business_id

If a test references a fixture not yet recorded, it skips with a clear
message pointing back at /admin/test-fixtures — see the `test_fixture`
fixture in `ai_agent/tests/conftest.py`.
"""

# Intentionally no `if __name__ == "__main__"` block — there's nothing
# to seed. Keeping the module importable so `python -m admin.seed_test_fixtures`
# doesn't error if someone wires it into a future bulk-seed runner.
