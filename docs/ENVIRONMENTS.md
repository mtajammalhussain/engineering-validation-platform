
# Development and Production Environments

## 1. Overview

The Engineering Validation Platform (EVP) uses Docker Compose to manage four services:

- PostgreSQL database
- Results API
- Reporting API
- Test Simulator

Two separate environments have been configured: development and production-like.

## 2. Environment Comparison

| Configuration | Development | Production-like |
|---|---|---|
| Compose file | compose.dev.yaml | compose.prod.yaml |
| Project name | evp-dev | evp-prod |
| Results API host port | 8011 | 8021 |
| Reporting API host port | 8012 | 8022 |
| Database volume | evp-dev_postgres_dev_data | evp-prod_postgres_prod_data |
| Application images | Local patched images | Private GHCR multiarch-test images |
| Credentials | Development variables | Separate PROD_* variables |
| Restart policy | Not configured | unless-stopped |
| Simulator | Optional | Optional |

Both environments use PostgreSQL 16 and have separate persistent storage.

The production-like environment is used for local validation and is not a production-approved deployment.

## 3. Starting the Environments

Development:

```bash
docker compose -f compose.dev.yaml up -d
```

Production-like:

```bash
docker compose -f compose.prod.yaml up -d
```

The required environment variables must be supplied before starting either environment.

Check container status:

```bash
docker compose -f compose.dev.yaml ps
docker compose -f compose.prod.yaml ps
```

Do not use `docker compose down -v` unless intentionally deleting the environment's persistent database volume.

## 4. Database Initialization

The PostgreSQL initialization script is located at:

`docker/postgres/init/01-create-roles.sh`

On first initialization of an empty database volume, it creates:

- `evp_writer` — Application role for the Results API and Alembic migrations.
- `evp_reader` — Read-only application role for the Reporting API.

The `postgres` role is used for database administration.

Database schema changes are managed by Alembic.

Example migration command:

```bash
docker compose -f compose.dev.yaml run --rm --no-deps \
  --entrypoint alembic results-api upgrade head
```

Replace `compose.dev.yaml` with `compose.prod.yaml` for the production-like environment.

The migration creates the `test_results` table and tracks the applied revision in `alembic_version`.

## 5. Simulator

The Simulator is an optional service configured using the `simulation` Compose profile.

It runs in one-time mode and generates three synthetic measurements per batch.

Development:

```bash
docker compose -f compose.dev.yaml run --rm simulator
```

Production-like:

```bash
docker compose -f compose.prod.yaml run --rm simulator
```

The Simulator sends results to the Results API over the internal Compose network using the appropriate API key.

## 6. Health Checks and Testing

PostgreSQL is checked using `pg_isready`.

The Results API and Reporting API are checked using their `/ready` endpoints.

Both environments were successfully tested end to end:

Simulator → Results API → PostgreSQL → Reporting API

Each environment received and stored three synthetic measurements. The Results API retrieved those records, and the Reporting API returned matching statistics.

All three long-running services were verified as healthy in both environments.

## 7. Security and Limitations

- Development and production-like environments use separate credentials and volumes.
- Passwords and API keys are supplied through environment variables, not hard-coded in Compose files.
- The Reporting API uses the restricted `evp_reader` role.
- API host ports are bound to 127.0.0.1 for local testing.
- Production-like application images are stored in private GitHub Container Registry packages.
- Production images currently use the temporary `multiarch-test` tag.
- Image security scans still report unresolved HIGH-severity vulnerabilities.
- Centralized secrets management, release pinning, HTTPS, deployment automation and disaster recovery require further implementation.

This setup addresses the development and production environment configuration of Liora Requirement 2. Kubernetes deployment and automated CI/CD will be implemented in later stages.
