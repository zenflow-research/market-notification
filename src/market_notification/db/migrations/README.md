# Alembic migrations

Initial schema bootstrap goes through `scripts/bootstrap_db.py` (calls
`Base.metadata.create_all`). This is fine for first-time setup.

For **all subsequent schema changes**, use Alembic so we have a versioned,
forward+backward upgrade trail:

```powershell
# Create a new revision after editing models.py
alembic revision --autogenerate -m "add column X to notifications"

# Apply pending migrations
alembic upgrade head

# Roll back one
alembic downgrade -1

# View status
alembic current
alembic history
```

`env.py` reads the DB URL from `Settings`, so no need to touch `alembic.ini`.

## Conventions

- One revision per logical change. Don't bundle unrelated schema edits.
- Always test the downgrade path locally before committing.
- After applying, run the project test suite to catch downstream breakage.
- Update `PLAN.md` §5 (schema) AND `design-decisions.md` §P (in-build decisions)
  for any column add/remove/rename.
