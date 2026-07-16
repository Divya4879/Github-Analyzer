import os
import json
import logging
import time
import asyncio
from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from opentelemetry import trace

from telemetry import setup_telemetry, tracer
from github_client import (
    get_user, get_repos, get_repo_metadata,
    get_all_files, get_commit_summary, clear_cache
)
from gemini_client import assess_repo, build_developer_profile

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="GitHub Analyzer", lifespan=lifespan)
setup_telemetry(app)
app.mount("/static", StaticFiles(directory="templates"), name="static")

# In-memory stats (resets on server restart)
_stats = {
    "total_analyses": 0,
    "total_repos": 0,
    "total_files": 0,
    "total_loc": 0,
    "gemini_tokens": 0,
    "analyses_history": [],   # last 20: {username, repos, timestamp, avg_score}
    "score_distribution": [0,0,0,0,0,0,0,0,0,0],  # buckets 1-10
    "top_languages": {},
}


def _html(name: str):
    with open(f"templates/{name}") as f:
        return HTMLResponse(f.read())


@app.get("/",          response_class=HTMLResponse) 
async def landing():      return _html("landing.html")

@app.get("/analyze",   response_class=HTMLResponse)
async def analyze_page(): return _html("analyze.html")

@app.get("/loading",   response_class=HTMLResponse)
async def loading_page(): return _html("loading.html")

@app.get("/results",   response_class=HTMLResponse)
async def results_page(): return _html("results.html")

@app.get("/compare",   response_class=HTMLResponse)
async def compare_page(): return _html("compare.html")


class AnalyzeRequest(BaseModel):
    username: str
    repos: list[str]  # max 5


class CompareRequest(BaseModel):
    username_a: str
    repos_a: list[str]
    username_b: str
    repos_b: list[str]


@app.get("/api/user/{username}")
async def fetch_user(username: str):
    with tracer.start_as_current_span("api.fetch_user", attributes={"username": username}):
        try:
            user = get_user(username)
            repos = get_repos(username)
            logger.info("api.fetch_user username=%s repos=%d", username, len(repos))
            return {
                "user": {
                    "login": user["login"],
                    "name": user.get("name"),
                    "avatar_url": user.get("avatar_url"),
                    "bio": user.get("bio"),
                    "public_repos": user.get("public_repos"),
                    "followers": user.get("followers"),
                    "following": user.get("following"),
                    "location": user.get("location"),
                    "blog": user.get("blog"),
                    "company": user.get("company"),
                    "created_at": user.get("created_at"),
                },
                "repos": [
                    {
                        "full_name": r["full_name"],
                        "name": r["name"],
                        "description": r.get("description"),
                        "language": r.get("language"),
                        "stargazers_count": r.get("stargazers_count", 0),
                        "forks_count": r.get("forks_count", 0),
                        "updated_at": r.get("updated_at"),
                        "default_branch": r.get("default_branch", "main"),
                    }
                    for r in repos
                ],
            }
        except Exception as e:
            logger.error("api.fetch_user error username=%s error=%s", username, str(e))
            raise HTTPException(status_code=400, detail=str(e))


