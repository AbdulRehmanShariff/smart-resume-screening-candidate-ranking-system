# Database Reference — Smart Resume Screening System

> **Last Updated:** Stage 1 Complete (Batch 1A–1F)
> **Database:** PostgreSQL 15+
> **ORM:** SQLAlchemy 2.0 (typed ORM — `Mapped` / `mapped_column`)
> **Migration Tool:** Flask-Migrate (Alembic)

---

## Table of Contents

1. [Schema Overview](#schema-overview)
2. [ER Diagram](#er-diagram)
3. [Table Reference](#table-reference)
4. [Relationship Map](#relationship-map)
5. [JSONB Field Schemas](#jsonb-field-schemas)
6. [Status Lifecycles](#status-lifecycles)
7. [Index Strategy](#index-strategy)
8. [Design Decisions](#design-decisions)

---

## Schema Overview

| Table | JSONB | Soft Delete | Description |
|-------|-------|-------------|-------------|
| `roles` | No | No | User role definitions |
| `users` | No | ✅ | Core user accounts + credentials |
| `token_blocklist` | No | No | Revoked JWT tokens |
| `candidate_profiles` | No | ✅ | Extended profile for candidates |
| `recruiter_profiles` | No | ✅ | Recruiter + company profiles |
| `jobs` | ✅ | ✅ | Job postings by recruiters |
| `resumes` | ✅ | ✅ | Uploaded resume files + AI results |
| `applications` | ✅ | No | Candidate applications + AI scoring |
| `notifications` | No | ✅ | In-app notification feed |
| `audit_logs` | ✅ | No (immutable) | Security + compliance audit trail |
| `ai_processing_jobs` | ✅ | No | Async AI task queue |
| `email_logs` | ✅ | No (immutable) | Email delivery records |
| `system_settings` | ✅ | No | Runtime-configurable settings |

---

## ER Diagram

```mermaid
erDiagram
    roles {
        uuid id PK
        varchar name UK
        varchar display_name
        text description
        timestamp created_at
        timestamp updated_at
    }

    users {
        uuid id PK
        uuid role_id FK
        varchar email UK
        varchar password_hash
        varchar first_name
        varchar last_name
        bool is_active
        bool is_verified
        bool is_suspended
        timestamp last_login
        timestamp created_at
        timestamp updated_at
        timestamp deleted_at
    }

    token_blocklist {
        uuid id PK
        uuid user_id FK
        varchar jti UK
        varchar token_type
        timestamp expires_at
        timestamp created_at
    }

    candidate_profiles {
        uuid id PK
        uuid user_id FK UK
        varchar headline
        text bio
        varchar availability_status
        numeric years_of_experience
        varchar location
        varchar linkedin_url
        varchar github_url
        timestamp created_at
        timestamp updated_at
        timestamp deleted_at
    }

    recruiter_profiles {
        uuid id PK
        uuid user_id FK UK
        varchar company_name
        varchar company_size
        varchar industry
        varchar designation
        bool is_verified_recruiter
        timestamp created_at
        timestamp updated_at
        timestamp deleted_at
    }

    jobs {
        uuid id PK
        uuid recruiter_id FK
        varchar title
        text description
        varchar status
        varchar experience_level
        varchar job_type
        jsonb skills_required
        jsonb nice_to_have_skills
        numeric salary_min
        numeric salary_max
        timestamp application_deadline
        int application_count
        timestamp created_at
        timestamp updated_at
        timestamp deleted_at
    }

    resumes {
        uuid id PK
        uuid user_id FK
        uuid previous_resume_id FK
        varchar file_path
        varchar sha256_hash
        varchar mime_type
        int file_size_bytes
        varchar parse_status
        int version
        bool is_primary
        jsonb parsed_data
        jsonb quality_report
        jsonb ai_metadata
        int quality_score
        timestamp created_at
        timestamp updated_at
        timestamp deleted_at
    }

    applications {
        uuid id PK
        uuid job_id FK
        uuid candidate_id FK
        uuid resume_id FK
        varchar status
        numeric match_score
        int rank
        jsonb score_breakdown
        jsonb ranking_reason
        jsonb skill_gap
        jsonb interview_questions
        jsonb ai_metadata
        timestamp last_ai_processed_at
        text recruiter_notes
        timestamp applied_at
        timestamp status_updated_at
        timestamp created_at
        timestamp updated_at
    }

    notifications {
        uuid id PK
        uuid user_id FK
        varchar title
        text message
        varchar type
        varchar priority
        varchar category
        uuid reference_id
        varchar action_url
        bool is_read
        timestamp read_at
        timestamp created_at
        timestamp deleted_at
    }

    audit_logs {
        uuid id PK
        uuid user_id FK
        varchar action
        varchar entity_type
        uuid entity_id
        text description
        jsonb old_value
        jsonb new_value
        varchar ip_address
        timestamp created_at
    }

    ai_processing_jobs {
        uuid id PK
        varchar job_type
        varchar status
        int priority
        varchar entity_type
        uuid entity_id
        jsonb input_data
        jsonb result_data
        int retry_count
        int max_retries
        timestamp next_retry_at
        varchar model_name
        varchar worker_id
        int processing_time_ms
        timestamp queued_at
        timestamp started_at
        timestamp completed_at
    }

    email_logs {
        uuid id PK
        uuid user_id FK
        varchar recipient_email
        varchar template
        varchar status
        varchar provider_message_id
        jsonb metadata
        int retry_count
        timestamp sent_at
        timestamp delivered_at
        timestamp opened_at
        timestamp created_at
    }

    system_settings {
        uuid id PK
        varchar key UK
        varchar category
        jsonb value
        varchar value_type
        varchar display_name
        bool is_public
        bool is_sensitive
        uuid updated_by FK
        timestamp created_at
        timestamp updated_at
    }

    roles ||--o{ users : "has many"
    users ||--o| candidate_profiles : "has one"
    users ||--o| recruiter_profiles : "has one"
    users ||--o{ token_blocklist : "revokes"
    users ||--o{ resumes : "uploads"
    users ||--o{ jobs : "posts"
    users ||--o{ applications : "submits"
    users ||--o{ notifications : "receives"
    users ||--o{ audit_logs : "generates"
    users ||--o{ email_logs : "receives"
    users ||--o{ system_settings : "updates"
    jobs ||--o{ applications : "receives"
    resumes ||--o{ applications : "used in"
    resumes ||--o| resumes : "previous version"
```

---

## Table Reference

---

### `roles`

**Purpose:** Stores the three role definitions that govern access control. Seeded at deployment — not created by users.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `name` | VARCHAR(50) | No | `candidate`, `recruiter`, `admin` (unique) |
| `display_name` | VARCHAR(100) | No | Human-readable label |
| `description` | TEXT | Yes | Role description |
| `created_at` | TIMESTAMPTZ | No | Creation time |
| `updated_at` | TIMESTAMPTZ | No | Last update |

**Constraints:** `UNIQUE(name)`, `CHECK name IN ('candidate', 'recruiter', 'admin')`

---

### `users`

**Purpose:** Central identity table for all user types. Stores credentials and account state. Role-specific profile data lives in separate extension tables.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `role_id` | UUID FK→roles | No | Assigned role (RESTRICT on delete) |
| `email` | VARCHAR(255) | No | Login identifier (unique) |
| `password_hash` | VARCHAR(255) | No | Bcrypt hash — never exposed in API |
| `first_name` | VARCHAR(100) | No | Given name |
| `last_name` | VARCHAR(100) | No | Family name |
| `phone` | VARCHAR(20) | Yes | Optional phone number |
| `is_active` | BOOLEAN | No | `TRUE` — System-level account toggle |
| `is_verified` | BOOLEAN | No | `FALSE` — Email verification flag |
| `is_suspended` | BOOLEAN | No | `FALSE` — Admin-imposed suspension |
| `verification_token` | VARCHAR(255) | Yes | Single-use, cleared after use |
| `verification_token_expiry` | TIMESTAMPTZ | Yes | Valid for 24 hours |
| `reset_password_token` | VARCHAR(255) | Yes | Single-use, cleared after use |
| `reset_token_expiry` | TIMESTAMPTZ | Yes | Valid for 1 hour |
| `last_login` | TIMESTAMPTZ | Yes | Most recent successful login |
| `created_at` | TIMESTAMPTZ | No | Account creation |
| `updated_at` | TIMESTAMPTZ | No | Last modification |
| `deleted_at` | TIMESTAMPTZ | Yes | Soft delete timestamp |

**Login Gate:** `is_active=TRUE AND is_verified=TRUE AND is_suspended=FALSE AND deleted_at IS NULL`

**Indexes:** PK, `UNIQUE(email)`, `ix_users_role_id`, `ix_users_is_active_suspended`

---

### `token_blocklist`

**Purpose:** JWT revocation. Checked on every authenticated request. Expired tokens are purged by a scheduled cleanup task.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `user_id` | UUID FK→users | Yes | Token owner (SET NULL on delete) |
| `jti` | VARCHAR(36) | No | JWT ID claim (unique) |
| `token_type` | VARCHAR(20) | No | `access` or `refresh` |
| `expires_at` | TIMESTAMPTZ | No | Token expiry — used for cleanup |
| `created_at` | TIMESTAMPTZ | No | Revocation timestamp |

**Indexes:** PK, `UNIQUE(jti)`, `ix_token_blocklist_user_id`, `ix_token_blocklist_expires_at`

---

### `candidate_profiles`

**Purpose:** One-to-one extension of `users` for candidates. All manually-maintained profile fields.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `user_id` | UUID FK→users | No | One profile per candidate (unique) |
| `headline` | VARCHAR(255) | Yes | Professional headline |
| `bio` | TEXT | Yes | About-me paragraph |
| `availability_status` | VARCHAR(20) | Yes | `immediately`, `open`, `passive`, `not_looking` |
| `years_of_experience` | NUMERIC(4,1) | Yes | Total years (≥ 0) |
| `current_job_title` | VARCHAR(150) | Yes | Current or last title |
| `current_company` | VARCHAR(150) | Yes | Current or last employer |
| `location` | VARCHAR(150) | Yes | City, Country |
| `website_url` | VARCHAR(500) | Yes | Personal website |
| `linkedin_url` | VARCHAR(500) | Yes | LinkedIn profile |
| `github_url` | VARCHAR(500) | Yes | GitHub profile |
| `portfolio_url` | VARCHAR(500) | Yes | Portfolio URL |
| `created_at` | TIMESTAMPTZ | No | — |
| `updated_at` | TIMESTAMPTZ | No | — |
| `deleted_at` | TIMESTAMPTZ | Yes | Soft delete |

**Constraints:** `UNIQUE(user_id)`, `CHECK years_of_experience >= 0`

**Computed Property:** `profile_completeness` (0–80; 20 reserved for having a primary resume)

---

### `recruiter_profiles`

**Purpose:** One-to-one extension of `users` for recruiters. Company info shown on public job postings.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `user_id` | UUID FK→users | No | One profile per recruiter (unique) |
| `company_name` | VARCHAR(255) | Yes | Employer company |
| `company_website` | VARCHAR(500) | Yes | Company URL |
| `company_size` | VARCHAR(20) | Yes | `1_10` → `1001_plus` |
| `industry` | VARCHAR(150) | Yes | Industry/sector |
| `company_description` | TEXT | Yes | About the company |
| `designation` | VARCHAR(150) | Yes | Recruiter's title |
| `department` | VARCHAR(100) | Yes | HR / Talent / Engineering |
| `phone_number` | VARCHAR(20) | Yes | Work phone |
| `linkedin_url` | VARCHAR(500) | Yes | Recruiter's LinkedIn |
| `company_logo_url` | VARCHAR(500) | Yes | Logo CDN URL |
| `is_verified_recruiter` | BOOLEAN | No | `FALSE` — Admin-verified flag |
| `created_at` | TIMESTAMPTZ | No | — |
| `updated_at` | TIMESTAMPTZ | No | — |
| `deleted_at` | TIMESTAMPTZ | Yes | Soft delete |

**Constraints:** `UNIQUE(user_id)`

---

### `jobs`

**Purpose:** Job postings with 5-state status lifecycle, dual JSONB skill arrays, and denormalised counters.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `recruiter_id` | UUID FK→users | No | Posting recruiter (CASCADE) |
| `title` | VARCHAR(255) | No | Job title |
| `description` | TEXT | No | Full job description |
| `location` | VARCHAR(255) | Yes | City/Country or 'Remote' |
| `job_type` | VARCHAR(30) | No | `full_time`, `part_time`, `contract`, `internship`, `freelance` |
| `experience_level` | VARCHAR(20) | Yes | `intern`, `fresher`, `junior`, `mid`, `senior`, `lead` |
| `status` | VARCHAR(20) | No | `draft` — see lifecycle |
| `salary_min` | NUMERIC(12,2) | Yes | Min salary (≥ 0) |
| `salary_max` | NUMERIC(12,2) | Yes | Max salary (≥ min) |
| `salary_currency` | VARCHAR(3) | No | `USD` — ISO 4217 code |
| `skills_required` | JSONB | Yes | Required skills array (GIN indexed) |
| `nice_to_have_skills` | JSONB | Yes | Preferred skills array (GIN indexed) |
| `requirements` | TEXT | Yes | Detailed requirements text |
| `benefits` | TEXT | Yes | Perks and benefits |
| `application_deadline` | TIMESTAMPTZ | Yes | Application closing date |
| `application_count` | INTEGER | No | `0` — Denormalised counter |
| `shortlisted_count` | INTEGER | No | `0` — Denormalised counter |
| `created_at` | TIMESTAMPTZ | No | — |
| `updated_at` | TIMESTAMPTZ | No | — |
| `deleted_at` | TIMESTAMPTZ | Yes | Soft delete |

**Indexes:** PK, `ix_jobs_recruiter_id`, `ix_jobs_status` (partial: `WHERE deleted_at IS NULL`), `ix_jobs_skills_required` (GIN), `ix_jobs_nice_to_have_skills` (GIN)

---

### `resumes`

**Purpose:** Uploaded resume files with full AI parse lifecycle, versioning chain, and duplicate detection.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `user_id` | UUID FK→users | No | Owning candidate (CASCADE) |
| `previous_resume_id` | UUID FK→resumes | Yes | Previous version in chain |
| `original_filename` | VARCHAR(255) | No | Uploaded filename |
| `file_path` | VARCHAR(1000) | No | Relative path from UPLOAD_FOLDER |
| `sha256_hash` | VARCHAR(64) | No | SHA-256 of file bytes |
| `mime_type` | VARCHAR(100) | No | Detected MIME type |
| `file_size_bytes` | INTEGER | No | File size in bytes (> 0) |
| `file_type` | VARCHAR(20) | No | `pdf`, `docx`, `txt`, `image` |
| `parse_status` | VARCHAR(30) | No | `uploaded` — see lifecycle |
| `version` | INTEGER | No | `1` — version number in chain |
| `is_primary` | BOOLEAN | No | `FALSE` — currently selected |
| `parsed_data` | JSONB | Yes | Structured resume content |
| `quality_report` | JSONB | Yes | Per-section quality scores |
| `ai_metadata` | JSONB | Yes | Model traceability per operation |
| `quality_score` | INTEGER | Yes | 0–100 overall quality |
| `ai_summary` | TEXT | Yes | AI-generated candidate summary |
| `last_parsed_at` | TIMESTAMPTZ | Yes | Most recent parse completion |
| `created_at` | TIMESTAMPTZ | No | Upload time |
| `updated_at` | TIMESTAMPTZ | No | — |
| `deleted_at` | TIMESTAMPTZ | Yes | Soft delete |

**Constraints:** `UNIQUE(user_id, sha256_hash)`, `CHECK file_size_bytes > 0`, `CHECK version >= 1`, `CHECK quality_score IN [0,100]`

**Indexes:** PK, `uq_resumes_user_sha256`, `ix_resumes_user_id`, `ix_resumes_parse_status`, `ix_resumes_is_primary` (partial: `WHERE is_primary=TRUE AND deleted_at IS NULL`)

---

### `applications`

**Purpose:** Central transactional record linking candidate, job, and resume. Stores 13-state lifecycle and all AI scoring output. No soft delete — use WITHDRAWN/REJECTED statuses.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `job_id` | UUID FK→jobs | No | Applied job (CASCADE) |
| `candidate_id` | UUID FK→users | No | Applying candidate (CASCADE) |
| `resume_id` | UUID FK→resumes | No | Submitted resume (RESTRICT) |
| `status` | VARCHAR(30) | No | `applied` — see lifecycle |
| `status_updated_at` | TIMESTAMPTZ | Yes | Last status change time |
| `applied_at` | TIMESTAMPTZ | No | Application submission time |
| `match_score` | NUMERIC(5,2) | Yes | AI overall score 0.00–100.00 |
| `rank` | INTEGER | Yes | Rank within job pool (≥ 1) |
| `score_breakdown` | JSONB | Yes | Per-category numeric scores |
| `ranking_reason` | JSONB | Yes | Explainable AI narrative |
| `skill_gap` | JSONB | Yes | Required/preferred skill gaps |
| `interview_questions` | JSONB | Yes | AI-generated questions |
| `ai_metadata` | JSONB | Yes | Model traceability per operation |
| `last_ai_processed_at` | TIMESTAMPTZ | Yes | Most recent AI run |
| `recruiter_notes` | TEXT | Yes | Private recruiter notes |
| `created_at` | TIMESTAMPTZ | No | — |
| `updated_at` | TIMESTAMPTZ | No | — |

**Constraints:** `UNIQUE(job_id, candidate_id)`, `CHECK match_score IN [0,100]`, `CHECK rank >= 1`

**Indexes:** PK, `uq_applications_job_candidate`, `ix_applications_job_score`, `ix_applications_candidate_created`, `ix_applications_job_status`, `ix_applications_last_ai_processed_at`

---

### `notifications`

**Purpose:** In-app notification feed with priority levels, dual read tracking, soft deletion, and polymorphic references.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `user_id` | UUID FK→users | No | Recipient (CASCADE) |
| `title` | VARCHAR(255) | No | Short notification heading |
| `message` | TEXT | No | Full notification text |
| `type` | VARCHAR(20) | No | `info`, `success`, `warning`, `alert` |
| `priority` | VARCHAR(20) | No | `low`, `normal`, `high`, `critical` |
| `category` | VARCHAR(50) | Yes | `application_update`, `job_match`, `interview`, `offer`, `system`, `account` |
| `reference_id` | UUID | Yes | Triggering entity UUID |
| `reference_type` | VARCHAR(50) | Yes | Triggering entity type |
| `action_url` | VARCHAR(500) | Yes | Frontend deep-link URL |
| `is_read` | BOOLEAN | No | `FALSE` — indexed for badge count |
| `read_at` | TIMESTAMPTZ | Yes | Precise read timestamp |
| `created_at` | TIMESTAMPTZ | No | — |
| `updated_at` | TIMESTAMPTZ | No | — |
| `deleted_at` | TIMESTAMPTZ | Yes | Dismissed (soft delete) |

**Indexes:** PK, `ix_notifications_user_id_is_read`, `ix_notifications_user_id_created_at`, `ix_notifications_user_priority_read`, `ix_notifications_reference`

---

### `audit_logs`

**Purpose:** Immutable, append-only security and compliance trail. Rows are never modified or deleted. Uses `AuditAction` dot-namespaced constants.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `user_id` | UUID FK→users | Yes | Actor — NULL for system events (SET NULL) |
| `action` | VARCHAR(100) | No | Dot-namespaced: `auth.login`, `job.published` |
| `entity_type` | VARCHAR(50) | Yes | Affected entity type |
| `entity_id` | UUID | Yes | Affected entity UUID |
| `description` | TEXT | Yes | Human-readable event summary |
| `old_value` | JSONB | Yes | Changed fields before action |
| `new_value` | JSONB | Yes | Changed fields after action |
| `ip_address` | VARCHAR(45) | Yes | IPv4 or IPv6 (up to 45 chars) |
| `user_agent` | TEXT | Yes | HTTP User-Agent header |
| `created_at` | TIMESTAMPTZ | No | Immutable event timestamp |

**Note:** No `updated_at` — modifying an audit record defeats compliance.

**Indexes:** PK, `ix_audit_logs_user_id_created_at`, `ix_audit_logs_action_created_at`, `ix_audit_logs_entity_type_entity_id`, `ix_audit_logs_created_at`

---

### `ai_processing_jobs`

**Purpose:** Persistent async task queue for AI operations. Workers poll this table, claim jobs, execute them, and update results. Implements exponential backoff retry.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `job_type` | VARCHAR(50) | No | `resume_parse`, `application_rank`, `skill_gap_analyze`, etc. |
| `status` | VARCHAR(20) | No | `queued` — see lifecycle |
| `priority` | INTEGER | No | `5` — 1–10 (lower = higher priority) |
| `entity_type` | VARCHAR(50) | No | Target entity type |
| `entity_id` | UUID | No | Target entity UUID |
| `input_data` | JSONB | Yes | Job-specific parameters |
| `result_data` | JSONB | Yes | AI operation result |
| `error_message` | TEXT | Yes | Last failure reason |
| `error_traceback` | TEXT | Yes | Full Python traceback |
| `retry_count` | INTEGER | No | `0` — Failed attempts |
| `max_retries` | INTEGER | No | `3` — Max allowed |
| `next_retry_at` | TIMESTAMPTZ | Yes | Next eligible retry time |
| `model_name` | VARCHAR(100) | Yes | AI model override |
| `model_version` | VARCHAR(50) | Yes | Model version |
| `worker_id` | VARCHAR(100) | Yes | Claiming worker identifier |
| `processing_time_ms` | INTEGER | Yes | AI call duration |
| `queued_at` | TIMESTAMPTZ | No | Job creation time |
| `started_at` | TIMESTAMPTZ | Yes | Worker claim time |
| `completed_at` | TIMESTAMPTZ | Yes | Terminal state time |

**Constraints:** `CHECK priority BETWEEN 1 AND 10`, `CHECK retry_count >= 0`

**Indexes:** PK, `ix_ai_jobs_worker_pickup` (partial: `WHERE status='queued'`), `ix_ai_jobs_entity_type_entity_id`, `ix_ai_jobs_next_retry_at` (partial)

---

### `email_logs`

**Purpose:** Immutable email delivery audit trail. Updated by provider webhook callbacks as delivery status progresses. Enables bounce detection and suppression list management.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `user_id` | UUID FK→users | Yes | Recipient user (SET NULL on delete) |
| `recipient_email` | VARCHAR(255) | No | Actual delivery address |
| `template` | VARCHAR(100) | No | Template identifier |
| `subject` | VARCHAR(500) | No | Rendered subject line |
| `status` | VARCHAR(20) | No | `pending` — see lifecycle |
| `reference_id` | UUID | Yes | Triggering entity UUID |
| `reference_type` | VARCHAR(50) | Yes | Triggering entity type |
| `provider_message_id` | VARCHAR(255) | Yes | Provider ID for webhook correlation |
| `metadata` | JSONB | Yes | Template vars + provider response |
| `error_message` | TEXT | Yes | Send failure description |
| `retry_count` | INTEGER | No | `0` — Send retry count |
| `sent_at` | TIMESTAMPTZ | Yes | Accepted by provider |
| `delivered_at` | TIMESTAMPTZ | Yes | Confirmed delivery |
| `opened_at` | TIMESTAMPTZ | Yes | Recipient opened |
| `created_at` | TIMESTAMPTZ | No | Send requested time |

**Indexes:** PK, `ix_email_logs_user_id_created_at`, `ix_email_logs_recipient_email`, `ix_email_logs_status_created_at`, `ix_email_logs_provider_message_id`

---

### `system_settings`

**Purpose:** Runtime-configurable key-value store. Allows admins to change AI models, upload limits, and feature flags without code changes.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | No | Primary key |
| `key` | VARCHAR(100) | No | Dot-namespaced key (unique): `ai.model_name` |
| `category` | VARCHAR(50) | No | `ai`, `storage`, `email`, `security`, `platform` |
| `value` | JSONB | Yes | Setting value (any JSON type) |
| `value_type` | VARCHAR(20) | No | `string`, `integer`, `float`, `boolean`, `json`, `list` |
| `display_name` | VARCHAR(255) | No | Admin panel label |
| `description` | TEXT | Yes | Help text |
| `is_public` | BOOLEAN | No | `FALSE` — accessible without auth |
| `is_sensitive` | BOOLEAN | No | `FALSE` — masked in API responses |
| `updated_by` | UUID FK→users | Yes | Last admin to modify (SET NULL) |
| `created_at` | TIMESTAMPTZ | No | — |
| `updated_at` | TIMESTAMPTZ | No | — |

**Indexes:** PK, `UNIQUE(key)`, `ix_system_settings_category`, `ix_system_settings_is_public`

---

## Relationship Map

```
roles ──────────────── users (1:N via role_id)
                         │
          ┌──────────────┼──────────────────────────────┐
          │              │                              │
  candidate_profiles  recruiter_profiles          token_blocklist
  (1:1, unique FK)    (1:1, unique FK)            (1:N)
                         │
          ┌──────────────┼──────────────────────────────┐
          │              │              │               │
       resumes          jobs      notifications    audit_logs
       (1:N)            (1:N via  (1:N)            (1:N, nullable FK)
         │              recruiter_id)
         │              │
         └──── applications (N:M — UNIQUE(job_id, candidate_id))
                         │
                    email_logs (1:N, nullable FK)
                    system_settings (1:N via updated_by)

ai_processing_jobs — no FK (polymorphic: entity_type + entity_id)
```

---

## JSONB Field Schemas

### `jobs.skills_required` / `jobs.nice_to_have_skills`

```json
["Python", "Flask", "PostgreSQL", "Docker", "REST APIs"]
```

Flat JSON array, GIN-indexed for `@>` containment queries.

---

### `resumes.parsed_data`

```json
{
  "contact": {
    "name": "John Smith",
    "email": "john@example.com",
    "phone": "+1-555-0100",
    "location": "San Francisco, CA",
    "linkedin": "https://linkedin.com/in/johnsmith",
    "github": "https://github.com/johnsmith"
  },
  "summary": "Experienced backend engineer with 6 years of Python expertise...",
  "skills": ["Python", "Flask", "PostgreSQL", "Docker", "Redis"],
  "experience": [
    {
      "company": "Acme Corp",
      "title": "Senior Backend Engineer",
      "location": "Remote",
      "start_date": "2020-03",
      "end_date": "2023-12",
      "is_current": false,
      "responsibilities": [
        "Led migration of monolith to microservices",
        "Reduced API latency by 40% through query optimisation"
      ],
      "technologies": ["Python", "Flask", "PostgreSQL", "AWS"]
    }
  ],
  "education": [
    {
      "institution": "MIT",
      "degree": "B.Sc. Computer Science",
      "field": "Computer Science",
      "start_date": "2016",
      "end_date": "2020",
      "gpa": "3.8"
    }
  ],
  "certifications": [
    {
      "name": "AWS Solutions Architect — Associate",
      "issuer": "Amazon Web Services",
      "date": "2022-06",
      "expiry": "2025-06"
    }
  ],
  "projects": [
    {
      "name": "FastAPI OpenAPI Generator",
      "description": "Open-source tool for generating type-safe API clients",
      "technologies": ["Python", "FastAPI"],
      "url": "https://github.com/johnsmith/oag"
    }
  ],
  "languages": ["English (Native)", "Spanish (Intermediate)"]
}
```

---

### `resumes.quality_report`

```json
{
  "formatting": {
    "score": 85,
    "feedback": "Clear structure with consistent bullet points"
  },
  "contact_info": {
    "score": 100,
    "feedback": "All required contact fields are present"
  },
  "experience_detail": {
    "score": 75,
    "feedback": "Could add more quantifiable achievements"
  },
  "skills_clarity": {
    "score": 90,
    "feedback": "Skills clearly listed and well categorised"
  },
  "quantifiable_achievements": {
    "score": 60,
    "feedback": "Only 2 metrics mentioned — aim for at least 5"
  },
  "grammar": {
    "score": 95,
    "feedback": "No grammatical errors detected"
  },
  "overall_score": 84,
  "overall_feedback": "Strong resume overall. Focus on adding measurable impact statements."
}
```

---

### `resumes.ai_metadata`

```json
{
  "parsing": {
    "model_name": "gemini-1.5-pro",
    "model_version": "001",
    "processed_at": "2026-06-15T10:30:00Z",
    "processing_time_ms": 2341,
    "token_count": 1842
  },
  "quality_scoring": {
    "model_name": "gemini-1.5-flash",
    "model_version": "001",
    "processed_at": "2026-06-15T10:30:05Z",
    "processing_time_ms": 1120
  },
  "embedding": {
    "model_name": "text-embedding-004",
    "dimensions": 768,
    "processed_at": "2026-06-15T10:30:08Z",
    "processing_time_ms": 450
  }
}
```

---

### `applications.score_breakdown`

```json
{
  "overall": 87.5,
  "skills_match": 90.0,
  "experience_match": 85.0,
  "education_match": 75.0,
  "keyword_overlap": 92.0,
  "semantic_similarity": 88.0
}
```

---

### `applications.ranking_reason` (Explainable AI)

```json
{
  "summary": "Strong candidate matching 8 of 10 required skills with 5+ years Python experience.",
  "strengths": [
    "5+ years Python experience aligns with the senior requirement",
    "Docker and Kubernetes experience covers all required DevOps skills",
    "Previous fintech experience is directly relevant"
  ],
  "areas_for_development": [
    "GraphQL not mentioned (nice-to-have)",
    "Leadership experience not documented in resume"
  ],
  "recommendation": "Highly recommended for technical interview"
}
```

---

### `applications.skill_gap`

```json
{
  "matched_required": ["Python", "Flask", "PostgreSQL", "REST APIs"],
  "missing_required": ["Docker"],
  "matched_preferred": ["Redis", "Celery"],
  "missing_preferred": ["Kubernetes", "Terraform", "GraphQL"]
}
```

---

### `applications.interview_questions`

```json
{
  "technical": [
    {
      "question": "Walk through a time you optimised a slow SQL query.",
      "rationale": "PostgreSQL listed but no specifics on query tuning",
      "difficulty": "medium"
    }
  ],
  "behavioral": [
    {
      "question": "Describe a time you delivered a project under a tight deadline.",
      "rationale": "Senior role requires strong delivery track record",
      "difficulty": "easy"
    }
  ],
  "culture_fit": [
    {
      "question": "How do you approach async collaboration in a distributed team?",
      "rationale": "The position is fully remote"
    }
  ]
}
```

---

### `system_settings.value` Examples

| `key` | `value_type` | `value` |
|-------|-------------|---------|
| `ai.model_name` | `string` | `"gemini-1.5-pro"` |
| `ai.temperature` | `float` | `0.7` |
| `storage.max_upload_mb` | `integer` | `10` |
| `storage.allowed_types` | `list` | `["pdf","docx","txt"]` |
| `platform.maintenance_mode` | `boolean` | `false` |
| `security.session_timeout_hours` | `integer` | `24` |
| `ai.openai_api_key` | `string` | `"sk-..."` — `is_sensitive=TRUE` → **REDACTED** in API |

---

## Status Lifecycles

### Job Status

```
draft ──→ published ──→ paused ──→ (back to published)
                    └──→ closed ──→ archived
```

| Status | Candidate Visible |
|--------|------------------|
| `draft` | No |
| `published` | **Yes** |
| `paused` | No |
| `closed` | No |
| `archived` | No |

---

### Resume Parse Status

```
uploaded → queued → parsing → parsed → embedding_generated → ranked → completed
         └─────────────────── any stage → failed
```

---

### Application Status (13 states)

```
applied → screening → assessment_pending → assessment_completed
                                       ↘ shortlisted → technical_interview
                                                     → hr_interview
                                                     → final_interview
                                                     → offer_sent → offer_accepted ●
                                                                 → offer_declined ●
From any active state → rejected ●
From any active state → withdrawn ●
```

● = Terminal state

---

### AI Processing Job Status

```
queued → processing → completed ●
                   → (re-queued with backoff if retries remain)
                   → failed ● (all retries exhausted)
queued → cancelled ●
```

**Retry backoff:** 60s → 120s → 240s (60s × 2^(retry−1))

---

### Email Status

```
pending → sent → delivered ●
                          → opened ●
              → bounced ●
pending → failed ●
```

---

## Index Strategy

| Type | Used For | Tables |
|------|----------|--------|
| **B-Tree** | Equality, range, ORDER BY | All |
| **GIN** | JSONB `@>` containment | `jobs` |
| **Partial** | Status-filtered tables | `ai_processing_jobs`, `resumes`, `jobs` |
| **Unique** | Business constraint enforcement | Multiple |

### Critical Performance Queries → Indexes

| Query | Index |
|-------|-------|
| Worker pickup: `status='queued' ORDER BY priority, queued_at` | `ix_ai_jobs_worker_pickup` (partial) |
| Unread badge: `user_id=X AND is_read=FALSE` | `ix_notifications_user_id_is_read` |
| Ranked applicants: `job_id=X ORDER BY match_score DESC` | `ix_applications_job_score` |
| Skills search: `skills_required @> '["Python"]'` | `ix_jobs_skills_required` (GIN) |
| Audit trail: `user_id=X ORDER BY created_at DESC` | `ix_audit_logs_user_id_created_at` |
| Primary resume: `user_id=X AND is_primary=TRUE AND deleted_at IS NULL` | `ix_resumes_is_primary` (partial) |

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **UUID primary keys** | Globally unique, safe for distributed environments, no sequential guessing attacks |
| **str-Enum + VARCHAR** | Python type safety without PostgreSQL `ENUM` rigidity — new statuses require no `ALTER TYPE` |
| **JSONB for AI output** | AI payload schemas evolve rapidly; JSONB allows iteration without migrations |
| **GIN indexes on skill arrays** | `@>` containment queries are critical hot paths — only GIN makes them fast |
| **Soft delete (`deleted_at`)** | Preserves history, enables restore, prevents FK cascade failures from hard deletes |
| **Separate `ranking_reason` from `score_breakdown`** | Numeric scores feed charts; narrative explanations feed the UI reasoning panel — separate fields = separate concerns |
| **Immutable `audit_logs`** | No `updated_at`, no soft delete — modifying an audit record defeats compliance intent |
| **Polymorphic references** | `entity_type + entity_id` avoids multiplying FK columns in `audit_logs`, `notifications`, `email_logs`, `ai_processing_jobs` |
| **Dual read tracking** (`is_read` + `read_at`) | `is_read` indexed for O(1) badge count; `read_at` for time-series analytics — both serve different query patterns |
| **`UNIQUE(user_id, sha256_hash)` on resumes** | Prevents duplicate uploads per candidate while allowing two candidates to upload identical files |
| **Self-referential FK on resumes** | Version chain without a junction table — `previous_resume_id` points to the prior version |
| **No soft delete on `applications`** | `WITHDRAWN` and `REJECTED` statuses are the authoritative closed states; the full application history must be preserved exactly |
| **`updated_by` FK on `system_settings`** | Lightweight change log at the row level — who last changed each setting, combined with `updated_at` |
