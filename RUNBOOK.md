# RUNBOOK.md

## Backup

### Database

**Backup command:**
```powershell
docker compose --env-file <path-to-external-env> -f docker-compose.prod.yml exec -T postgres pg_dump -U immigration -d immigration -Fc > immigration.dump
```

**Restore command:**
```powershell
# Create a new, empty database first (never restore over live DB)
docker compose --env-file <path-to-external-env> -f docker-compose.prod.yml exec -T postgres psql -U immigration -d postgres -c "CREATE DATABASE immigration_restore;"
# Pass host dump to container via stdin
docker compose --env-file <path-to-external-env> -f docker-compose.prod.yml exec -T postgres pg_restore -U immigration -d immigration_restore --no-owner --no-privileges < immigration.dump
# Verify row counts, schema/migration version, document hashes, rule versions and audit integrity before switching
```

**Never restore over the live database or append-only audit tables in place.**

### Raw snapshots

**Backup command:**
```powershell
docker run --rm -v immigration-knowledge-prod_raw_snapshots:/raw:ro -v <pwd>:/backup alpine tar czf /backup/raw-snapshots.tgz -C /raw .
```

**Restore command:**
```powershell
docker run --rm -v immigration-knowledge-prod_raw_snapshots:/raw -v <pwd>:/backup alpine tar xzf /backup/raw-snapshots.tgz -C /raw
```

**Verify integrity:** Check SHA-256 hashes of retrieved files against database records.

## Secret Rotation

1. **JWT_SECRET_KEY**: Generate a new 32+ character secret. Update your external environment file (e.g., `/etc/immigration/prod.env` outside the repo) and re-run `docker compose --env-file <path-to-external-env> -f docker-compose.prod.yml up -d --break-recreate`. The API will refuse to start with an invalid secret at startup (fail-closed). Never edit tracked `deploy/prod.env.example` for production secrets.

2. **POSTGRES_PASSWORD**: Update in your external environment file. The production config validates the database URL password against known weak values.

3. **ADMIN_PASSWORD**: Update in your external environment file. Re-run API startup to bootstrap with new password.

4. **COHERE_API_KEY**: Rotate in your external environment file. Update `EXTRACTION_MODEL` if needed.

5. **CORS_ORIGINS**: Update `https://${DOMAIN}` if the domain changes in your external environment file.

## Cooldown Management

- **Host refusal cooldown**: When a government host (e.g., Home Affairs WAF) refuses a request (HTTP 403/429), the source is paused for `FETCH_COOLDOWN_HOURS` (default 15 minutes). Existing cooldowns keep their recorded expiry.
- **Audit log**: All refusal events are recorded in the `fetch_log` table with `error_type` and `blocked_until`.
- **Manual retry**: Use the admin UI "Run now" button after the cooldown expires. A forced request cannot bypass an active cooldown.

## Adding a New Source

1. Obtain the government source URL.
2. Run `POST /api/v1/admin/discover` with the seed URL (admin only).
2. Review candidates in `GET /api/v1/admin/discovered`.
3. Approve a candidate via `POST /api/v1/admin/discovered/{id}/approve`, filling in:
   - `source_id`, `program_id`, `visa_category`, `authority`, `priority`, `monitoring_method`, `crawl_frequency`, `content_kind`, `notes`, `url` (fixed to candidate URL).
4. The source is now `ACTIVE` and will be synced on the next scheduled run.

## Upgrades

1. **Code changes**: Commit changes to the repository.
2. **Docker image rebuild**: `docker build -t immigration-knowledge-api .` and `docker build -t immigration-admin .`.
3. **Docker compose up**: `docker compose -f docker-compose.prod.yml -f <env-file> up -d --break-recreate`.
4. **Database migration**: `alembic upgrade head` (included in the compose up command).
5. **Verification**: Run the full test suite: `python -m pytest -q` and `npm test -- --run`.

## Monitoring

- Monitor `fetch_log` for refusal patterns and cooldowns.
- Monitor API health: `curl http://<domain>/health` and `curl http://<domain>/ready`.
- Monitor Caddy logs for TLS issues or rate limiting.
- Monitor PostgreSQL disk space and WAL size.

## Emergency Procedures

- **DB unreachable**: API starts without DB (graceful degradation). Check `DATABASE_URL` and PostgreSQL status.
- **Caddy TLS failure**: Check domain DNS and Caddy logs. Restart Caddy: `docker compose -f docker-compose.prod.yml restart caddy`.
- **Source sync stuck**: Use `POST /api/v1/admin/sync` with forced request, or wait for cooldown to expire.
EOF