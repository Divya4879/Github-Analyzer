# GitIntel

AI-powered GitHub profile analyzer. Enter a username, select up to 5 repos, and get a deep code assessment powered by Gemini 2.5 Flash — with full observability via OpenTelemetry and SigNoz.

**Live demo:** [gitintel-2kh2.onrender.com](https://gitintel-2kh2.onrender.com)

> Built for the [Agents of SigNoz Hackathon](https://signoz.io/hackathon) — blog track.

---

## Features

- **AI code assessment** — scores each repo across 8 dimensions: code quality, architecture, security, test coverage, documentation, complexity, engineering practices, and overall
- **Developer profile** — cross-repo analysis with maturity level (Junior → Staff), strengths, weaknesses, and growth areas
- **Side-by-side comparison** — compare two GitHub profiles against each other
- **Downloadable assessment card** — export your full results as a PNG with your GitHub profile picture and all scores
- **Real-time streaming** — analysis progress streamed live via SSE, no page refresh needed
- **Full observability** — traces, metrics, and logs via OpenTelemetry exported to SigNoz
  - Gemini token usage tracked per repo (`gemini.tokens.total`, `gemini.tokens.prompt`, `gemini.tokens.completion`)
  - GitHub rate limit gauge updated on every API response (`github.ratelimit.remaining`)
  - Per-repo assessment duration histogram (`assessment.duration`)
  - File and LOC counters (`github.files.processed`, `github.loc.processed`)
  - API error counter by service (`api.errors`)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn |
| AI | Gemini 2.5 Flash via `google-genai` |
| GitHub data | GitHub REST API, `aiohttp` (10 concurrent workers) |
| Observability | OpenTelemetry SDK, SigNoz (self-hosted) |
| Frontend | Vanilla JS, HTML/CSS (no framework) |

---

## Prerequisites

- Ubuntu 24.04 (or similar)
- Python 3.12
- Docker and Docker Compose v2
- A **GitHub Personal Access Token** — [github.com/settings/tokens](https://github.com/settings/tokens) → Generate new token (classic) → enable `repo` and `read:user`
- A **Gemini API key** — [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (free tier works)

Verify:
```bash
python3 --version    # 3.12.x
docker --version
docker compose version
```

---

## Local Setup

### 1. Clone and install

```bash
git clone https://github.com/Divya4879/Github-Analyzer
cd Github-Analyzer

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
nano .env
```

```env
GITHUB_TOKEN=ghp_yourtokenhere
GEMINI_API_KEY=AIzaSy_yourkeyhere
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
OTEL_SERVICE_NAME=gitintel
OTEL_ENABLED=true
```

### 3. Start SigNoz (observability backend)

```bash
cd pours/deployment
docker compose up -d

# Wait ~60 seconds for ClickHouse to initialize, then verify:
docker compose ps
```

SigNoz UI: `http://localhost:8080`

### 4. Run the app

```bash
cd ../..
source venv/bin/activate
uvicorn main:app --reload
```

App: `http://localhost:8000`

Once you run an analysis, your `gitintel` service appears in SigNoz → Services tab with live traces, metrics, and logs.

---

## What SigNoz Tracks

| Signal | What you see |
|---|---|
| Traces | Full span tree per analysis: `github.get_user` → `github.get_all_files` → `gemini.assess_repo` → `gemini.generate` (per batch) → `gemini.developer_profile` |
| `gemini.tokens.total` | Token cost per repo — reveals which repos are expensive |
| `github.ratelimit.remaining` | Live GitHub API headroom (5,000 req/hour limit) |
| `assessment.duration` | p50/p95 latency histogram per repo |
| `api.errors` | Error counts broken down by service (github / gemini) |
| Logs | All structured logs correlated with trace IDs — click any log to jump to its trace |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Landing page |
| `GET` | `/analyze` | Analysis form |
| `GET` | `/results` | Results page |
| `GET` | `/compare` | Comparison page |
| `GET` | `/api/user/{username}` | Fetch user info and repo list |
| `POST` | `/api/analyze/stream` | Run analysis with SSE streaming (max 5 repos) |
| `POST` | `/api/analyze` | Run analysis, return JSON (max 5 repos) |
| `POST` | `/api/compare` | Compare two users (max 3 repos each) |
| `GET` | `/api/proxy/avatar` | Proxy GitHub avatars for CORS-safe PNG export |
| `DELETE` | `/api/cache` | Clear in-memory file cache |

---

## Project Structure

```
Github-Analyzer/
├── main.py              # FastAPI app, all routes and SSE streaming
├── github_client.py     # GitHub API client, async file fetching, OTel spans
├── gemini_client.py     # Gemini AI client, batching logic, token tracking
├── telemetry.py         # OTel setup — traces, metrics, logs, custom instruments
├── requirements.txt
├── .env.example
├── Procfile             # For Render deployment
├── templates/
│   ├── landing.html
│   ├── analyze.html
│   ├── loading.html
│   ├── results.html     # Assessment cards + PNG download
│   ├── compare.html
│   ├── shared.css
│   └── theme.js         # Dark/light theme toggle
└── pours/deployment/
    ├── compose.yaml     # SigNoz full stack (ClickHouse, Keeper, Postgres, Ingester)
    ├── ingester/        # OTel Collector config
    ├── telemetrystore/  # ClickHouse config
    └── telemetrykeeper/ # ClickHouse Keeper config
```

---

## Deployment (Render)

The app deploys to Render without SigNoz — set `OTEL_ENABLED=false` in Render's environment variables to disable OTel export cleanly.

1. Push to GitHub
2. Create a new Web Service on [render.com](https://render.com), connect your repo
3. Set environment variables in the Render dashboard:
   ```
   GITHUB_TOKEN=your_token
   GEMINI_API_KEY=your_key
   OTEL_SERVICE_NAME=gitintel
   ```
4. Build command: `pip install -r requirements.txt`
5. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

> Note: Render's free tier spins down after 15 minutes of inactivity. First request after sleep takes ~30 seconds.

---

## Notes

- **Max repos per analysis:** 5 (analyze), 3 per user (compare)
- **File concurrency:** 10 parallel GitHub API workers per repo
- **Gemini batching:** source files are batched at 800,000 characters per call to keep individual request latency predictable
- **File cache:** fetched repo files are cached in memory for the session — use `DELETE /api/cache` to clear
- **OTel export:** controlled by `OTEL_ENABLED` env var (`true` by default). Set to `false` on deployments without SigNoz to disable export cleanly.
