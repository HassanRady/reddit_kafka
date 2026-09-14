# Database Migrations

This directory contains SQL migration files for the PostgreSQL database schema.

## Running Migrations

### Option 1: Using the Migration Runner (Recommended for Development)

```bash
python -m src.migrations
```

This applies pending `.sql` files in numerical order. Applied filenames and
checksums are recorded in the `schema_migrations` table.

### Option 2: Using psql (Manual)

```bash
psql -h localhost -U local -d reddit_stream < migrations/001_initial_schema.sql
```

### Option 3: Using Docker

```bash
# If using docker-compose:
docker-compose exec postgres psql -U local -d reddit_stream < migrations/001_initial_schema.sql
```

## Migration Files

- `001_initial_schema.sql` - Creates base tables: `streams`, `stream_checkpoints`, `stream_errors`

## Adding New Migrations

1. Create a new file: `NNNN_description.sql` (e.g., `002_add_retry_table.sql`)
2. Write the SQL statements
3. Run the migration runner or manually execute the SQL

## Best Practices

- Never modify a migration after it has been applied; add a new migration instead
- A migration runs once and is recorded atomically with its schema changes
- SQL files may contain functions, triggers, and literals with semicolons
- Include comments explaining the purpose of the migration
- Keep migrations in chronological order (001, 002, 003, etc.)

