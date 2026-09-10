# Deploy Mark — Railway (API) + Netlify (React)

Mark splits into two services:

| Service | Platform | What it runs |
|---------|----------|--------------|
| **Backend** | Railway | FastAPI + PostgreSQL |
| **Frontend** | Netlify | React static site |

---

## Prerequisites

1. Push this repo to **GitHub** (Railway and Netlify deploy from Git).
2. Accounts on [railway.app](https://railway.app) and [netlify.com](https://netlify.com).

**Repo:** https://github.com/ShaheerMinhas/Mark

---

## Part 1 — Deploy backend on Railway

### 1. Create project

1. Go to [railway.app/new](https://railway.app/new)
2. **Deploy from GitHub repo** → select your `mark` repository
3. Railway detects Python from `requirements.txt`

### 2. Add PostgreSQL

1. In your Railway project → **+ New** → **Database** → **PostgreSQL**
2. Railway auto-sets `DATABASE_URL` on your web service (link the variable if needed)

> SQLite does not persist on Railway — use Postgres in production.

### 3. Set environment variables

Open your **web service** → **Variables**:

| Variable | Value | Required |
|----------|-------|----------|
| `MARK_SECRET_KEY` | Long random string (32+ chars) | Yes |
| `CORS_ORIGINS` | Your Netlify URL, e.g. `https://mark-app.netlify.app` | Yes |
| `OPENAI_API_KEY` | OpenAI key (optional, for AI descriptions) | No |

`DATABASE_URL` is set automatically when Postgres is linked.

### 4. Deploy settings

Railway uses `railway.toml` / `Procfile`:

```
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

Health check: `/api/health`

### 5. Get your API URL

1. Service → **Settings** → **Networking** → **Generate domain**
2. Copy the URL, e.g. `https://mark-production.up.railway.app`
3. Test: `https://YOUR-URL/api/health` → should return `{"status":"ok",...}`

---

## Part 2 — Deploy frontend on Netlify

### 1. Create site

1. [app.netlify.com](https://app.netlify.com) → **Add new site** → **Import from Git**
2. Select the same GitHub repo

### 2. Build settings

Netlify reads `netlify.toml` automatically:

| Setting | Value |
|---------|-------|
| Base directory | `frontend` |
| Build command | `npm run build` |
| Publish directory | `frontend/dist` |

### 3. Environment variable

**Site settings → Environment variables → Add:**

| Key | Value |
|-----|-------|
| `VITE_API_URL` | Your Railway URL, e.g. `https://mark-production.up.railway.app` |

No trailing slash.

### 4. Deploy

Click **Deploy site**. Netlify builds React and publishes to e.g. `https://random-name.netlify.app`.

### 5. Update Railway CORS

Copy your final Netlify URL and set on Railway:

```
CORS_ORIGINS=https://your-site.netlify.app
```

Redeploy Railway if needed.

---

## Part 3 — Verify

1. Open your Netlify URL
2. Register a new account
3. Run a lead search
4. Check Dashboard + CSV export

---

## CLI alternative (optional)

### Railway CLI

```bash
npm i -g @railway/cli
railway login
railway init
railway add --database postgres
railway variables set MARK_SECRET_KEY="your-long-secret"
railway up
railway domain
```

### Netlify CLI

```bash
npm i -g netlify-cli
cd frontend
netlify login
netlify init
netlify env:set VITE_API_URL https://YOUR-RAILWAY-URL.up.railway.app
netlify deploy --prod
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| CORS error in browser | Set `CORS_ORIGINS` on Railway to exact Netlify URL (https, no trailing slash) |
| API calls go to Netlify | Set `VITE_API_URL` on Netlify and redeploy |
| Registration 500 | Check Railway logs; ensure Postgres is linked |
| Empty search results | Railway outbound network — DuckDuckGo search may rate-limit; check logs |

---

## Architecture

```
User browser
    ↓
Netlify (React)          Railway (FastAPI)
mark.netlify.app    →    mark.up.railway.app/api/*
                              ↓
                         PostgreSQL
```
