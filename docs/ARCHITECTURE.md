# Architecture — Smart Resume Screening System

> **Version:** Stage 1 — Foundation Complete
> **Stack:** Python 3.11 · Flask 3.x · PostgreSQL 15 · SQLAlchemy 2.0 · FAISS · Google Gemini API

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Technology Stack](#technology-stack)
3. [Backend Folder Structure](#backend-folder-structure)
4. [Layered Architecture](#layered-architecture)
5. [Request Lifecycle](#request-lifecycle)
6. [Authentication Architecture](#authentication-architecture)
7. [Database Architecture](#database-architecture)
8. [Storage Architecture](#storage-architecture)
9. [AI Pipeline Architecture](#ai-pipeline-architecture)
10. [Design Principles](#design-principles)

---

## System Overview

The Smart Resume Screening System is a production-grade AI-powered recruitment platform serving three user roles — **Candidates**, **Recruiters**, and **Admins** — through a secure REST API backend.

```
┌────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER                            │
│   Web Browser (React/Next.js)  ·  Mobile App  ·  API Client   │
└──────────────────────────┬─────────────────────────────────────┘
                           │ HTTPS + JWT
┌──────────────────────────▼─────────────────────────────────────┐
│                      API GATEWAY                               │
│          Nginx (reverse proxy, TLS termination, rate limit)    │
└──────────────────────────┬─────────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────────┐
│                   FLASK APPLICATION                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐ │
│  │  Auth API   │  │  Core API   │  │      Admin API          │ │
│  │  /auth/*    │  │  /api/v1/*  │  │      /admin/*           │ │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘ │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                 SERVICE LAYER                           │   │
│  │  AuthService · ResumeService · JobService               │   │
│  │  ApplicationService · AIService · NotificationService   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │               DATA ACCESS LAYER                         │   │
│  │          SQLAlchemy ORM + Repository Pattern            │   │
│  └─────────────────────────────────────────────────────────┘   │
└──────────────────────────┬─────────────────────────────────────┘
                           │
           ┌───────────────┼───────────────┐
           │               │               │
┌──────────▼──┐  ┌─────────▼──┐  ┌────────▼──────────┐
│ PostgreSQL  │  │   FAISS    │  │  Local Filesystem  │
│  (primary)  │  │  (vectors) │  │  (uploaded files)  │
└─────────────┘  └────────────┘  └────────────────────┘
                                          │
                           ┌──────────────▼─────────┐
                           │   AI Provider (Gemini)  │
                           │   Google Cloud AI API   │
                           └────────────────────────┘
```

---

## Technology Stack

| Layer | Technology | Version | Justification |
|-------|-----------|---------|---------------|
| Language | Python | 3.11+ | Mature AI ecosystem, type hints, performance improvements |
| Web Framework | Flask | 3.x | Lightweight, flexible, production-proven for REST APIs |
| ORM | SQLAlchemy | 2.0 | Typed ORM with `Mapped`/`mapped_column`, async support |
| Database | PostgreSQL | 15+ | JSONB, GIN indexes, ACID compliance, enterprise proven |
| Migrations | Flask-Migrate (Alembic) | Latest | Incremental, version-controlled schema migrations |
| Auth | Flask-JWT-Extended | Latest | JWT access + refresh tokens, blocklist support |
| Password Hashing | Flask-Bcrypt | Latest | bcrypt with configurable cost factor |
| Email | Flask-Mail | Latest | Pluggable SMTP, compatible with SendGrid/SES |
| CORS | Flask-CORS | Latest | Configurable cross-origin access |
| Vector Store | FAISS | Latest | In-process semantic similarity search |
| AI Provider | Google Gemini API | 1.5+ | Multi-modal LLM for parsing, ranking, summarisation |
| File Storage | Local Filesystem | — | Development; S3/GCS adapter planned |
| Logging | Python `logging` | stdlib | JSON-formatted, configurable per environment |
| Testing | Pytest + pytest-flask | Latest | Unit and integration tests |
| Environment | python-dotenv | Latest | `.env` file loading |

---

## Backend Folder Structure

```
backend/
├── run.py                          # Application entry point
├── .env                            # Runtime secrets (never committed)
├── .env.example                    # Template for required env vars
├── requirements.txt                # Pinned production dependencies
├── requirements-dev.txt            # Development + test dependencies
│
├── app/
│   ├── __init__.py                 # Application factory (create_app)
│   ├── config.py                   # Environment-aware configuration classes
│   ├── extensions.py               # Flask extension instances (db, jwt, mail…)
│   │
│   ├── models/                     # SQLAlchemy 2.0 ORM models
│   │   ├── __init__.py             # Model registry — Alembic discovery
│   │   ├── base.py                 # TimestampMixin, SoftDeleteMixin
│   │   ├── role.py                 # roles table
│   │   ├── user.py                 # users table
│   │   ├── token_blocklist.py      # JWT revocation
│   │   ├── candidate_profile.py    # candidate_profiles table
│   │   ├── recruiter_profile.py    # recruiter_profiles table
│   │   ├── job.py                  # jobs table
│   │   ├── resume.py               # resumes table
│   │   ├── application.py          # applications table
│   │   ├── notification.py         # notifications table
│   │   ├── audit_log.py            # audit_logs table
│   │   ├── ai_processing_job.py    # ai_processing_jobs table
│   │   ├── email_log.py            # email_logs table
│   │   └── system_settings.py      # system_settings table
│   │
│   ├── storage/                    # File storage abstraction
│   │   ├── __init__.py             # Package exports
│   │   └── local_storage.py        # Local filesystem adapter
│   │
│   ├── api/                        # Route blueprints (Stage 2+)
│   │   ├── __init__.py
│   │   ├── v1/
│   │   │   ├── auth/               # /auth/* — register, login, logout
│   │   │   ├── candidates/         # /api/v1/candidates/*
│   │   │   ├── recruiters/         # /api/v1/recruiters/*
│   │   │   ├── jobs/               # /api/v1/jobs/*
│   │   │   ├── resumes/            # /api/v1/resumes/*
│   │   │   └── applications/       # /api/v1/applications/*
│   │   └── admin/                  # /admin/* — admin-only endpoints
│   │
│   ├── services/                   # Business logic layer (Stage 2+)
│   │   ├── auth_service.py
│   │   ├── resume_service.py
│   │   ├── job_service.py
│   │   ├── application_service.py
│   │   ├── ai_service.py
│   │   ├── notification_service.py
│   │   └── email_service.py
│   │
│   ├── ai/                         # AI pipeline modules (Stage 3+)
│   │   ├── __init__.py
│   │   ├── parser.py               # Resume text extraction + structured parsing
│   │   ├── embedder.py             # Vector embedding generation
│   │   ├── ranker.py               # Candidate-job match scoring
│   │   ├── explainer.py            # Explainable AI (ranking_reason)
│   │   ├── skill_gap.py            # Skill gap analysis
│   │   ├── interview_gen.py        # Interview question generation
│   │   └── quality_scorer.py       # Resume quality scoring
│   │
│   ├── core/                       # Cross-cutting concerns
│   │   ├── exceptions.py           # Custom exception hierarchy
│   │   ├── logger.py               # Logging configuration
│   │   ├── decorators.py           # Auth + role decorators (Stage 2+)
│   │   └── pagination.py           # Cursor/offset pagination (Stage 2+)
│   │
│   └── utils/                      # Stateless helper utilities (Stage 2+)
│       ├── validators.py
│       ├── serializers.py
│       └── date_helpers.py
│
├── migrations/                     # Alembic migration files (auto-generated)
│   ├── env.py
│   └── versions/
│
└── tests/                          # Test suite (Stage 2+)
    ├── conftest.py
    ├── unit/
    └── integration/
```

---

## Layered Architecture

The backend follows a strict **4-layer architecture** where each layer only communicates with the layer immediately below it.

```
┌─────────────────────────────────────────────────────────────┐
│                    PRESENTATION LAYER                       │
│                  (app/api/  — Blueprints)                   │
│                                                             │
│  Responsibilities:                                          │
│  • Parse and validate HTTP requests                         │
│  • Enforce authentication and role-based access             │
│  • Call the service layer                                   │
│  • Format and return HTTP responses                         │
│  • Map service exceptions to HTTP status codes             │
│                                                             │
│  Must NOT: contain business logic, touch the DB directly,   │
│            or call the AI pipeline directly                 │
└─────────────────────────┬───────────────────────────────────┘
                          │ calls
┌─────────────────────────▼───────────────────────────────────┐
│                    SERVICE LAYER                             │
│                  (app/services/  — Services)                │
│                                                             │
│  Responsibilities:                                          │
│  • Implement all business rules and workflows               │
│  • Coordinate between models and AI modules                 │
│  • Manage database transactions                             │
│  • Create audit log entries and notifications               │
│  • Queue async AI jobs via AIProcessingJob model            │
│                                                             │
│  Must NOT: render HTTP responses, access request context    │
└─────────────────────────┬───────────────────────────────────┘
                          │ calls
┌─────────────────────────▼───────────────────────────────────┐
│                  DATA ACCESS LAYER                          │
│           (app/models/  — SQLAlchemy ORM models)            │
│                                                             │
│  Responsibilities:                                          │
│  • Define table schemas and relationships                   │
│  • Implement domain-pure helper methods                     │
│  • Provide consistent serialization (to_dict, etc.)        │
│  • Enforce model-level constraints and invariants           │
│                                                             │
│  Must NOT: contain business logic, call services,           │
│            make HTTP requests, or use Flask context         │
└─────────────────────────┬───────────────────────────────────┘
                          │ reads/writes
┌─────────────────────────▼───────────────────────────────────┐
│                  INFRASTRUCTURE LAYER                        │
│   PostgreSQL · FAISS · Local Filesystem · Gemini API        │
│                                                             │
│  Accessed through: SQLAlchemy, FAISS Python API,            │
│  LocalStorage adapter, google-generativeai SDK              │
└─────────────────────────────────────────────────────────────┘
```

### Helper Modules (Cross-Layer)

| Module | Layer | Consumed By |
|--------|-------|-------------|
| `app/core/exceptions.py` | Cross-cutting | All layers |
| `app/core/logger.py` | Cross-cutting | All layers |
| `app/core/decorators.py` | Presentation | API blueprints |
| `app/storage/` | Infrastructure | Service layer |
| `app/ai/` | Infrastructure | Service layer |

---

## Request Lifecycle

A complete authenticated API request flows through the following stages:

```
Client
  │
  ▼ HTTPS Request
Nginx (reverse proxy)
  │ • TLS termination
  │ • Rate limiting
  │ • Request logging
  ▼
Flask Application
  │
  ├─ 1. BEFORE_REQUEST hooks
  │     • Load JWT identity (@jwt_required)
  │     • Check token blocklist (TokenBlocklist.is_jti_blocklisted)
  │     • CORS validation
  │
  ├─ 2. BLUEPRINT ROUTE HANDLER (api/v1/)
  │     • Input validation (JSON schema / marshmallow)
  │     • Role check (@recruiter_required / @candidate_required)
  │     • Extract validated data
  │
  ├─ 3. SERVICE LAYER (services/)
  │     • Business rule enforcement
  │     • Cross-model coordination
  │     • Database transaction management
  │     • AI job queuing (AIProcessingJob.create)
  │     • Audit log entry (AuditLog.log)
  │     • Notification creation
  │
  ├─ 4. MODEL LAYER (models/)
  │     • SQLAlchemy queries via db.session
  │     • Relationship loading
  │     • Computed properties (status_label, is_terminal…)
  │     • Serialization (.to_dict(), .to_public_dict()…)
  │
  ├─ 5. AFTER_REQUEST hooks
  │     • Response headers (security, caching)
  │     • Structured logging
  │
  └─ 6. JSON Response → Client
       • Consistent envelope: { success, data, message, meta }
       • HTTP status code
       • Pagination metadata (where applicable)
```

### Error Handling

All exceptions are caught by global error handlers registered in the application factory:

```
AppException (base)
  ├── ValidationError    → 422 Unprocessable Entity
  ├── AuthenticationError → 401 Unauthorized
  ├── AuthorizationError  → 403 Forbidden
  ├── NotFoundError       → 404 Not Found
  ├── ConflictError       → 409 Conflict
  └── StorageError        → 413 / 422 (file validation)

Uncaught exceptions   → 500 Internal Server Error + error logged
```

---

## Authentication Architecture

The system uses **JWT-based stateless authentication** with server-side revocation via the `token_blocklist` table.

```
Registration Flow:
  POST /auth/register
    → validate input
    → create User (is_verified=FALSE)
    → create CandidateProfile or RecruiterProfile
    → generate verification_token (32-byte URL-safe)
    → send verification email (EmailLog created)
    → AuditLog.log(AUTH_REGISTER)
    → return 201 (no tokens yet)

Email Verification Flow:
  GET /auth/verify-email?token=<token>
    → User.verify_email(token) — checks expiry + timing-safe compare
    → is_verified = TRUE, token fields cleared
    → send welcome email
    → return 200

Login Flow:
  POST /auth/login
    → User.check_password(password) — bcrypt verify
    → User.can_login (4-condition gate)
    → create access_token (15min) + refresh_token (30 days)
    → User.record_login() — updates last_login
    → AuditLog.log(AUTH_LOGIN)
    → return { access_token, refresh_token, user }

Authenticated Request Flow:
  Authorization: Bearer <access_token>
    → JWT decode (Flask-JWT-Extended)
    → TokenBlocklist.is_jti_blocklisted(jti) — revocation check
    → load User from sub claim
    → User.can_login — session validity
    → proceed to route handler

Token Refresh Flow:
  POST /auth/refresh (Authorization: Bearer <refresh_token>)
    → validate refresh token
    → revoke old refresh token (blocklist)
    → issue new access_token + refresh_token (rotation)
    → return new tokens

Logout Flow:
  POST /auth/logout
    → add access_token JTI to token_blocklist
    → add refresh_token JTI to token_blocklist
    → AuditLog.log(AUTH_LOGOUT)
    → return 200
```

### Role-Based Access Control

Three roles with distinct permissions:

| Endpoint Group | candidate | recruiter | admin |
|----------------|-----------|-----------|-------|
| `POST /auth/*` | ✅ | ✅ | ✅ |
| `GET /jobs` | ✅ | ✅ | ✅ |
| `POST /applications` | ✅ | ❌ | ✅ |
| `GET /applications/{id}/interview-questions` | ❌ | ✅ | ✅ |
| `POST /jobs` | ❌ | ✅ | ✅ |
| `GET /applications` (full list for a job) | ❌ | ✅ | ✅ |
| `GET /admin/*` | ❌ | ❌ | ✅ |
| `PUT /admin/users/{id}/suspend` | ❌ | ❌ | ✅ |

Role checks are enforced by decorators applied at the blueprint level — never inside service or model code.

---

## Database Architecture

### Database Selection: PostgreSQL

PostgreSQL was chosen for the following capabilities essential to this system:

| Capability | Used For |
|-----------|----------|
| **JSONB** with GIN indexes | AI output storage, skills arrays — queryable without full schema |
| **ACID transactions** | Multi-model writes (application + notification + audit_log in one tx) |
| **Partial indexes** | `WHERE status='queued'` — efficient worker pickup without full table scans |
| **UUID native support** | `uuid-ossp` extension for server-side UUID generation |
| **Full-text search** | Future: job description search without Elasticsearch dependency |

### Key Schema Patterns

**Soft Delete:** Business entities use `deleted_at TIMESTAMPTZ` instead of hard deletes. Queries filter `WHERE deleted_at IS NULL`. Preserves history and prevents FK cascade failures.

**Mixin Inheritance:**
- `TimestampMixin` → `created_at` + `updated_at` (auto-maintained)
- `SoftDeleteMixin` → `deleted_at` + `is_deleted` property + `soft_delete()` + `restore()` methods
- Immutable tables (`audit_logs`, `email_logs`, `ai_processing_jobs`) use neither mixin

**UUID Primary Keys:** All 13 tables use UUID v4 primary keys. Benefits: globally unique (safe for data export/merge), no sequential guessing, compatible with distributed generation.

**Enum Strategy:** Python `str`-mixin enums stored as VARCHAR in PostgreSQL. Example:
```python
class ApplicationStatus(str, enum.Enum):
    APPLIED = "applied"          # stored as 'applied' in VARCHAR(30)
    SHORTLISTED = "shortlisted"  # application.status == "shortlisted" → True
```
This gives Python type safety and IDE autocomplete without PostgreSQL `ENUM` type rigidity (no `ALTER TYPE` needed to add values).

### Migration Strategy

Alembic (Flask-Migrate) manages all schema changes:
```bash
flask --app run.py db migrate -m "Description of change"
flask --app run.py db upgrade        # apply to current DB
flask --app run.py db downgrade -1   # roll back one revision
```

Migrations are version-controlled, reviewed before apply, and tested on a staging database before production.

---

## Storage Architecture

### File Upload Pipeline

```
HTTP Request (multipart/form-data)
  │
  ▼
Flask Route Handler
  │  validates: field name, file present
  ▼
LocalStorage.save_resume(file, user_id)
  │
  ├─ 1. file.read() → bytes
  │
  ├─ 2. SHA-256 hash ──→ duplicate check (UNIQUE(user_id, sha256_hash))
  │
  ├─ 3. MIME detection from bytes (python-magic / libmagic)
  │     ↳ NOT from extension or Content-Type header
  │
  ├─ 4. Validation
  │     • MIME type ∈ allowed set
  │     • file_size_bytes ≤ max_size_bytes
  │
  ├─ 5. Filename sanitization
  │     • Unicode normalize (NFKD)
  │     • Strip non-ASCII, dangerous characters
  │     • UUID prefix for collision-proofing
  │
  ├─ 6. Build path: resumes/{year}/{month}/{user_id}/{uuid}_{name}.{ext}
  │
  └─ 7. atomic write to disk
       │
       ▼
  StorageResult { file_path, sha256_hash, mime_type, file_type, file_size_bytes }
       │
       ▼
  Resume model created in PostgreSQL
```

### Storage Path Structure

```
{UPLOAD_FOLDER}/
  resumes/
    2026/
      06/
        {user_uuid}/
          abc123def456_john_smith_cv.pdf
  jd_files/
    2026/
      06/
        {recruiter_uuid}/
          xyz789ghi012_senior_engineer_jd.pdf
```

**Only the relative path** (`resumes/2026/06/{user_id}/{filename}`) is stored in the database. The absolute path is reconstructed at runtime — UPLOAD_FOLDER can change without a DB migration.

### Security Measures

| Threat | Mitigation |
|--------|-----------|
| MIME type spoofing | Content-based detection with libmagic — not extension or Content-Type header |
| Path traversal | `Path.resolve().relative_to(UPLOAD_FOLDER)` — raises on any `../` attempt |
| Malicious filenames | Regex sanitization removes all non-safe characters + UUID prefix |
| File size attacks | Validated against configurable max_size_bytes before write |
| Duplicate uploads | SHA-256 hash checked against `UNIQUE(user_id, sha256_hash)` index |

### Storage Scalability Path

```
Stage 1: LocalStorage (current)
    ↓ swap only the adapter, zero model/service changes
Stage N: CloudStorage (S3 / GCS)
    • Same public interface: save_resume(), delete_file(), get_absolute_path()
    • Add pre-signed URL generation for secure direct downloads
    • CDN integration for fast resume delivery
```

---

## AI Pipeline Architecture

> **Note:** This section describes the high-level AI architecture. Implementation modules are in Stage 3.

### AI Operations Overview

```
Resume Upload
    ▼
[1] Text Extraction (pdf2image + pytesseract / pdfminer)
    ▼
[2] Structured Parsing (Gemini 1.5 Pro)
    → parsed_data JSONB (contact, skills, experience, education…)
    ▼
[3] Quality Scoring (Gemini 1.5 Flash)
    → quality_report JSONB (per-section scores + feedback)
    → quality_score INTEGER (0–100)
    ▼
[4] AI Summary Generation (Gemini 1.5 Flash)
    → ai_summary TEXT (1-paragraph candidate profile)
    ▼
[5] Vector Embedding (text-embedding-004)
    → 768-dimensional vector stored in FAISS index
    → parse_status → 'completed'

Candidate Applies to Job
    ▼
[6] Candidate Ranking (Gemini 1.5 Pro)
    → match_score NUMERIC
    → score_breakdown JSONB (skills_match, experience_match…)
    → ranking_reason JSONB (explainable AI narrative)
    ▼
[7] Skill Gap Analysis (Gemini 1.5 Flash)
    → skill_gap JSONB (matched/missing required + preferred skills)
    ▼
[8] Interview Question Generation (Gemini 1.5 Pro)
    → interview_questions JSONB (technical, behavioral, culture_fit)
```

### Async Processing Model

Operations [1]–[5] and [6]–[8] run asynchronously via the `ai_processing_jobs` queue:

```
API Request → create AIProcessingJob (status=queued) → 200 OK
                  │
                  ▼
             Background Worker polls:
             SELECT ... WHERE status='queued'
             ORDER BY priority ASC, queued_at ASC
                  │
                  ├─ mark_started(worker_id)
                  ├─ execute AI operation
                  ├─ mark_completed(result_data, processing_time_ms)  — on success
                  └─ mark_failed(error_message)                        — on failure
                       └─ if retry_count < max_retries: re-queue with exponential backoff
                       └─ else: status = 'failed' (terminal)
```

### Vector Similarity Search (FAISS)

```
Query (job description / candidate skills)
  ▼
Embed with text-embedding-004 → 768-dim query vector
  ▼
FAISS Index (all active resume embeddings)
  ▼
cosine_similarity(query_vector, resume_vectors)
  ▼
Top-K candidates ordered by similarity score
  ▼
Gemini re-ranking (semantic cross-encoder, detailed scoring)
  ▼
Final ranked candidate list with scores
```

### AI Traceability

Every AI-generated value is traceable via `ai_metadata` JSONB fields on `resumes` and `applications`:

```json
{
  "ranking": {
    "model_name": "gemini-1.5-pro",
    "model_version": "001",
    "processed_at": "2026-06-29T10:00:00Z",
    "processing_time_ms": 3240
  }
}
```

Combined with `last_ai_processed_at` timestamps, this allows:
- Detecting stale scores when job or resume is updated
- Triggering selective re-analysis (not full reprocessing)
- Comparing outputs across model versions (A/B testing)
- Auditing which model version produced each hiring decision

---

## Design Principles

The following principles are consistently applied across all code in this project.

### 1. Separation of Concerns
Each layer has a single, clearly defined responsibility. Business logic lives exclusively in the service layer — never in route handlers or models.

### 2. Fail Loudly, Fail Early
Validation happens at the earliest possible point (input validation in routes, type validation in models). Errors surface immediately with clear messages rather than propagating silently.

### 3. Immutability Where Appropriate
Audit logs and email logs are append-only by design. Updating a compliance record would defeat its purpose. Status machines (`ApplicationStatus`, `AIJobStatus`) use methods that enforce invariants rather than allowing direct field assignment.

### 4. Explainability by Default
AI output is never a black box. Every score comes with `ranking_reason` (narrative), `score_breakdown` (numeric per-category), and `ai_metadata` (model traceability). Candidates can see their skill gaps; recruiters can see the AI's reasoning.

### 5. Defence in Depth for File Uploads
Multiple independent validation layers: MIME detection from content bytes (not extension), size validation, filename sanitization, path traversal prevention. No single bypass point exists.

### 6. Consistent API Surface
All API responses follow a single envelope format. All models follow the same SQLAlchemy 2.0 patterns, same mixin strategy, same serializer method naming. A developer who understands one model understands all of them.

### 7. Migration-Friendly Schema Design
- Python Enums stored as VARCHAR (not PostgreSQL ENUM types) — new values require no `ALTER TYPE`
- JSONB for AI output — schema changes require no column additions
- Soft delete instead of hard delete — avoids FK cascade surprises during iteration

### 8. Security as a First-Class Citizen
- All tokens are cryptographically random (32-byte URL-safe)
- Timing-safe comparison (`secrets.compare_digest`) for token validation
- Password hashing with bcrypt (configurable cost factor)
- JWT revocation via server-side blocklist
- Sensitive settings masked in API responses (`is_sensitive` flag)
- Role enforcement at the route layer, not inside business logic

### 9. Observability Built In
- Structured JSON logging in all environments
- `audit_logs` table for every significant business event
- `ai_metadata` for AI operation traceability
- `email_logs` for email delivery monitoring
- `ai_processing_jobs` for async task observability
- Health check endpoint (`/health`) for infrastructure monitoring

### 10. Scalability Designed From Day One
- Async AI pipeline (queue-based, horizontal worker scaling)
- Storage adapter pattern (LocalStorage → S3 requires no service changes)
- FAISS for in-process vector search (upgradeable to Qdrant/Weaviate for distributed)
- Partial indexes on hot-path tables reduce index bloat
- Denormalised counters (`application_count`) avoid expensive COUNT(*) aggregates