def _run_analysis(username: str, repos: list[str]):
    """Generator that yields SSE events and returns final result."""
    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    user_data = get_user(username)
    repo_assessments = {}
    repo_metadata_all = {}

    for full_name in repos:
        owner, repo = full_name.split("/", 1)
        yield event({"stage": "repo_start", "repo": full_name})

        for attempt in range(3):
            try:
                yield event({"stage": "fetching_meta", "repo": full_name})
                metadata = get_repo_metadata(owner, repo)
                branch = metadata["info"].get("default_branch", "main")
                commit_summary = get_commit_summary(metadata.get("commits", []))

                yield event({"stage": "fetching_files", "repo": full_name})
                files = get_all_files(owner, repo, branch)
                yield event({"stage": "files_ready", "repo": full_name,
                             "file_count": len(files),
                             "total_loc": sum(f["loc"] for f in files)})

                yield event({"stage": "analyzing", "repo": full_name})
                assessment = assess_repo(owner, repo, files, metadata, commit_summary)
                repo_assessments[full_name] = assessment
                repo_metadata_all[full_name] = {
                    "languages": metadata["languages"],
                    "stars": metadata["info"].get("stargazers_count", 0),
                    "forks": metadata["info"].get("forks_count", 0),
                    "open_issues": metadata["info"].get("open_issues_count", 0),
                    "file_count": len(files),
                    "total_loc": sum(f["loc"] for f in files),
                }
                yield event({"stage": "repo_done", "repo": full_name})
                break
            except Exception as e:
                logger.error("analysis error attempt=%d repo=%s error=%s",
                             attempt + 1, full_name, str(e))
                if attempt < 2:
                    wait = 15 * (attempt + 1)
                    yield event({"stage": "retry", "repo": full_name,
                                 "attempt": attempt + 1, "wait": wait})
                    time.sleep(wait)
                else:
                    repo_assessments[full_name] = {
                        "assessment": f"Analysis failed: {str(e)}",
                        "scores": {}, "strengths": [], "red_flags": []
                    }

    yield event({"stage": "building_profile"})
    successful = {k: v for k, v in repo_assessments.items()
                  if v.get("scores")}
    developer_profile = build_developer_profile(successful) if successful else {}

    result = {
        "username": username,
        "repo_assessments": repo_assessments,
        "repo_metadata": repo_metadata_all,
        "developer_profile": developer_profile,
        "user_info": {
            "avatar_url": user_data.get("avatar_url"),
            "name": user_data.get("name"),
            "bio": user_data.get("bio"),
            "company": user_data.get("company"),
            "location": user_data.get("location"),
            "public_repos": user_data.get("public_repos"),
            "followers": user_data.get("followers"),
        },
    }
    yield event({"stage": "done", "result": result})


@app.post("/api/analyze/stream")
async def analyze_stream(req: AnalyzeRequest):
    if len(req.repos) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 repos allowed")

    def generate():
        yield from _run_analysis(req.username, req.repos)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Keep non-streaming endpoint for compare mode
@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    if len(req.repos) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 repos allowed")
    with tracer.start_as_current_span("api.analyze",
                                      attributes={"username": req.username,
                                                  "repo_count": len(req.repos)}):
        result = None
        for event_str in _run_analysis(req.username, req.repos):
            data = json.loads(event_str.replace("data: ", "").strip())
            if data.get("stage") == "done":
                result = data["result"]
        return result


@app.post("/api/compare")
async def compare(req: CompareRequest):
    if len(req.repos_a) > 3 or len(req.repos_b) > 3:
        raise HTTPException(status_code=400, detail="Maximum 3 repos per user in compare mode")
    with tracer.start_as_current_span("api.compare"):
        result_a, result_b = None, None
        for event_str in _run_analysis(req.username_a, req.repos_a):
            data = json.loads(event_str.replace("data: ", "").strip())
            if data.get("stage") == "done":
                result_a = data["result"]
        for event_str in _run_analysis(req.username_b, req.repos_b):
            data = json.loads(event_str.replace("data: ", "").strip())
            if data.get("stage") == "done":
                result_b = data["result"]
        return {"a": result_a, "b": result_b}


import httpx

@app.get("/api/proxy/avatar")
async def proxy_avatar(url: str):
    """Proxy GitHub avatar images to avoid CORS issues with html2canvas."""
    if not url.startswith("https://avatars.githubusercontent.com/"):
        raise HTTPException(status_code=400, detail="Only GitHub avatars allowed")
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
    return StreamingResponse(iter([r.content]), media_type=r.headers.get("content-type", "image/png"))


@app.delete("/api/cache")
async def clear_file_cache():
    clear_cache()
    return {"status": "cleared"}
