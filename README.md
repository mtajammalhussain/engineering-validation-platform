# Engineering Validation Platform

A DevOps and Cloud Engineering portfolio project that simulates a small engineering validation system used to collect, store, analyse, and report synthetic ECU test results.

The main purpose of this project is not only to build the application, but to design and operate it using modern DevOps practices including containerisation, CI/CD, Kubernetes, Infrastructure as Code, monitoring, security, and disaster recovery.

> **Project status:** Application development and Dockerization completed.
>
> Docker images are published to GitHub Container Registry (GHCR).
> Development and production deployment, Kubernetes, CI/CD, Terraform,
> monitoring and disaster recovery are being implemented progressively.

---

## Project Idea

In automotive validation engineering, electronic control units (ECUs) are tested under different operating conditions such as temperature, voltage, and load.

This project simulates that workflow.

A simulator generates synthetic validation test results. The results are sent to an API, stored in PostgreSQL, and made available through a separate reporting service for analysis.

---

## High-Level Architecture

The application consists of four main components:

### 1. Test Simulator

A Python application that generates synthetic ECU validation results.

Responsibilities:

- simulate engineering test measurements
- determine PASS / FAIL results
- send results to the Results API
- support one-time and continuous execution

### 2. Results API

A FastAPI service responsible for receiving and storing test results.

Responsibilities:

- accept validation results through REST endpoints
- validate incoming requests
- store results in PostgreSQL
- expose health, readiness and Prometheus metrics endpoints

### 3. Reporting API

A separate FastAPI service providing read-only access to validation statistics.

Responsibilities:

- retrieve stored results
- provide summaries by device and test
- expose time-series reporting endpoints
- use a read-only PostgreSQL database role
- expose health, readiness and Prometheus metrics

### 4. PostgreSQL

The central database containing validation results.

The database is shared by the Results API and Reporting API, while the Reporting API receives read-only access.

---

## Application Architecture
```text

                +----------------+
                | Test Simulator |
                |    Python      |
                +--------+-------+
                         |
                         | HTTP POST
                         v
                +----------------+
                | Results API    |
                |    FastAPI     |
                +--------+-------+
                         |
                         | SQL
                         v
                +----------------+
                |   PostgreSQL   |
                |                |
                +--------+-------+
                         ^
                         | Read-only SQL
                         |
                +----------------+
                | Reporting API  |
                |    FastAPI     |
                +--------+-------+
```

---

## Planned DevOps Architecture

The application will progressively be deployed using a modern DevOps workflow.

```text
Developer
    |
    v
GitHub Repository
    |
    v
GitHub Actions
    |
    +--> Run tests
    |
    +--> Build Docker images
    |
    +--> Security scanning
    |
    +--> Push images to container registry
    |
    v
Kubernetes
    |
    +--> Results API
    |
    +--> Reporting API
    |
    +--> Test Simulator
    |
    +--> PostgreSQL
    |
    +--> Prometheus
    |
    +--> Grafana
```

Terraform will be used to provision the required cloud infrastructure.

Separate development and production environments will be used, with automated deployment to development and controlled deployment to production.

## Technology Stack

- Python 3.12
- FastAPI
- PostgreSQL
- pytest
- Docker
- Kubernetes
- GitHub Actions
- Terraform
- Prometheus
- Grafana
- AWS
- GitHub Container Registry (GHCR)
- Trivy

---


## Current Project Status

### Implemented

- Results API, Reporting API and Test Simulator
- PostgreSQL schema and Alembic migrations
- Unit and integration tests
- Dockerfiles and .dockerignore files for all three applications
- Docker image builds and local functional testing
- Trivy vulnerability scanning and Debian security updates
- Multi-platform Docker images (AMD64 and ARM64) published to private GHCR packages

### In Progress

- Development and production deployment configuration
- Docker documentation and deployment instructions

### Planned

- Automated CI/CD pipeline with GitHub Actions
- Kubernetes deployment
- Terraform infrastructure
- Monitoring with Prometheus and Grafana
- Additional DevSecOps controls
- Backup and disaster recovery strategy

---

## Documentation

- [Dockerization Guide](docs/DOCKER.md)
