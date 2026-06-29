# Smart Resume Screening & Candidate Ranking System

An AI-powered recruitment platform that intelligently analyzes resumes, ranks candidates against job descriptions, and provides explainable AI insights for recruiters, candidates, and platform administrators.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python, Flask, SQLAlchemy, Flask-Migrate, Flask-JWT-Extended |
| **Database** | PostgreSQL (Neon for cloud) |
| **AI** | Google Gemini API, Sentence Transformers, spaCy, FAISS |
| **Frontend** | React.js, Tailwind CSS, Axios, React Router, Recharts |
| **Deployment** | Docker, Render (Backend), Vercel (Frontend) |

---

## Project Structure

```
Smart Resume Screening & Candidate Ranking System/
├── backend/                    # Flask REST API
│   ├── app/
│   │   ├── __init__.py         # Application Factory
│   │   ├── extensions.py       # Flask extensions
│   │   ├── config.py           # Environment configurations
│   │   ├── core/               # Shared utilities
│   │   │   ├── logger.py
│   │   │   ├── responses.py
│   │   │   └── exceptions.py
│   │   ├── models/             # SQLAlchemy models (Stage 1B)
│   │   ├── api/                # REST API blueprints
│   │   │   ├── health/
│   │   │   ├── auth/
│   │   │   ├── jobs/
│   │   │   ├── resume/
│   │   │   ├── candidates/
│   │   │   ├── recruiters/
│   │   │   ├── admin/
│   │   │   └── ai/
│   │   └── storage/            # File storage abstraction
│   ├── migrations/             # Flask-Migrate revisions
│   ├── tests/                  # Pytest test suite
│   ├── uploads/                # Local file storage (dev)
│   ├── logs/                   # Application logs
│   ├── .env.example            # Environment variable template
│   ├── requirements.txt
│   ├── run.py                  # Development entry point
│   └── wsgi.py                 # Production (Gunicorn) entry point
├── frontend/                   # React application (Stage 3+)
├── .gitignore
└── README.md
```

---

## Quick Start (Development)

### Prerequisites

- Python 3.11+
- PostgreSQL 15+
- pip

### 1. Clone & Navigate

```bash
cd backend
```

### 2. Create Virtual Environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
cp .env.example .env
# Edit .env with your actual values
```

### 5. Run Development Server

```bash
python run.py
```

The API will be available at `http://localhost:5000`

### 6. Health Check

```bash
curl http://localhost:5000/api/v1/health
```

---

## Running Tests

```bash
cd backend
pytest tests/ -v
```

---

## API Response Format

All API endpoints return a consistent JSON envelope:

```json
{
  "success": true,
  "message": "Human-readable message",
  "data": { },
  "meta": {
    "pagination": { }
  }
}
```

---

## User Roles

| Role | Description |
|------|-------------|
| **Candidate** | Job seekers who upload resumes and apply for jobs |
| **Recruiter** | HR professionals who post jobs and rank candidates |
| **Admin** | Platform administrators with full management access |

---

## Environment Variables

See [`.env.example`](backend/.env.example) for the full list of required environment variables.

---

## Development Stages

| Stage | Status | Description |
|-------|--------|-------------|
| 1A | ✅ Complete | Project foundation, Flask factory, health endpoint |
| 1B | 🔜 Planned | Database models and migrations |
| 2 | 🔜 Planned | Authentication (register, login, JWT, email verify) |
| 3 | 🔜 Planned | Frontend scaffolding and design system |
| 4 | 🔜 Planned | AI resume parsing and analysis |
| 5 | 🔜 Planned | Candidate ranking and job matching |

---

## License

Private — All rights reserved.
