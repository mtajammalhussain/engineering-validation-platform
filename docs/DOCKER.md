
# Dockerization — Engineering Validation Platform

## 1. Overview

The Engineering Validation Platform (EVP) consists of three custom Python applications and one PostgreSQL database.

Docker packages the applications and their dependencies into images, allowing them to run consistently across different environments.

| Service | Image | Port |
|---|---|---|
| Results API | evp-results-api | 8001 |
| Reporting API | evp-reporting-api | 8002 |
| Simulator | evp-simulator | No exposed port |
| Database | postgres:16 | 5432 |

The three applications use custom Dockerfiles. PostgreSQL uses the official Docker image.

## 2. Dockerfiles

Each application has its own Dockerfile and .dockerignore.

Dockerfiles use Python 3.12.14-slim and:

- Install available Debian package updates.
- Install the application's Python dependencies.
- Copy the required application files.
- Create a non-root application user.
- Define the command for starting the application.

The .dockerignore files exclude unnecessary files from the build context, such as local virtual environments, caches and secrets.

## 3. Building Images

Run these commands from the project root:

```bash
docker build --pull -t evp-results-api:patched services/results-api

docker build --pull -t evp-reporting-api:patched services/reporting-api

docker build --pull -t evp-simulator:patched services/simulator
```

`docker build` creates an image from a Dockerfile.

`--pull` checks for an updated base image.

`-t` assigns an image name and tag.

The final argument specifies the build context.

The `patched` tag identifies images built with the Debian package update instructions.

## 4. Container Networking

A user-defined Docker network allows containers to communicate using container names.

```bash
docker network create evp-network
```

The existing PostgreSQL container was connected using:

```bash
docker network connect evp-network evp-postgres
```

The Results API connects to PostgreSQL using:

```text
DB_HOST=evp-postgres
DB_PORT=5432
```

The Simulator communicates with the Results API using:

```text
RESULTS_API_URL=http://evp-results-api:8001
```

Docker provides internal DNS resolution for containers connected to the same user-defined network.

Local API ports are bound to 127.0.0.1 during development rather than exposed on all host interfaces.

## 5. Environment Variables

Application configuration is passed through environment variables.

Examples:

- PORT — application listening port.
- DB_HOST — PostgreSQL hostname.
- DB_USER — database username.
- DB_PASSWORD — database password.
- RESULTS_API_KEY — API authentication key.
- SIM_MODE — simulator execution mode.
- SIM_BATCH_SIZE — measurements per batch.

The Reporting API uses the read-only PostgreSQL role `evp_reader`.

Actual passwords, API keys and local .env files must not be committed to Git.

## 6. Functional Testing

The three applications were tested successfully in Docker.

Results API:

```bash
curl -i http://127.0.0.1:8001/health
curl -i http://127.0.0.1:8001/ready
```

Reporting API:

```bash
curl -i http://127.0.0.1:8002/ready
curl -i http://127.0.0.1:8002/api/v1/reports/summary
```

Simulator was tested in one-time mode with three measurements.

Result:

```text
3 sent | 3 accepted | 0 rejected | 0 undelivered
```

The measurements were subsequently retrieved through the Results API.

## 7. Security Scanning — Trivy

Trivy was used to scan all three images for HIGH and CRITICAL vulnerabilities.

Example:

```bash
trivy image --scanners vuln --severity HIGH,CRITICAL evp-results-api:patched
```

Security updates were added to each Dockerfile:

```dockerfile
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
```

Results from November 2026:

| Image | Before | After |
|---|---|---|
| Results API | 54 HIGH | 47 HIGH |
| Reporting API | 54 HIGH | 47 HIGH |
| Simulator | 54 HIGH | 47 HIGH |

No CRITICAL vulnerabilities were reported in these scans.

The remaining HIGH findings require further risk assessment and should not be treated as automatically resolved.

## 8. GitHub Container Registry (GHCR)

All three patched application images were uploaded to GHCR as private packages.

Image locations:

```text
ghcr.io/mtajammalhussain/evp-results-api
ghcr.io/mtajammalhussain/evp-reporting-api
ghcr.io/mtajammalhussain/evp-simulator
```

Example commands:

```bash
docker tag evp-results-api:patched \
  ghcr.io/mtajammalhussain/evp-results-api:patched

docker push ghcr.io/mtajammalhussain/evp-results-api:patched
```

`docker tag` assigns a registry-compatible reference.

`docker push` uploads the image to the registry.

The GHCR packages are private and require appropriate authentication.

## 9. Multi-Platform Images

Docker Buildx was used to build images for both:

- linux/amd64 — common Intel/AMD cloud servers.
- linux/arm64 — Apple Silicon and ARM-based servers.

Example:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/mtajammalhussain/evp-results-api:multiarch-test \
  --push \
  services/results-api
```

All three multi-platform images were published to GHCR.

Their architectures were verified using:

```bash
docker buildx imagetools inspect \
  ghcr.io/mtajammalhussain/evp-results-api:multiarch-test
```

The AMD64 Results API was also tested under emulation on an Apple Silicon Mac. Its health and readiness checks both passed.

## 10. Known Limitations and Next Steps

- The images still have 47 HIGH-severity Trivy findings each.
- The complete patched multi-platform stack has not yet been tested together.
- Native AMD64 cloud deployment has not yet been verified.
- Base-image digest pinning and full Python dependency locking are not yet implemented.
- Package upgrades during builds can produce different package versions over time.
- Docker Compose and separate development/production configurations are pending.
- Kubernetes deployment, CI/CD automation and disaster recovery remain future work.

This document records the completed Dockerization work for Liora Requirement 1. Development and production deployment configuration will be addressed under Requirement 2.
