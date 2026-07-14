# GitHub Analyzer — Full-Depth AI Code Assessment with SigNoz Observability

A local web app that takes a GitHub username, fetches complete data for up to 5 selected repositories, runs a deep AI-powered assessment of every file using the Gemini API, and makes every API call, token, and pipeline step fully observable through SigNoz.

Built for the [Agents of SigNoz Hackathon](https://signoz.io/hackathon) — blog track.

---

## What It Does

1. Enter a GitHub username
2. App fetches full profile + all public repositories
3. Pick up to 5 repos for deep analysis
4. For each selected repo, the app fetches:
   - Every file's content (all directories, all files)
   - README
   - Languages and LOC breakdown
   - Commit history, contributors, open issues, PRs, repo metadata
5. Gemini API analyzes everything and produces a per-repo assessment:
   - Code quality (naming, structure, modularity, error handling)
   - Engineering practices (design patterns, separation of concerns, test presence)
   - Documentation quality (README, inline comments, docstrings)
   - Language breakdown and lines of code
   - Complexity (file sizes, nesting depth, coupling)
   - Red flags (hardcoded values, missing error handling, etc.)
   - Strengths and weaknesses
   - Score out of 10 per dimension
6. An overall developer profile score is generated across all selected repos
7. SigNoz observes every step — traces, metrics, logs, dashboards, alerts

---

## Observability with SigNoz

Every part of the pipeline is instrumented with OpenTelemetry and ships telemetry to a self-hosted SigNoz instance.

### Traces
- Full distributed trace per assessment run
- Spans for every GitHub API call (profile fetch, repo list, file tree, individual file fetches)
- Spans for every Gemini API call (per repo, per batch)
- Analysis pipeline spans (file batching, prompt construction, response parsing)

### Metrics
- GitHub API rate limit remaining (gauge)
- Gemini token usage — prompt tokens, completion tokens, total tokens per repo and per run
- File count and LOC processed per repo
- API call latency (GitHub and Gemini)
- Assessment duration per repo and total run time
- Error counts per API

### Logs
- Structured logs at every pipeline step
- Which file is being fetched, its size, language
- Gemini prompt size and response size per batch
- Rate limit warnings
- Full error context on failures

### Dashboards
- API latency over time (GitHub + Gemini)
- Token cost per repo and cumulative per run
- GitHub rate limit gauge with warning threshold
- Error rate panel
- Assessment duration breakdown per repo
- File and LOC volume processed

### Alerts
- GitHub API rate limit below threshold
- Gemini API error spike

---

## Tech Stack

| Layer | Tool |
|---|---|
| Backend | Python + FastAPI |
| GitHub Data | GitHub REST API (via `requests`) |
| AI Analysis | Google Gemini API (`google-generativeai`) |
| Instrumentation | `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-requests` |
| Telemetry Export | OTLP → SigNoz (self-hosted, Docker) |
| Frontend | HTML + vanilla JS (local only) |

---

## SigNoz Setup (Self-Hosted via Docker)

This project uses SigNoz deployed locally with [Foundry](https://signoz.io/docs/install/docker/).

### Prerequisites
- Docker Engine 20.10+ with Docker Compose v2
- At least 4GB memory allocated to Docker
- Ports open: `8080` (SigNoz UI), `4317` and `4318` (OTLP ingestion)

### Install foundryctl

```bash
curl -fsSL https://signoz.io/foundry.sh | bash
```

### Create `casting.yaml`

```yaml
apiVersion: v1alpha1
kind: Installation
metadata:
  name: signoz
spec:
  deployment:
    flavor: compose
    mode: docker
```

### Deploy

```bash
foundryctl cast -f casting.yaml
```

### Verify

```bash
docker ps
```

You should see containers for `signoz`, `clickhouse`, `postgres`, `clickhouse-keeper`, and the `otel-collector`. Once all are healthy, open [http://localhost:8080](http://localhost:8080).

---

## Project Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd github-analyzer
pip install -r requirements.txt
```

### 2. Set environment variables

```bash
cp .env.example .env
# Fill in your GitHub token and Gemini API key
```

### 3. Run the app

```bash
uvicorn main:app --reload
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

---

## Environment Variables

```
GITHUB_TOKEN=your_github_personal_access_token
GEMINI_API_KEY=your_gemini_api_key
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
OTEL_SERVICE_NAME=github-analyzer
```

---

## Project Structure

```
github-analyzer/
├── main.py               # FastAPI app + OTel setup
├── github_client.py      # GitHub API fetching layer
├── gemini_client.py      # Gemini analysis layer
├── telemetry.py          # OpenTelemetry instrumentation setup
├── templates/
│   └── index.html        # Frontend UI
├── casting.yaml          # SigNoz Foundry deployment config
├── requirements.txt
└── .env.example
```

---

## Notes

- Analysis time per run can be large depending on repo size — this is intentional. The goal is depth, not speed.
- GitHub API rate limits apply. A personal access token with `repo` read scope is recommended to avoid hitting unauthenticated limits.
- Gemini context window limits are handled by batching files per repo.
