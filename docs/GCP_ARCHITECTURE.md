# Structured Address AI — GCP Production Architecture

> **Version:** 1.1 — _17 March 2026_
> **Status:** Proposed — For Architecture Approval Forum review
> **Audience:** Architecture Review Board, Engineering Leadership, Security & Compliance
> **Prerequisite:** [DESIGN_V3.2.md](./DESIGN_V3.2.md) — agent architecture and pipeline logic
> **Companion:** [EXECUTIVE_SUMMARY.md](./EXECUTIVE_SUMMARY.md) — stakeholder overview

---

## Table of Contents

1. [Document Purpose](#1-document-purpose)
2. [Solution Overview](#2-solution-overview)
3. [Architecture Principles](#3-architecture-principles)
4. [GCP Target Architecture](#4-gcp-target-architecture)
5. [Architecture Diagrams](#5-architecture-diagrams)
6. [Component Specification](#6-component-specification)
7. [Data Architecture](#7-data-architecture)
8. [AI / LLM Architecture](#8-ai--llm-architecture)
9. [Network & Security Architecture](#9-network--security-architecture)
10. [Deployment Architecture](#10-deployment-architecture)
11. [Scalability & Performance](#11-scalability--performance)
12. [Reliability & Disaster Recovery](#12-reliability--disaster-recovery)
13. [Observability & Monitoring](#13-observability--monitoring)
14. [Cost Model](#14-cost-model)
15. [Environment Strategy](#15-environment-strategy)
16. [Migration Path (POC → Production)](#16-migration-path-poc--production)
17. [Compliance & Governance](#17-compliance--governance)
18. [Risks & Mitigations](#18-risks--mitigations)
19. [Architecture Decision Records](#19-architecture-decision-records)
20. [Appendix A: GCP Service Mapping](#appendix-a-gcp-service-mapping)
21. [Appendix B: Capacity Planning](#appendix-b-capacity-planning)

---

## 1. Document Purpose

This document presents the **GCP production architecture** for the Structured Address AI pipeline — a standalone batch-processing component that extracts, validates, and enriches city/town information from unstructured, multilingual address records at enterprise scale.

The system is **not an API**. It reads CSV files, processes them via Cloud Dataflow, writes results to BigQuery for review via a reporting tool, and — upon manual approval — promotes reviewed data to a production BigQuery table via a second Dataflow job.

### Scope

| In Scope | Out of Scope |
|----------|-------------|
| GCP infrastructure design for batch processing | Pipeline logic and agent internals (see DESIGN_V3.2.md) |
| Security, networking, IAM, encryption | Detailed code walkthrough |
| Scaling from 32K rows (POC) to 5M+ rows/day | Business case and problem statement (see EXECUTIVE_SUMMARY.md) |
| BigQuery staging → review → promote workflow | Non-GCP deployment options |
| Cost modelling for GCP services | Real-time / API-based processing |
| CI/CD and deployment pipelines | Historical design evolution (V1–V3) |
| Disaster recovery and reliability | Desktop / local development setup |

### Key Numbers

| Metric | Value |
|--------|-------|
| Current POC scale | 32K rows per batch |
| Production target | 5 million rows per day |
| Deterministic resolution rate | 70–85% of rows (no AI needed) |
| LLM resolution rate | 15–30% of rows |
| Deterministic row latency | < 5 ms per row |
| LLM row latency (Gemini Flash) | < 2 seconds per row |
| GeoNames reference database | 229K cities, 1.8M postal codes — Cloud Spanner (production), SQLite (dev) |

---

## 2. Solution Overview

### 2.1 What the System Does

The Structured Address AI pipeline receives free-form address text (e.g., `"Via Roma 15, 08042 Barisardo (OG)"` tagged with country code `IE`) and:

1. Extracts the correct **city/town name** by cross-referencing a 230K+ city database
2. Detects and corrects **wrong country codes**
3. Assigns a **confidence score** (0.00–1.00) to every result
4. Flags uncertain rows for **human review** rather than guessing
5. Writes results to **BigQuery staging** for review and approval

### 2.2 Design Philosophy

> **Use rules first, AI only when needed.**

The 8-step pipeline resolves ~85% of addresses deterministically (Steps 0–5: parsing, postal lookup, exact match, fuzzy scan). Only the remaining ~15% invoke a cloud LLM (Step 6). This delivers:

- **270× cost reduction** vs. a pure-AI approach
- **Deterministic, reproducible results** for the majority
- **Sub-5ms latency** for rule-resolved rows

### 2.3 Agent Architecture (Summary)

Built on **Google ADK (Agent Development Kit)** — 1 orchestrator + 4 sub-agents:

```
AddressPipelineAgent (orchestrator)
  ├── DeterministicResolverAgent  — Steps 0–5 (rule-based, no AI)
  ├── LlmAddressParserAgent       — Step 6 (AI, conditionally skipped)
  ├── RevalidationAgent            — Step 7 (safety check, always runs)
  └── PersistAgent                 — Step 8 (write results, always runs)
```

The same agent code runs unchanged across all deployment modes: local CLI and Dataflow batch.

### 2.4 Two-Job Workflow

```
Job 1: Process Pipeline (scheduled / event-triggered)
  CSV → GCS → Dataflow → BigQuery staging table
  ↓
  Data stewards review results via reporting tool (Looker Studio)
  ↓
Job 2: Promote (manually triggered after review)
  BigQuery staging (approved rows) → Dataflow → BigQuery main table
```

---

## 3. Architecture Principles

| # | Principle | Application |
|---|-----------|-------------|
| 1 | **Rules first, AI second** | 85% of rows resolved by deterministic lookups. LLM invoked only when rules fail. |
| 2 | **Never guess — flag for review** | No city is marked `validated` without a confirmed GeoNames database match. |
| 3 | **Batch-first, standalone component** | No API layer. CSV in → BigQuery out. Simple, auditable, scheduled. |
| 4 | **Human-in-the-loop** | All results go to a staging table for review via a reporting tool before promotion to production. |
| 5 | **Minimise blast radius** | Each GCP service is single-purpose. Failure in one component doesn't cascade. |
| 6 | **Defence in depth** | VPC, private networking, IAM least-privilege, CMEK encryption, no public endpoints for data services. |
| 7 | **Cost proportional to value** | Pay for LLM only on the ~15% of rows that need it. Serverless infrastructure scales to zero. |
| 8 | **Observability by default** | Structured logging, distributed tracing, custom metrics, and alerting — built in from day one. |
| 9 | **Immutable infrastructure** | All deployments via Terraform/IaC. No manual GCP console changes in production. |
| 10 | **Data sovereignty** | All processing and storage within a single GCP region. No cross-region data transfer. |
| 11 | **Idempotent processing** | Every row can be re-processed safely. Upsert semantics on all writes. |

---

## 4. GCP Target Architecture

### 4.1 High-Level Component Map

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         GCP Project: address-ai-prod                        │
│                         Region: europe-west2 (London)                       │
│                                                                             │
│  ┌─ Ingestion ─────────────────────────────────────────────────────────┐    │
│  │                                                                     │    │
│  │  Cloud Storage (GCS)                Cloud Scheduler                 │    │
│  │  ├── gs://addr-input/               (cron / manual trigger)         │    │
│  │  └── gs://addr-checkpoints/               │                         │    │
│  │                                           ▼                         │    │
│  │                                    Cloud Functions (trigger)        │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                            │                                │
│  ┌─ Processing ───────────────────────────▼────────────────────────────┐    │
│  │                                                                     │    │
│  │  ┌─────────────────────────────────────────────────────────────┐    │    │
│  │  │  Dataflow Job 1: Address Pipeline (scheduled/event-driven)  │    │    │
│  │  │  ├── Apache Beam pipeline                                   │    │    │
│  │  │  ├── ADK agentic pipeline (1 orchestrator + 4 sub-agents)   │    │    │
│  │  │  │   Deterministic agent resolves ~85% (no LLM)             │    │    │
│  │  │  │   LLM agent invoked only for remaining ~15%              │    │    │
│  │  │  ├── Auto-scaling workers (n1-standard-4)                   │    │    │
│  │  │  └── Output → BigQuery staging table                        │    │    │
│  │  └─────────────────────────────────────────────────────────────┘    │    │
│  │                                                                     │    │
│  │  ┌─────────────────────────────────────────────────────────────┐    │    │
│  │  │  Dataflow Job 2: Promote (manually triggered after review)  │    │    │
│  │  │  ├── Reads approved rows from BigQuery staging              │    │    │
│  │  │  ├── Writes to BigQuery main table                          │    │    │
│  │  │  └── Archives processed staging rows                        │    │    │
│  │  └─────────────────────────────────────────────────────────────┘    │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                            │                                │
│  ┌─ AI ───────────────────────────────────▼────────────────────────────┐    │
│  │                                                                     │    │
│  │  Vertex AI (Gemini 2.0 Flash)                                       │    │
│  │  ├── Accessed via LiteLLM abstraction layer                         │    │
│  │  ├── Temperature 0.0 (deterministic output)                         │    │
│  │  ├── Only invoked for ~15% of rows (Step 6)                         │    │
│  │  └── ~3,042 tokens per LLM row (97% prompt, 3% completion)          │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                            │                                │
│  ┌─ Data ─────────────────────────────────▼────────────────────────────┐    │
│  │                                                                     │    │
│  │  BigQuery                           Memorystore (Redis 7.x)         │    │
│  │  ├── staging.pipeline_results       ├── LLM response cache          │    │
│  │  ├── staging.jobs (metadata)        └── GeoNames query cache        │    │
│  │  ├── production.address_master                                      │    │
│  │  └── reference.geonames_cities                                      │    │
│  │                                                                     │    │
│  │  Cloud Spanner (GeoNames reference)                                 │    │
│  │  └── 229K cities, 1.8M postal codes — managed, low-latency reads    │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  ┌─ Review ────────────────────────────────────────────────────────────┐    │
│  │                                                                     │    │
│  │  Looker Studio / Connected Sheets                                   │    │
│  │  ├── Dashboard on BigQuery staging table                            │    │
│  │  ├── Data stewards review flagged rows (needs_review / rejected)    │    │
│  │  └── Mark rows as approved → triggers Dataflow Job 2                │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  ┌─ Observability ─────────────────────────────────────────────────────┐    │
│  │                                                                     │    │
│  │  Cloud Logging          Cloud Monitoring          Cloud Trace       │    │
│  │  (structured logs)      (metrics + alerts)        (distributed      │    │
│  │                                                    tracing)         │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 GCP Services Summary

| GCP Service | Role | Justification |
|-------------|------|---------------|
| **Cloud Dataflow** | Batch processing — Job 1 (pipeline) + Job 2 (promote) | Managed Apache Beam. Auto-scaling, fault-tolerant, exactly-once semantics. Two separate jobs for separation of concerns. |
| **Cloud Spanner** | GeoNames reference data (cities, postal codes, admin regions) | Managed, strongly-consistent, low-latency reads (~2–5 ms). Auto-scales with Dataflow worker count. No data baked into container. |
| **BigQuery** | Pipeline results (staging + production tables), job metadata, GeoNames reference (reporting) | Serverless analytics warehouse. Native Looker Studio integration for review. No capacity planning. Streaming inserts from Dataflow. |
| **Memorystore (Redis)** | Caching | LLM response cache (avoid re-calling Gemini for identical addresses across batch runs). GeoNames query cache for hot paths. |
| **Cloud Storage (GCS)** | File I/O, checkpoints, archives | Input CSV files, Dataflow checkpoints, audit archives. |
| **Vertex AI** | LLM inference (Gemini 2.0 Flash) | Native GCP integration, VPC-SC compatible, pay-per-token, SLA-backed. |
| **Cloud Functions** | Event triggers | Trigger Dataflow Job 1 when new file lands in GCS. Trigger Dataflow Job 2 manually via console / gcloud CLI. |
| **Cloud Scheduler** | Cron scheduling | Scheduled batch runs (e.g., nightly 2 AM processing of accumulated input files). |
| **Looker Studio** | Review & reporting | Data stewards review pipeline results directly on BigQuery staging table. No custom review UI needed. |
| **Artifact Registry** | Container images | Store and version Docker images for Dataflow worker containers. |
| **Secret Manager** | Secrets | API keys, service account keys. No secrets in env vars or code. |
| **Cloud Logging** | Centralized logging | Structured JSON logs from all services. Log-based metrics and alerts. |
| **Cloud Monitoring** | Metrics & alerting | Custom pipeline metrics, SLO dashboards, PagerDuty/email alerts. |
| **Cloud Trace** | Distributed tracing | End-to-end trace per address row across Dataflow → Vertex AI → BigQuery. |
| **VPC / Private Service Connect** | Networking | Private connectivity between all services. No public internet exposure for data plane. |
| **Cloud IAM** | Access control | Least-privilege service accounts per component. Workload Identity for Dataflow. |
| **Cloud KMS** | Encryption | Customer-managed encryption keys (CMEK) for BigQuery, GCS, and Redis at-rest encryption. |

---

## 5. Architecture Diagrams

### 5.1 Dataflow Job 1: Address Pipeline (Primary Use Case)

```
                     ┌──────────────┐
                     │ Upstream     │
                     │ System       │
                     │ (SFTP / API) │
                     └──────┬───────┘
                            │ Upload CSV/Excel
                            ▼
┌───────────────────────────────────────────────────┐
│  Cloud Storage: gs://addr-input/                  │
│  └── 2026-03-17/addresses_batch_001.csv           │
└───────────────────┬───────────────────────────────┘
                    │ GCS Object Notification
                    ▼
┌───────────────────────────────────────────────────┐
│  Cloud Functions: trigger-pipeline                │
│  ├── Validates file format (CSV/Excel)            │
│  ├── Creates job record in BigQuery staging.jobs  │
│  ├── Launches Dataflow Job 1 (pipeline)           │
│  │   with input_path, job_id                      │
│  └── Posts notification to ops channel            │
└───────────────────┬───────────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  Dataflow Job 1: address-pipeline-batch                                   │
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────────┐   │
│  │  ADK Pipeline (unified — 1 orchestrator + 4 sub-agents per row)    │   │
│  │                                                                    │   │
│  │  ReadFromGCS → ParseCSV → ProcessAddressFn (ADK Runner per row)    │   │
│  │    │                                                               │   │
│  │    ├── DeterministicResolverAgent (Steps 0–5, CustomAgent)         │   │
│  │    │   ~85% resolved → skip LLM                                    │   │
│  │    │   ~15% unresolved → LlmAddressParserAgent (Step 6, LlmAgent)  │   │
│  │    │                                                               │   │
│  │    ├── RevalidationAgent (Step 7) — always runs                    │   │
│  │    ├── PersistAgent (Step 8) — always runs                         │   │
│  │    └── Result → WriteToBigQuery (staging.pipeline_results)         │   │
│  └────────────────────────────────────────────────────────────────────┘   │
│                                          │                                │
│  Auto-scaling: 1–50 workers (n1-standard-4)                               │
│  Checkpointing: Beam-managed                                              │
│  Region: europe-west2                                                     │
└──────────────────────────────────┬────────────────────────────────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
  ┌──────────────────┐  ┌──────────────┐  ┌────────────────────┐
  │ BigQuery         │  │ Memorystore  │  │ Cloud Monitoring   │
  │ (staging)        │  │ (Redis)      │  │ (Metrics + Alerts) │
  │                  │  │              │  │                    │
  │ • pipeline_      │  │ LLM resp.    │  │ • Rows processed   │
  │   results        │  │ cache        │  │ • LLM ratio        │
  │ • jobs           │  │              │  │ • Error rate       │
  │   (metadata)     │  │              │  │ • Latency p95      │
  └──────────────────┘  └──────────────┘  └────────────────────┘
```

### 5.2 Review & Promote Workflow

```
┌───────────────────────────────────────────────────────────────────────────┐
│  BigQuery: staging.pipeline_results                                       │
│  (all rows from Dataflow Job 1)                                           │
│                                                                           │
│  ┌─────────────────────────────────────────────────────┐                  │
│  │  status = 'validated'    (85%)  ← auto-approved     │                  │
│  │  status = 'needs_review' (10%)  ← flagged for review│                  │
│  │  status = 'rejected'    (5%)   ← auto-rejected      │                  │
│  └──────────────────────────┬──────────────────────────┘                  │
└─────────────────────────────┼─────────────────────────────────────────────┘
                              │
                              ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  Looker Studio / Connected Sheets (Reporting Tool)                        │
│                                                                           │
│  ├── Dashboard: batch summary, confidence distribution, country mismatches│
│  ├── Review view: filter by status = 'needs_review'                       │
│  ├── Data stewards: inspect flagged rows, verify town, update status      │
│  │   ├── Approve → UPDATE staging.pipeline_results SET review_status =    │
│  │   │             'approved' WHERE ...                                   │
│  │   └── Reject  → UPDATE staging.pipeline_results SET review_status =    │
│  │                  'rejected_by_reviewer' WHERE ...                      │
│  └── Validated rows are auto-approved (no manual review needed)           │
│                                                                           │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │  Manual trigger (gcloud / console / Cloud Function)
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  Dataflow Job 2: address-promote                                          │
│                                                                           │
│  ├── ReadFromBigQuery: staging.pipeline_results                           │
│  │     WHERE review_status IN ('approved', 'auto_approved')               │
│  │     AND promoted = FALSE                                               │
│  │                                                                        │
│  ├── Transform: map staging schema → production schema                    │
│  │                                                                        │
│  ├── WriteToBigQuery: production.address_master                           │
│  │     (WRITE_APPEND or merge on primary key)                             │
│  │                                                                        │
│  └── Update staging: SET promoted = TRUE, promoted_at = CURRENT_TIMESTAMP │
│                                                                           │
│  Triggered: Manually by data steward after review is complete             │
│  Frequency: After each review cycle (typically once per batch)            │
│  Idempotent: promoted flag prevents duplicate promotion                   │
└───────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  BigQuery: production.address_master                                      │
│  (clean, reviewed data — source of truth for downstream consumers)        │
│                                                                           │
│  ├── Downstream reporting tools                                           │
│  ├── Data lake / data warehouse consumers                                 │
│  └── Operational systems                                                  │
└───────────────────────────────────────────────────────────────────────────┘
```

### 5.3 Network Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  VPC: addr-ai-vpc (10.0.0.0/16)                                             │
│  Region: europe-west2                                                       │
│                                                                             │
│  ┌─ Private Subnet: processing (10.0.1.0/24) ──────────────────────────┐    │
│  │                                                                     │    │
│  │  Cloud Dataflow workers (Job 1 + Job 2)                             │    │
│  │  Cloud Functions (via VPC connector)                                │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                              │                                              │
│                              │ Private Service Connect / Private IP         │
│                              │                                              │
│  ┌─ Private Subnet: data (10.0.2.0/24) ────────────────────────────────┐    │
│  │                                                                     │    │
│  │  Cloud Spanner (GeoNames) — private IP via PSC                      │    │
│  │  Memorystore (Redis 7.x) — private IP only                          │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  ┌─ Google-Managed Services (no VPC placement) ────────────────────────┐    │
│  │                                                                     │    │
│  │  BigQuery — serverless, accessed via googleapis.com                 │    │
│  │  Vertex AI — via googleapis.com Private Service Connect             │    │
│  │  GCS — via VPC Service Controls perimeter                           │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  NAT Gateway: addr-ai-nat (egress for Dataflow workers if needed)           │
│  DNS: Cloud DNS private zone for internal service resolution                │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Component Specification

### 6.1 Cloud Dataflow — Job 1: Address Pipeline

| Attribute | Value |
|-----------|-------|
| **Runtime** | Apache Beam (Python SDK) |
| **Worker type** | `n1-standard-4` (4 vCPU, 15 GB RAM) |
| **Auto-scaling** | 1–50 workers (Dataflow auto-scaling) |
| **Pipeline type** | Batch |
| **Disk** | 50 GB SSD per worker (temp/shuffle) |
| **Docker image** | Custom container with libpostal and pipeline code |
| **Max parallelism** | 200 concurrent rows (50 workers × 4 vCPU) |
| **SDK version** | Apache Beam 2.55+ (Python) |
| **Trigger** | GCS object notification → Cloud Functions, or Cloud Scheduler cron |
| **Output** | BigQuery `staging.pipeline_results` (streaming inserts) |

**Worker container contents:**

```dockerfile
FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpostal-dev \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application code
COPY address_pipeline_agent/ /app/address_pipeline_agent/
COPY services/ /app/services/
COPY utils/ /app/utils/
COPY src/ /app/src/

WORKDIR /app
```

**GeoNames reference data** is stored in **Cloud Spanner** — a fully managed, globally consistent database. Dataflow workers connect to Spanner over the private network for low-latency reads (~2–5 ms). This eliminates the need to bake reference data into the container image, reduces image size, and allows GeoNames refreshes to be applied immediately (Spanner data update) without rebuilding and redeploying container images.

### 6.2 Cloud Dataflow — Job 2: Promote

| Attribute | Value |
|-----------|-------|
| **Runtime** | Apache Beam (Python SDK) |
| **Worker type** | `n1-standard-2` (2 vCPU, 7.5 GB RAM) — lightweight, no LLM |
| **Auto-scaling** | 1–5 workers |
| **Pipeline type** | Batch |
| **Trigger** | Manually triggered by data steward (gcloud CLI, console, or Cloud Function endpoint) |
| **Input** | BigQuery `staging.pipeline_results WHERE review_status IN ('approved','auto_approved') AND promoted = FALSE` |
| **Output** | BigQuery `production.address_master` |
| **Post-action** | Updates `staging.pipeline_results SET promoted = TRUE, promoted_at = CURRENT_TIMESTAMP` |

**Beam pipeline (simplified):**

```python
with beam.Pipeline(options=pipeline_options) as p:
    approved_rows = (
        p
        | 'ReadStaging' >> beam.io.ReadFromBigQuery(
            query="""
                SELECT * FROM staging.pipeline_results
                WHERE review_status IN ('approved', 'auto_approved')
                  AND promoted = FALSE
                  AND job_id = @job_id
            """,
            use_standard_sql=True)
        | 'MapToProductionSchema' >> beam.Map(map_staging_to_production)
        | 'WriteToProduction' >> beam.io.WriteToBigQuery(
            'production.address_master',
            write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND,
            create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER)
    )
```

### 6.3 BigQuery

| Attribute | Value |
|-----------|-------|
| **Dataset: staging** | Pipeline results, job metadata (written by Job 1, read by reviewers + Job 2) |
| **Dataset: production** | Promoted address data (written by Job 2, read by downstream systems) |
| **Dataset: reference** | GeoNames cities, postal codes (loaded by ETL, used for analytics/reporting) |
| **Location** | europe-west2 (London) |
| **Encryption** | CMEK via Cloud KMS |
| **Access control** | Dataset-level IAM. Staging: read/write for pipeline, read for reviewers. Production: write for promote job, read for consumers. |
| **Streaming inserts** | Used by Dataflow Job 1 (low-latency writes during batch processing) |
| **Cost model** | Storage: $0.02/GB/month. Queries: $6.25/TB scanned (on-demand). |

**Schema — staging.pipeline_results:**

```sql
CREATE TABLE staging.pipeline_results (
    id                  STRING NOT NULL,        -- UUID
    job_id              STRING NOT NULL,
    row_index           INT64 NOT NULL,
    address_1           STRING,
    address_2           STRING,
    address_3           STRING,
    country_code        STRING NOT NULL,         -- 2-char ISO
    town                STRING,
    status              STRING NOT NULL,          -- validated|needs_review|rejected
    confidence_score    FLOAT64,
    parser_source       STRING,                   -- libpostal|geonames_scan|llm_agent
    geonames_match      BOOL DEFAULT FALSE,
    geonames_id         INT64,
    normalized_town     STRING,
    suggested_country_code STRING,
    mismatch_detected   BOOL DEFAULT FALSE,
    warnings            STRING,
    review_reason       STRING,
    llm_prompt_tokens   INT64 DEFAULT 0,
    llm_completion_tokens INT64 DEFAULT 0,
    processing_time_ms  INT64,

    -- Review workflow columns
    review_status       STRING DEFAULT 'pending', -- pending|auto_approved|approved|rejected_by_reviewer
    reviewer            STRING,
    reviewed_at         TIMESTAMP,
    review_notes        STRING,

    -- Promotion tracking
    promoted            BOOL DEFAULT FALSE,
    promoted_at         TIMESTAMP,
    promoted_job_id     STRING,                   -- Job 2 run that promoted this row

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
)
PARTITION BY DATE(created_at)
CLUSTER BY job_id, status, review_status;
```

**Schema — staging.jobs:**

```sql
CREATE TABLE staging.jobs (
    job_id              STRING NOT NULL,
    input_path          STRING NOT NULL,
    total_rows          INT64 NOT NULL,
    processed_rows      INT64 DEFAULT 0,
    deterministic_count INT64 DEFAULT 0,
    llm_count           INT64 DEFAULT 0,
    status              STRING DEFAULT 'running',  -- running|completed|failed
    started_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
    completed_at        TIMESTAMP,
    error_message       STRING,
    -- Review summary (updated after review)
    approved_count      INT64 DEFAULT 0,
    rejected_count      INT64 DEFAULT 0,
    promoted            BOOL DEFAULT FALSE,
    promoted_at         TIMESTAMP,
);
```

**Schema — production.address_master:**

```sql
CREATE TABLE production.address_master (
    id                  STRING NOT NULL,
    source_job_id       STRING NOT NULL,
    source_row_index    INT64 NOT NULL,
    address_1           STRING,
    address_2           STRING,
    address_3           STRING,
    country_code        STRING NOT NULL,
    town                STRING NOT NULL,
    confidence_score    FLOAT64,
    parser_source       STRING,
    geonames_id         INT64,
    normalized_town     STRING,
    mismatch_detected   BOOL DEFAULT FALSE,
    suggested_country_code STRING,
    promoted_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
)
PARTITION BY DATE(promoted_at)
CLUSTER BY country_code, town;
```

**Auto-approval rule:** Rows with `status = 'validated'` and `confidence_score >= 0.85` are automatically set to `review_status = 'auto_approved'` by a post-processing step in Dataflow Job 1. Only `needs_review` rows require manual inspection.

### 6.4 Memorystore (Redis)

| Attribute | Value |
|-----------|-------|
| **Tier** | Standard (HA with replica) |
| **Instance size** | 4 GB |
| **Redis version** | 7.x |
| **Connectivity** | Private IP only (VPC peering) |
| **Eviction policy** | `allkeys-lru` |
| **Max memory policy** | 80% threshold |

**Cache strategy:**

| Cache Key Pattern | TTL | Purpose | Hit Rate (est.) |
|-------------------|-----|---------|----------------|
| `llm:{hash(prompt)}` | 24h | Avoid re-calling Gemini for identical addresses | 5–15% (duplicate addresses across batches) |
| `geo:city:{cc}:{name_lower}` | 7d | GeoNames city lookup cache | 60–80% (common cities repeat) |
| `geo:postal:{cc}:{code}` | 7d | Postal code lookup cache | 40–60% |

### 6.5 Cloud Storage (GCS)

| Bucket | Purpose | Lifecycle | Access |
|--------|---------|-----------|--------|
| `gs://addr-input/` | Incoming address CSV/Excel files | Delete after 90 days | Write: upstream systems. Read: Cloud Functions, Dataflow. |
| `gs://addr-checkpoints/` | Dataflow job checkpoints | Delete 7 days after job completion | Write/Read: Dataflow workers only. |
| `gs://addr-archive/` | Long-term audit trail (exported from BigQuery) | Coldline after 90 days, delete after 7 years | Write: nightly archive job. Read: compliance audits only. |

### 6.6 Vertex AI

| Attribute | Value |
|-----------|-------|
| **Model** | Gemini 2.0 Flash (`gemini-2.0-flash`) |
| **Access method** | Vertex AI API via LiteLLM abstraction |
| **Temperature** | 0.0 (deterministic) |
| **Max output tokens** | 2,048 |
| **Timeout** | 180 seconds per request |
| **Region** | europe-west2 (same as data) |
| **Quota** | 1,000 RPM (requests per minute) — request increase if needed |

### 6.7 Looker Studio (Review Tool)

| Attribute | Value |
|-----------|-------|
| **Data source** | BigQuery `staging.pipeline_results` |
| **Users** | Data stewards (review), managers (oversight) |
| **Access** | Google Workspace SSO, IAM-controlled BigQuery access |
| **Key views** | Batch summary, flagged rows (needs_review), confidence distribution, country mismatch report |
| **Review action** | Reviewers update `review_status` in BigQuery via Connected Sheets or a lightweight Apps Script form |
| **Cost** | Free (included with Google Workspace) |

---

## 7. Data Architecture

### 7.1 Data Flow Diagram

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Data Flow: End-to-End                                                     │
│                                                                            │
│  External        GCS Input      Dataflow       BigQuery        Reporting   │
│  ─────────       ─────────      Job 1          ────────        ──────────  │
│                                                                            │
│  Upstream    ──► addr-input/ ──► ADK Pipeline:  staging.       ──► Looker  │
│  system          (CSV/Excel)     Steps 0–8      pipeline_          Studio /│
│  (SFTP/API)                      via ADK        results          Connected │
│                                  orchestrator                    Sheets    │
│                                      │                                     │
│                                      │ ~15% → LLM                          │
│                                      │ (Step 6 only)                       │
│                                      ▼                                     │
│                                  Vertex AI                                 │
│                                  Gemini Flash                              │
│                                  (~3K tokens/row)                          │
│                                                                            │
│                                                     ┌──────────────────┐   │
│                                                     │ Data stewards    │   │
│                                                     │ review flagged   │   │
│                                                     │ rows, approve /  │   │
│                                                     │ reject           │   │
│                                                     └────────┬─────────┘   │
│                                                              │             │
│                                                     Manual trigger         │
│                                                              │             │
│                                                              ▼             │
│                                                     ┌──────────────────┐   │
│                                                     │ Dataflow Job 2:  │   │ 
│                                                     │ Promote approved │   │
│                                                     │ rows → BigQuery  │   │
│                                                     │ production.      │   │
│                                                     │ address_master   │   │
│                                                     └──────────────────┘   │
│                                                                            │
│  GeoNames    ──► Cloud Spanner (low-latency reads from Dataflow workers)   │
│  (quarterly     + BigQuery reference.geonames_cities (for reporting)       │
│   refresh)                                                                 │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

### 7.2 Data Classification

| Data Category | Classification | Handling |
|---------------|---------------|----------|
| Address text (input) | **Confidential** — may contain PII (names, street addresses) | Encrypted at-rest (CMEK), encrypted in-transit (TLS 1.3), no logging of raw address text in production, retention per data policy |
| Pipeline results (BigQuery staging) | **Confidential** | Same as input. Includes original address text plus enriched fields. BigQuery dataset-level IAM. |
| Production address data (BigQuery production) | **Confidential** | Reviewed and approved data. Same protections as staging. |
| GeoNames reference data | **Public** | Open-source geographic data. No special handling required. |
| LLM prompts/responses | **Confidential** | Contain address data. Vertex AI does not store prompts/responses for model improvement when using the API (data processing commitment). |
| Job metadata | **Internal** | Job IDs, timestamps, counts. No PII. Standard protection. |

### 7.3 Data Retention

| Data | Retention | Storage Tier | Deletion Method |
|------|-----------|-------------|----------------|
| Input files (GCS) | 90 days | Standard → Coldline | GCS lifecycle policy |
| BigQuery staging results | 90 days active, archive to long-term storage | BigQuery (active → long-term auto-tiering) | Partition expiration or scheduled DELETE |
| BigQuery production table | Indefinite (source of truth) | BigQuery (active) | N/A — retained as long as needed |
| Checkpoints (GCS) | 7 days after job completion | Standard | GCS lifecycle policy |
| Logs | 30 days (Cloud Logging) | Cloud Logging | Default log retention policy |
| Redis cache | 24h (LLM), 7d (geo) | Memorystore | TTL-based automatic eviction |
| Archive exports (GCS) | 7 years | Coldline | GCS lifecycle policy |

### 7.4 GeoNames Data Refresh

The GeoNames reference database is refreshed **quarterly** to capture new cities and updated administrative boundaries:

```
1. Download updated GeoNames source files (cities500.txt, allCountries.txt, admin1CodesASCII.txt)
2. Run ETL: python -m src.geonames_etl → produces updated reference data
3. Validate: run benchmark test suite against new data
4. Load into Cloud Spanner: update geonames_cities, geonames_postal_codes tables (for pipeline processing)
5. Load into BigQuery: bq load reference.geonames_cities (for reporting)
6. No container rebuild needed — next batch run reads updated Spanner data automatically
```

---

## 8. AI / LLM Architecture

### 8.1 Model Selection

| Criterion | Gemini 2.0 Flash (selected) | GPT-4o mini | Claude 3.5 Sonnet |
|-----------|----------------------------|-------------|-------------------|
| Cost (30M rows, 30% LLM) | **$3,000** | $4,500 | $91,800 |
| Latency (p95) | ~1.5s | ~2s | ~3s |
| Native GCP integration | ✅ Vertex AI | ❌ External API | ❌ External API |
| VPC-SC compatible | ✅ | ❌ | ❌ |
| Data residency (EU) | ✅ europe-west2 | ❌ US-based | ❌ US-based |
| ADK native support | ✅ | Via LiteLLM | Via LiteLLM |

### 8.2 LLM Integration Pattern

```
Pipeline Code                  LiteLLM                    Vertex AI
─────────────                  ───────                    ─────────

LlmAddressParserAgent ──► litellm.completion() ──► Gemini 2.0 Flash API
  │                          │                        │
  │ model="gemini-2.0-flash" │ Translates to          │ Returns structured
  │ tools=[5 GeoNames tools] │ Vertex AI format       │ JSON response
  │ temperature=0.0          │                        │
  │ max_tokens=2048          │                        │
  ◄──────────────────────────◄────────────────────────┘
  │
  │ Parse response → extract town → verify via GeoNames tools
```

**LLM abstraction via LiteLLM** enables zero-code-change model switching:

| Environment | Model Identifier | Backend |
|-------------|-----------------|---------|
| Local dev | `ollama_chat/qwen3.5` | Ollama (local, free) |
| Staging | `gemini-2.0-flash` | Vertex AI (GCP) |
| Production | `gemini-2.0-flash` | Vertex AI (GCP) |
| Fallback | `gemini-1.5-flash` | Vertex AI (GCP, lower cost) |

### 8.3 LLM Cost Control Mechanisms

| Mechanism | Implementation | Impact |
|-----------|---------------|--------|
| **Rules-first pipeline** | Steps 0–5 resolve ~85% of rows without LLM | 85% cost reduction vs. pure-AI |
| **Max turns limit** | `LLM_MAX_TURNS=2` — cap tool-calling rounds | Prevents runaway token consumption |
| **Response cache (Redis)** | Cache LLM responses by prompt hash | 5–15% fewer API calls (duplicate addresses) |
| **Temperature 0.0** | Deterministic output — same input = same output | Enables caching, reproducibility |
| **Token budget** | `LLM_MAX_TOKENS=2048` — hard ceiling on response length | Cost predictability per row |
| **Concurrency semaphore** | `LLM_CONCURRENCY=N` — limits parallel API calls | Prevents quota exhaustion, controls spend rate |
| **Batch scheduling** | Run large batches during off-peak hours | Lower Vertex AI latency, better throughput |

### 8.4 LLM Token Budget (Measured)

| Metric | Value | Source |
|--------|-------|--------|
| Average tokens per LLM row | 3,042 | Measured from 13-row POC |
| Prompt tokens per call | ~1,500–1,800 | System prompt + tools + context |
| Completion tokens per call | ~50–100 | Structured JSON response |
| Average tool-calling rounds | 2.0 | Model queries GeoNames, then answers |
| Prompt/completion split | 97% / 3% | Prompt-dominated cost profile |

### 8.5 Vertex AI Responsible AI

| Control | Implementation |
|---------|---------------|
| Data Processing Agreement | Vertex AI API — Google does not use customer data for model training |
| Prompt logging | Disabled in production (no prompt/response storage on Google side) |
| Output validation | Step 7 (RevalidationAgent) re-verifies every LLM response against GeoNames |
| Hallucination guard | LLM must confirm town via GeoNames tools. Unverified towns → `needs_review` |
| Temperature | 0.0 — deterministic, reproducible output |

---

## 9. Network & Security Architecture

### 9.1 IAM & Service Accounts

| Service Account | Role | Permissions |
|----------------|------|-------------|
| `sa-dataflow-pipeline@` | Dataflow Job 1 worker | `dataflow.worker`, `storage.objectAdmin` (input/checkpoint buckets), `bigquery.dataEditor` (staging dataset), `aiplatform.user` (Vertex AI), `spanner.databaseReader` (GeoNames) |
| `sa-dataflow-promote@` | Dataflow Job 2 worker | `dataflow.worker`, `bigquery.dataEditor` (staging + production datasets) |
| `sa-trigger@` | Cloud Functions trigger | `dataflow.developer`, `storage.objectViewer` (input bucket), `bigquery.dataEditor` (staging.jobs) |
| `sa-etl@` | GeoNames ETL | `bigquery.dataEditor` (reference dataset), `storage.objectViewer` (reference bucket), `spanner.databaseAdmin` (GeoNames refresh) |
| `sa-reviewer@` | Data steward (review via Looker Studio) | `bigquery.dataViewer` (staging), `bigquery.dataEditor` (staging.pipeline_results — review_status column only via row-level security) |

**Principle:** Each service has its own service account with minimum required permissions. No shared service accounts. No `roles/editor` or `roles/owner`.

### 9.2 Network Security

| Control | Implementation |
|---------|---------------|
| **No public IPs** | Redis, Cloud Spanner — private IP only. No public endpoint. |
| **VPC Service Controls** | Perimeter around GCS buckets, BigQuery datasets, Cloud Spanner, Vertex AI. Prevents data exfiltration. |
| **Dataflow networking** | Workers in private subnet. Egress via NAT gateway (only for Vertex AI API if Private Service Connect not available). |
| **Private Service Connect** | Vertex AI and BigQuery accessed via private endpoint (no public internet). |
| **TLS everywhere** | All service-to-service communication encrypted in-transit (TLS 1.3). |

### 9.3 Encryption

| Data State | Mechanism | Key Management |
|-----------|-----------|---------------|
| At-rest (BigQuery) | AES-256 | CMEK via Cloud KMS (`projects/addr-ai/locations/europe-west2/keyRings/addr-ai-kr`) |
| At-rest (Cloud Spanner) | AES-256 | CMEK via Cloud KMS |
| At-rest (GCS) | AES-256 | CMEK via Cloud KMS |
| At-rest (Redis) | AES-256 | Google-managed (Memorystore limitation — CMEK available on Redis Cluster) |
| In-transit | TLS 1.3 | Google-managed certificates |
| Secrets | Secret Manager | Automatic rotation (90-day policy) |

### 9.4 Secret Management

| Secret | Stored In | Accessed By |
|--------|-----------|-------------|
| Redis connection string | Secret Manager | Dataflow Job 1 (via service account) |
| Spanner instance/database ID | Not a secret — configured via env | Dataflow Job 1 |
| Vertex AI API key | Not needed (Workload Identity) | — |
| GCS credentials | Not needed (Workload Identity) | — |
| BigQuery credentials | Not needed (Workload Identity) | — |

**Workload Identity** is used wherever possible — service accounts authenticate to GCP APIs without managing keys.

---

## 10. Deployment Architecture

### 10.1 CI/CD Pipeline

```
┌───────────────┐    ┌────────────────┐    ┌───────────────┐    ┌──────────────┐
│   Developer   │    │  Cloud Build   │    │  Artifact     │    │  Dataflow    │
│   pushes to   │───►│  (CI/CD)       │───►│  Registry     │───►│  Flex        │
│   main branch │    │                │    │               │    │  Templates   │
└───────────────┘    │  1. Lint/test  │    │  Docker image │    │              │
                     │  2. Build img  │    │  (versioned)  │    │  Job 1 + 2   │
                     │  3. Push to AR │    │               │    │              │
                     │  4. Update tpl │    └───────────────┘    └──────────────┘
                     └────────────────┘
```

### 10.2 Cloud Build Pipeline

```yaml
# cloudbuild.yaml
steps:
  # 1. Run tests
  - name: 'python:3.12-slim'
    entrypoint: 'bash'
    args:
      - '-c'
      - |
        pip install -r requirements.txt
        pytest tests/ -v --tb=short

  # 2. Build Docker image
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', '${_IMAGE}:${SHORT_SHA}', '.']

  # 3. Push to Artifact Registry
  - name: 'gcr.io/cloud-builders/docker'
    args: ['push', '${_IMAGE}:${SHORT_SHA}']

  # 4. Update Dataflow Flex Template metadata
  - name: 'gcr.io/cloud-builders/gcloud'
    args:
      - 'dataflow'
      - 'flex-template'
      - 'build'
      - 'gs://addr-ai-dataflow-templates/pipeline-${SHORT_SHA}.json'
      - '--image=${_IMAGE}:${SHORT_SHA}'
      - '--sdk-language=PYTHON'

substitutions:
  _IMAGE: 'europe-west2-docker.pkg.dev/addr-ai-prod/addr-ai/pipeline'
```

### 10.3 Infrastructure as Code (Terraform)

All GCP resources are provisioned via Terraform:

```
terraform/
├── main.tf                 # Provider, backend (GCS state)
├── variables.tf            # Input variables
├── outputs.tf              # Output values
├── modules/
│   ├── networking/         # VPC, subnets, NAT, firewall rules
│   ├── bigquery/           # Datasets (staging, production, reference), tables, IAM
│   ├── spanner/            # Cloud Spanner instance, GeoNames database + tables, IAM
│   ├── redis/              # Memorystore instance
│   ├── storage/            # GCS buckets, lifecycle policies
│   ├── dataflow/           # Job 1 + Job 2 Flex Templates, IAM
│   ├── monitoring/         # Dashboards, alert policies
│   └── security/           # KMS, Secret Manager, VPC-SC perimeter
└── environments/
    ├── staging.tfvars
    └── production.tfvars
```

### 10.4 Deployment Strategy

| Component | Strategy | Rollback Time |
|-----------|----------|------------|
| Dataflow Job 1 (pipeline) | New Flex Template version; in-flight job completes on old version | N/A (batch is atomic) |
| Dataflow Job 2 (promote) | New Flex Template version; previous template still launchable | N/A (batch is atomic) |
| BigQuery schema | Forward-compatible DDL migrations (versioned SQL scripts) | Rollback via time travel (7 days) |
| GeoNames data | Versioned in container image | Redeploy previous image tag |

---

## 11. Scalability & Performance

### 11.1 Throughput Targets

| Metric | POC (Current) | Production (Target) |
|--------|--------------|-------------------|
| Daily volume | 32K rows (ad-hoc) | 5M rows/day |
| Deterministic rate | < 5 ms/row | < 5 ms/row (unchanged) |
| LLM rate (Gemini) | N/A (local Ollama) | < 2 sec/row |
| End-to-end batch time (32K) | ~30 min (Ollama) | ~2 min (Gemini) |
| End-to-end batch time (5M) | N/A | ~4 hours |

### 11.2 Scaling Dimensions

| Dimension | Mechanism | Limits |
|-----------|-----------|--------|
| **Dataflow workers** | Auto-scaling 1–50 workers | GCP quota (adjustable) |
| **Vertex AI throughput** | 1,000 RPM default | Request quota increase to 5,000 RPM |
| **BigQuery ingestion** | Streaming inserts or batch load | 1 GB/sec (streaming), unlimited (batch load) |
| **Redis** | 4 GB (handles ~100K cached entries) | Scale to 16 GB if needed |

### 11.3 Performance Optimization: ADK Conditional LLM Skip

The **ADK orchestrator's conditional routing** (documented in DESIGN_V3.2.md §4) is critical for production performance. All rows pass through the same ADK pipeline, but the orchestrator skips the LLM agent for rows resolved deterministically:

```
ADK Pipeline (per row):
  → DeterministicResolverAgent (Steps 0–5, CustomAgent, no LLM)
      ~85% resolved → orchestrator skips Step 6
      ~15% unresolved → LlmAddressParserAgent (Step 6, LlmAgent)
  → RevalidationAgent (Step 7) — always
  → PersistAgent (Step 8) — always

At scale (5M rows/day):
  • ~4.25M rows: Steps 0–5 + 7 + 8 (~5 ms/row) = ~6 hours total
  • ~750K rows: Steps 0–6 + 7 + 8 (~1.5s/row, Gemini Flash) / 50 workers
  • With Vertex AI batching: ~4 hours for LLM rows
  • Results → BigQuery staging

Total: ~4.5 hours for 5M rows/day (fits in an overnight batch window)
```

### 11.4 Bottleneck Analysis

| Bottleneck | Likelihood | Mitigation |
|-----------|-----------|------------|
| Vertex AI rate limiting | Medium | Request quota increase. Redis response cache. Batch scheduling during off-peak. |
| BigQuery streaming insert quota | Low | Use batch load jobs instead of streaming for large volumes. Buffer inserts in Beam. |
| Dataflow worker memory | Low | No local GeoNames DB. GeoNames reads served by Cloud Spanner over network (~2–5 ms). n1-standard-4 has 15 GB. Ample headroom. |
| Network latency to Vertex AI | Low | Same region (europe-west2). Private Service Connect eliminates public internet hop. |

---

## 12. Reliability & Disaster Recovery

### 12.1 Availability Targets

| Component | Target SLA | GCP Backing SLA |
|-----------|-----------|----------------|
| Batch processing (Dataflow) | 99.5% | Dataflow: 99.9% |
| GeoNames reference (Spanner) | 99.999% | Cloud Spanner regional: 99.999% |
| Results store (BigQuery) | 99.99% | BigQuery: 99.99% |
| Cache (Redis) | 99.9% | Memorystore Standard: 99.9% |
| Overall pipeline | 99.5% | Lowest component (Dataflow) |

### 12.2 Failure Modes & Recovery

| Failure | Impact | Recovery | RTO |
|---------|--------|----------|-----|
| **Dataflow worker crash** | Loss of in-flight bundle (10–100 rows) | Beam auto-retry. Application checkpoint resume. | Automatic (seconds) |
| **Dataflow job failure** | Partial batch incomplete | Restart with `--resume`. Checkpoint has completed rows. | < 5 minutes |
| **Vertex AI outage** | LLM rows fail | Rows marked `needs_review`. Deterministic rows unaffected. Re-run LLM rows when service recovers. | Depends on GCP recovery |
| **BigQuery unavailable** | No writes (rare — 99.99% SLA) | Dataflow buffers in-memory. BigQuery recovers within minutes. | < 5 minutes |
| **Redis failure** | Cache miss → higher latency, more Vertex AI calls | Auto-failover to replica. Pipeline continues (cache is non-essential). | < 30 seconds |
| **GCS unavailable** | No input file access | Extremely rare (99.999999999% durability SLA). Wait for recovery. | Depends on GCP recovery |

### 12.3 Checkpointing (Production)

Production checkpointing uses BigQuery + GCS instead of local CSV:

| Aspect | Local (POC) | Production (GCP) |
|--------|-------------|-----------------|
| Progress tracking | `.ckpt.csv` file | `staging.jobs` table in BigQuery |
| Row results | `.ckpt.csv` rows | `staging.pipeline_results` table |
| Resume trigger | `--resume` CLI flag | Job-level resume via Cloud Functions |
| Max data loss | ≤ batch_size rows | ≤ Beam bundle size (~100 rows) |
| Idempotency | Row index matching | `job_id + row_index` deduplication in BigQuery |

### 12.4 Backup Strategy

| Data | Method | Frequency | Retention | Tested |
|------|--------|-----------|-----------|--------|
| BigQuery (staging) | Time travel + snapshots | Continuous (automatic) | 7-day time travel; snapshots per policy | Quarterly restore test |
| BigQuery (production) | Table snapshots + dataset copies | Daily snapshot | 30 days | Quarterly restore test |
| GCS (input) | Source system retains originals | N/A | 90 days on GCS | N/A |
| Terraform state | GCS backend with versioning | Every `terraform apply` | Indefinite | On every deployment |

---

## 13. Observability & Monitoring

### 13.1 Three Pillars

#### Logging (Cloud Logging)

| Log Source | Format | Retention | Key Fields |
|-----------|--------|-----------|-----------|
| Dataflow workers | Structured JSON | 30 days | `job_id`, `row_index`, `step`, `status`, `duration_ms` |
| Cloud Functions | Structured JSON | 30 days | `trigger_file`, `job_id`, `action` |

**Sensitive data policy:** Raw address text is **not logged** in production. Logs contain only metadata: row indexes, statuses, durations, and error categories.

#### Metrics (Cloud Monitoring)

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|----------------|
| `address/rows_processed_total` | Counter | Total rows processed (by status) | — |
| `address/deterministic_ratio` | Gauge | % of rows resolved without LLM | < 70% (investigate) |
| `address/llm_latency_seconds` | Histogram | LLM call latency distribution | p95 > 5s |
| `address/llm_error_rate` | Gauge | % of LLM calls that fail/timeout | > 5% |
| `address/batch_duration_seconds` | Gauge | Total batch processing time | > 2× expected |
| `address/review_queue_depth` | Gauge | Pending human review items | > 10,000 |
| `address/cache_hit_rate` | Gauge | Redis cache hit percentage | < 30% (investigate) |
| `address/llm_tokens_total` | Counter | Total LLM tokens consumed | Budget threshold |

#### Tracing (Cloud Trace)

End-to-end trace per address row:

```
Trace: row_42 (job: abc-123)
  │
  ├── dataflow.process_row (total: 1,850ms)
  │     ├── deterministic_resolver (4ms)
  │     │     ├── preprocess (0.5ms)
  │     │     ├── libpostal_parse (1ms)
  │     │     ├── postal_lookup (0.8ms)
  │     │     ├── exact_match (1.2ms)
  │     │     └── result: unresolved
  │     │
  │     ├── llm_parser (1,820ms)
  │     │     ├── litellm.completion (turn 1) (1,200ms)
  │     │     │     └── vertex_ai.gemini_flash (1,180ms)
  │     │     ├── tool_call: query_city("barisardo", "IE") (3ms)
  │     │     ├── litellm.completion (turn 2) (600ms)
  │     │     │     └── vertex_ai.gemini_flash (585ms)
  │     │     └── result: town=Barisardo, parser_source=llm_agent
  │     │
  │     ├── revalidation (8ms)
  │     │     └── geonames_exact_match: confirmed in IT
  │     │
  │     └── persist (15ms)
  │           ├── bigquery.streaming_insert (12ms)
  │           └── result: status=validated, confidence=0.75
  │
  Total: 1,850ms
```

### 13.2 Dashboards

| Dashboard | Audience | Key Panels |
|-----------|----------|-----------|
| **Pipeline Operations** | SRE / Ops | Rows/sec throughput, error rate, LLM latency p50/p95/p99, Dataflow worker count, batch progress |
| **Data Quality** | Data Stewards | Deterministic ratio, validation rate, review queue depth, country mismatch frequency, confidence histogram |
| **Cost** | Management | Daily LLM token spend, Vertex AI cost projection, BigQuery query cost, GCS egress |
| **Infrastructure** | Platform Team | BigQuery slot utilisation, Redis memory, Dataflow CPU utilisation, Dataflow worker count |

### 13.3 Alerting

| Alert | Condition | Channel | Severity |
|-------|-----------|---------|----------|
| Batch job failed | `jobs.status = 'failed'` | PagerDuty + Email | P2 |
| LLM error rate > 10% | `llm_error_rate > 0.10` for 5 min | PagerDuty | P2 |
| LLM latency p95 > 10s | `llm_latency_p95 > 10` for 10 min | Email | P3 |
| Review queue > 50K | `review_queue_depth > 50000` | Email | P3 |
| Promote job failed | `promote_job.status = 'failed'` | PagerDuty + Email | P2 |
| Deterministic ratio < 60% | `deterministic_ratio < 0.60` for 1 batch | Email | P3 (investigate data quality) |
| LLM spend > daily budget | Projected daily cost > threshold | Email + Slack | P3 |

---

## 14. Cost Model

### 14.1 Monthly Cost Estimate (5M rows/day)

| GCP Service | Specification | Monthly Cost (est.) |
|------------|--------------|-------------------|
| **Vertex AI (Gemini Flash)** | 5M × 30% LLM × 3,042 tokens × 30 days | **$2,700** |
| **Cloud Dataflow** | 10–20 workers × 4h/day × 30 days (Job 1) + Job 2 ~10 min/run | **$1,850** |
| **BigQuery** | ~2 TB active storage + ~5 TB queries/month (staging + production + review) | **$35** |
| **Cloud Spanner** | 1-node regional instance (GeoNames reference, read-heavy) | **$70** |
| **Memorystore (Redis)** | 4 GB Standard HA | **$250** |
| **Cloud Storage** | ~500 GB total (input + archive) | **$15** |
| **Cloud Logging/Monitoring** | Structured logs, custom metrics | **$100** |
| **Networking** | Private Service Connect, NAT | **$50** |
| **Cloud KMS** | 5 keys, ~1M operations/month | **$30** |
| **Artifact Registry** | Docker images, ~10 GB | **$5** |
| **Cloud Build** | ~20 builds/month | **$10** |
| | | |
| **Total estimated** | | **~$5,115/month** |

### 14.2 Cost Comparison

| Approach | Monthly Cost (5M rows/day) | Annual Cost |
|----------|---------------------------|-------------|
| **Manual processing** (ops team) | ~$7,500,000 ($0.05/row × 5M × 30) | ~$90M |
| **Pure AI** (every row → Gemini Flash) | ~$9,100/month (LLM) + $2,500 (infra) | ~$139K |
| **Our approach** (rules + 30% AI) | ~$5,115/month total | **~$61K** |

### 14.3 Cost Optimization Levers

| Lever | Impact | Trade-off |
|-------|--------|-----------|
| Improve deterministic ratio (85% → 90%) | -33% LLM cost | Requires fuzzy threshold tuning, may increase false positives |
| Use Gemini 1.5 Flash (cheaper) | -25% LLM cost | Slightly lower quality (verify with benchmark) |
| Increase Redis cache TTL | -5–15% LLM cost | Stale results for changed addresses (unlikely in batch) |
| Use preemptible/spot Dataflow workers | -60–80% compute cost | Workers can be preempted (checkpoint handles this) |
| BigQuery flat-rate pricing (editions) | Predictable query cost | Requires commitment; evaluate when query volume stabilises |

---

## 15. Environment Strategy

### 15.1 Three Environments

| Environment | GCP Project | Purpose | LLM | Data |
|-------------|------------|---------|-----|------|
| **Dev** | `addr-ai-dev` | Development, feature testing | Ollama (local) or Gemini Flash | Synthetic test data (13–100 rows) |
| **Staging** | `addr-ai-staging` | Integration testing, performance testing, UAT | Gemini Flash | Anonymised production sample (10K rows) |
| **Production** | `addr-ai-prod` | Live processing | Gemini Flash | Real customer data (5M rows/day) |

### 15.2 Environment Parity

| Aspect | Dev | Staging | Production |
|--------|-----|---------|-----------|
| Pipeline code | Same | Same | Same |
| Agent architecture | Same (5 agents) | Same | Same |
| GeoNames database | SQLite (local, 304 MB) | Cloud Spanner (managed) | Cloud Spanner (managed) |
| LLM model | Ollama (local) | Gemini Flash | Gemini Flash |
| Results store | Local CSV | BigQuery (staging dataset) | BigQuery (staging + production datasets) |
| Infrastructure | Local / Docker Compose | Scaled-down GCP resources | Full GCP resources |
| Terraform | Shared modules | `staging.tfvars` | `production.tfvars` |
| Monitoring | Local logs | Full observability stack | Full observability stack + alerting |

### 15.3 Configuration Management

All environment-specific configuration is managed via environment variables (`.env` / Secret Manager):

| Variable | Dev | Staging | Production |
|----------|-----|---------|-----------|
| `LLM_MODEL` | `ollama_chat/qwen3.5` | `gemini-2.0-flash` | `gemini-2.0-flash` |
| `LLM_MAX_TOKENS` | 2048 | 2048 | 2048 |
| `LLM_MAX_TURNS` | 2 | 2 | 2 |
| `LLM_CONCURRENCY` | 2 | 8 | 50 (per worker) |
| `LLM_TIMEOUT_SECONDS` | 180 | 30 | 30 |
| `FUZZY_MATCH_THRESHOLD` | 92 | 92 | 92 (tuned against prod data) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | N/A | N/A |
| `SPANNER_INSTANCE` | N/A (local SQLite) | `addr-ai-spanner` | `addr-ai-spanner` |
| `SPANNER_DATABASE` | N/A (local SQLite) | `geonames` | `geonames` |
| `BQ_STAGING_DATASET` | N/A (local CSV) | `addr_staging` | `addr_staging` |
| `BQ_PRODUCTION_DATASET` | N/A | `addr_production` | `addr_production` |

---

## 16. Migration Path (POC → Production)

### 16.1 Phased Rollout

```
Phase 1 (CURRENT)                    Phase 2
POC — Local Batch                    Cloud Batch (Production)
──────────────────                   ─────────────────────────

✅ Pipeline code                      Dataflow Job 1 (pipeline)
✅ 8-step pipeline                    Dataflow Job 2 (promote)
✅ Agent architecture                 BigQuery staging + production datasets
✅ GeoNames SQLite (local)             Cloud Spanner (GeoNames reference)
✅ Local batch runner                 Looker Studio review layer
✅ Checkpoint/resume                  Memorystore Redis cache
✅ Dashboard                          Scheduled batch (Cloud Scheduler)
                                     Production monitoring + alerting
                                     DR testing

Timeline: Complete                   ~4 weeks
```

### 16.2 Phase 2: Cloud Batch Deployment

**Scope:** Deploy the pipeline as a Dataflow batch job with BigQuery staging/review/promote workflow.

| Task | Description | Effort |
|------|-------------|--------|
| 1. Dockerize | Build container image with libpostal + pipeline code + GeoNames DB | 1 day |
| 2. Beam pipeline | Implement `ProcessAddressFn` (ParDo) with ADK orchestrator pipeline | 3 days |
| 3. Dataflow Job 1 template | Create Flex Template for parameterised pipeline job launch | 1 day |
| 4. BigQuery setup | Create staging, production, reference datasets + tables + IAM | 1 day |
| 5. Vertex AI integration | Switch `LLM_MODEL` to `gemini-2.0-flash`, configure Workload Identity | 0.5 day |
| 6. Redis cache | Provision Memorystore, integrate caching layer | 1 day |
| 7. Cloud Functions trigger | GCS event → launch Dataflow Job 1 | 1 day |
| 8. Cloud Scheduler | Nightly batch schedule | 0.5 day |
| 9. Looker Studio | Build review dashboard on BigQuery staging data | 2 days |
| 10. Dataflow Job 2 (promote) | Beam pipeline: read approved rows from staging → write to production | 1 day |
| 11. Networking & security | VPC Service Controls, Private Service Connect, KMS, IAM | 1 day |
| 12. Monitoring | Cloud Logging, Cloud Monitoring dashboards, alerting | 1 day |
| 13. Performance testing | 100K → 500K → 5M row scale tests | 3 days |
| 14. DR testing | Simulate Dataflow worker crash, Vertex AI outage | 1 day |
| 15. Integration testing | End-to-end tests with staging data | 2 days |
| 16. UAT | User acceptance with real address samples + review workflow | 2 days |
| 17. Runbook | Operational procedures for batch monitoring, failure recovery, GeoNames refresh | 1 day |

---

## 17. Compliance & Governance

### 17.1 Data Governance

| Requirement | Implementation |
|-------------|----------------|
| **Data classification** | Address data classified as Confidential. Tagged in Data Catalog. |
| **Data residency** | All processing and storage in `europe-west2` (London). No cross-region transfer. |
| **Access audit** | Cloud Audit Logs enabled for all data access. IAM policy reviewed quarterly. |
| **Retention** | Per Section 7.3 retention policy. Automated lifecycle enforcement. |
| **Right to deletion** | Pipeline results deletable by `job_id` or `row_index`. Cascade to review queue. |

### 17.2 Regulatory Alignment

| Regulation | Relevance | Controls |
|-----------|-----------|----------|
| **GDPR** | Address data may contain personal data (names embedded in address lines) | Data residency in EU/UK, encryption at-rest and in-transit, right to deletion, processing records |
| **PCI DSS** | Not applicable (no payment data) | — |
| **SOX** | Audit trail for financial data quality | Immutable audit log in GCS, Cloud Audit Logs |

### 17.3 Change Management

| Change Type | Process | Approval |
|-------------|---------|----------|
| Pipeline logic change | PR → code review → CI → staging deploy → UAT → production | Tech lead + QA |
| GeoNames data refresh | ETL → benchmark test → staging validation → production deploy | Data steward |
| Infrastructure change | Terraform PR → plan review → staging apply → production apply | Platform team + architecture approval |
| LLM model change | Benchmark evaluation → cost analysis → staging A/B test → production | Architecture forum |
| Threshold tuning | A/B test on staging → quality metrics comparison → production update | Data steward + tech lead |

---

## 18. Risks & Mitigations

| # | Risk | Probability | Impact | Mitigation |
|---|------|-------------|--------|------------|
| 1 | **Vertex AI outage** — LLM rows cannot be processed | Low | Medium | Deterministic rows (85%) unaffected. LLM rows queued for retry. Fallback model (Gemini 1.5 Flash). |
| 2 | **LLM hallucination** — wrong city returned confidently | Low | High | Step 7 re-validates every LLM response against GeoNames. Unverified → `needs_review`. Temperature 0.0. |
| 3 | **Cost overrun** — LLM ratio higher than expected | Medium | Medium | Redis cache, max turns limit, budget alerts, threshold tuning to increase deterministic ratio. |
| 4 | **Google ADK immaturity** — framework bugs or breaking changes | Medium | Medium | Pin ADK version. Business logic is plain Python with zero ADK dependency. Can rewire in ~2 days. |
| 5 | **Data quality degradation** — upstream sends worse data | Medium | Medium | Deterministic ratio metric with alert. Fuzzy threshold tuning. Expanded GeoNames with cities500 → cities1000 if needed. |
| 6 | **BigQuery query cost spike** — inefficient queries from Looker Studio or ad-hoc analysis | Low | Low | Partition and cluster all tables. Use BigQuery BI Engine for Looker dashboards. Set per-user query quotas. |
| 7 | **GeoNames data gap** — city missing from reference database | Low | Low | 229K cities (pop ≥ 500) covers 99%+ of business addresses. Postal code fallback. Quarterly refresh. |
| 8 | **Vendor lock-in** — deep GCP dependency | Low | Low | LiteLLM abstracts LLM provider. Pipeline logic is portable Python. BigQuery → any columnar store or data warehouse. Main lock-in: Dataflow (replaceable by Spark/Flink). |
| 9 | **Key person dependency** | Medium | Medium | Comprehensive documentation (this doc + DESIGN_V3.2 + EXECUTIVE_SUMMARY). Code is well-structured (5 agents, plain services). |

---

## 19. Architecture Decision Records

### ADR-GCP-001: Dataflow over Cloud Run Jobs for Batch Processing

**Decision:** Use Cloud Dataflow (Apache Beam) for batch processing instead of Cloud Run Jobs.

**Rationale:**
- Dataflow provides auto-scaling, fault-tolerance, and exactly-once semantics out of the box
- Built-in shuffle and GCS I/O via Beam transforms
- Handles worker failures transparently (retry bundles)
- Better suited for 5M+ row batches (Cloud Run Jobs have 24h timeout, limited parallelism)
- ADK orchestrator pipeline maps cleanly to Beam's `ParDo` pattern (conditional LLM skip handled internally)

**Trade-off:** Higher operational complexity. Cloud Run Jobs would be simpler for <100K row batches. Decision may be revisited if batch sizes remain small.

---

### ADR-GCP-002: Gemini 2.0 Flash as Production LLM

**Decision:** Use Gemini 2.0 Flash via Vertex AI as the production LLM.

**Rationale:**
- `$0.10/M prompt + $0.40/M completion` — cheapest viable option for our workload
- Native Vertex AI integration (VPC-SC, Workload Identity, data residency)
- Temperature 0.0 produces deterministic output — critical for caching and reproducibility
- Measured quality on POC test set: correctly resolves all adversarial test cases
- ADK provides native Gemini support — no LiteLLM translation overhead in production

**Trade-off:** Vendor lock-in to Google's model ecosystem. Mitigated by LiteLLM abstraction layer — model change requires only `.env` update.

---

### ADR-GCP-003: BigQuery over Cloud SQL for Pipeline Results

**Decision:** Use BigQuery for all pipeline results (staging and production) instead of Cloud SQL.

**Rationale:**
- Serverless — no instance sizing, connection pooling, or capacity planning required
- Native integration with Looker Studio for the review workflow — data stewards query staging data directly
- BigQuery supports the two-job workflow natively (staging → review → promote to production)
- Partition by date + cluster by job_id/status enables efficient queries at any scale
- Time travel (7 days) provides built-in point-in-time recovery without configuring backups
- Cost-efficient for our write-heavy, query-light workload ($5/TB scanned, first 1 TB/month free)
- GeoNames reference data served by Cloud Spanner for low-latency reads (~2–5 ms)

**Trade-off:** No ACID transactions for upserts — deduplication handled via `job_id + row_index` and `MERGE` statements. BigQuery has ~1s query latency (not suitable for real-time lookups, but fine for batch results).

---

### ADR-GCP-004: europe-west2 (London) as Primary Region

**Decision:** Deploy all resources in `europe-west2` (London).

**Rationale:**
- Data residency: UK/EU regulatory requirement for customer address data
- Proximity: closest GCP region to primary operations
- Service availability: all required GCP services available in europe-west2
- Vertex AI Gemini: available in europe-west2

**Trade-off:** Single-region deployment (no multi-region DR). Acceptable given batch workload nature — RPO is minutes (checkpoint), RTO is < 30 minutes (restart job).

---

### ADR-GCP-005: Cloud Spanner for GeoNames Reference Data

**Decision:** Use Cloud Spanner for GeoNames reference data (229K cities, 1.8M postal codes) accessed by Dataflow workers during pipeline processing.

**Rationale:**
- Managed, serverless — no instance sizing or connection pooling for read-heavy workloads
- Low-latency reads (~2–5 ms) over private network within the same region
- GeoNames data refreshes are applied instantly (Spanner data update) — no container image rebuild or redeployment required
- Strongly consistent reads across all Dataflow workers — no stale data risk
- Eliminates 304 MB from the container image, reducing image pull time and storage
- Auto-scales read throughput as Dataflow worker count increases

**Trade-off:** Small additional per-query network latency vs. in-process SQLite (~2–5 ms vs. <1 ms). Mitigated by Memorystore Redis caching of hot GeoNames lookups. Adds Cloud Spanner to the infrastructure cost (~$70/month for a single-node regional instance). GeoNames reference data also loaded into BigQuery reference dataset for ad-hoc analytical queries outside the pipeline.

---

### ADR-GCP-006: Two-Job Staging/Promote Workflow

**Decision:** Implement a two-job workflow: Dataflow Job 1 writes to BigQuery staging; Dataflow Job 2 (manually triggered) promotes reviewed rows to BigQuery production.

**Rationale:**
- Human-in-the-loop: data stewards must review pipeline output before it enters the production address master
- Clean separation of concerns: pipeline processing is fully automated; promotion requires explicit human approval
- Staging table serves dual purpose: review queue + audit trail of all pipeline output
- Manual trigger for Job 2 prevents accidental promotion of unreviewed data
- Auto-approval rule (status = `validated`, confidence ≥ 0.90) reduces review burden for high-confidence rows while retaining human oversight

**Trade-off:** Adds latency between pipeline completion and data availability in production (review cycle). Acceptable given the data quality requirements.

---

## Appendix A: GCP Service Mapping

| Current (POC) | GCP (Production)  | Notes |
|---------------|-------------------|-------|
| Local Python script (`batch_runner.py`) | **Cloud Dataflow Job 1** | Beam pipeline wrapping same pipeline code |
| N/A (manual review) | **Cloud Dataflow Job 2** | Promote reviewed rows from staging → production |
| SQLite (`geonames.db`) | **Cloud Spanner** + **BigQuery reference dataset** | Spanner for pipeline lookups; BigQuery for ad-hoc analysis |
| Local CSV output | **BigQuery (staging dataset)** | All pipeline results written to staging tables |
| N/A (no review workflow) | **Looker Studio** + **BigQuery (production dataset)** | Review on staging; approved rows promoted to production |
| Ollama (local LLM) | **Vertex AI (Gemini Flash)** | LiteLLM abstraction — one config change |
| `.env` file | **Secret Manager** + env vars | Secrets in Secret Manager, config in env |
| `logs/` directory | **Cloud Logging** | Structured JSON logging |
| Print statements | **Cloud Monitoring** | Custom metrics + dashboards |
| Manual file upload | **Cloud Functions** + GCS trigger | Event-driven batch initiation |
| `cron` (manual) | **Cloud Scheduler** | Scheduled batch runs |
| `docker build` | **Cloud Build** + Artifact Registry | CI/CD pipeline |

---

## Appendix B: Capacity Planning

### B.1 5M Rows/Day Scenario

| Resource | Calculation | Result |
|----------|------------|--------|
| **Dataflow workers** | 750K LLM rows ÷ (3,600s/h × 4h window) ÷ 1 row/1.5s per worker | ~35 workers (use 50 for headroom) |
| **Vertex AI RPM** | 750K rows × 2 calls/row ÷ (4h × 60min) = 6,250 RPM | Request 10,000 RPM quota |
| **BigQuery storage** | 5M rows × ~500 bytes/row × 365 days = ~912 GB/year (staging); production similar | ~2 TB total/year (90-day retention on staging reduces to ~450 GB active) |
| **BigQuery queries** | Looker Studio review (~10 queries/day × ~1 GB scanned) + promote job (~1 GB) | ~11 GB/day; ~330 GB/month ($1.65/month at $5/TB) |
| **Redis memory** | ~100K cached entries × ~1 KB avg = ~100 MB | 4 GB instance (ample headroom) |
| **GCS storage** | 5M rows × ~200 bytes × 30 days = ~30 GB (input files) | Minimal cost |
| **Network egress** | Vertex AI responses: 750K × 500 bytes = ~375 MB/day | Negligible |

### B.2 Growth Path

| Scale | Dataflow Workers | Vertex AI RPM | BigQuery Storage (annual) | Monthly Cost |
|-------|-----------------|---------------|---------------------------|-------------|
| 1M rows/day | 10 | 2,000 | ~400 GB | ~$2,270 |
| 5M rows/day | 50 | 10,000 | ~2 TB | ~$5,115 |
| 20M rows/day | 200 | 40,000 | ~8 TB | ~$17,070 |
| 50M rows/day | 500 | 100,000 | ~20 TB | ~$38,070 |

---

*Document prepared for Architecture Approval Forum. For pipeline internals, see [DESIGN_V3.2.md](./DESIGN_V3.2.md). For stakeholder overview, see [EXECUTIVE_SUMMARY.md](./EXECUTIVE_SUMMARY.md).*
