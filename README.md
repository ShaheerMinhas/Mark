# Mark — Lead Discovery Platform

React frontend + FastAPI backend for marketing lead discovery.

## Tech stack

| Layer | Stack |
|-------|-------|
| **Frontend** | React 19, Vite |
| **Backend** | FastAPI, SQLAlchemy, SQLite |
| **Search** | Public web search (no LinkedIn login) |

## Quick start

### 1. Backend

```powershell
cd D:\mark
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

### 2. Frontend (development)

```powershell
cd D:\mark\frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173 — Vite proxies `/api` to the backend.

### 3. Production build

```powershell
cd D:\mark\frontend
npm run build
```

Then run only the backend — it serves the React app from `frontend/dist` at http://127.0.0.1:8000

## Database

SQLite file: `D:\mark\data\mark.db`

## Roadmap & tasks

Product plan with pickable tasks (checkboxes, task IDs, dependencies):

- **[docs/ROADMAP.md](./docs/ROADMAP.md)** — main backlog (start here)
- **[docs/COMPETITOR-GAP.md](./docs/COMPETITOR-GAP.md)** — what we do / don’t match vs Scrupp, AirScale, etc.
- **[docs/TASK-TEMPLATE.md](./docs/TASK-TEMPLATE.md)** — copy when opening a task

**Workflow:** pick next unchecked task in ROADMAP → branch `feat/roadmap-1.3-…` → ship → check box → log in Changelog.

## Deploy to production

See **[DEPLOY.md](./DEPLOY.md)** for Railway (backend) + Netlify (frontend) setup.

## Project structure

```
mark/
├── backend/           # FastAPI API
│   ├── main.py
│   ├── models.py
│   ├── routers/
│   └── services/
├── frontend/          # React + Vite
│   ├── src/
│   │   ├── components/
│   │   ├── context/
│   │   └── api/
│   └── dist/          # production build (after npm run build)
└── data/mark.db
```
