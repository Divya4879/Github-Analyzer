import os
import asyncio
import base64
import logging
import requests
import aiohttp
from opentelemetry import trace
from telemetry import (
    tracer, github_ratelimit_gauge, files_processed_counter,
    loc_processed_counter, api_error_counter
)

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
HEADERS = {
    "Authorization": f"token {os.getenv('GITHUB_TOKEN')}",
    "Accept": "application/vnd.github+json",
}
CONCURRENCY = 10  # max parallel file fetches

# In-memory cache: {owner/repo@branch: [files]}
_file_cache: dict[str, list[dict]] = {}


def _get(url, params=None):
    resp = requests.get(url, headers=HEADERS, params=params)
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        github_ratelimit_gauge.set(int(remaining))
        logger.info("github.ratelimit.remaining=%s url=%s", remaining, url)
    if not resp.ok:
        api_error_counter.add(1, {"service": "github", "status": str(resp.status_code)})
        logger.error("GitHub API error status=%s url=%s", resp.status_code, url)
        resp.raise_for_status()
    return resp.json()


def get_user(username: str) -> dict:
    with tracer.start_as_current_span("github.get_user", attributes={"github.username": username}):
        return _get(f"{GITHUB_API}/users/{username}")


def get_repos(username: str) -> list:
    with tracer.start_as_current_span("github.get_repos", attributes={"github.username": username}):
        repos, page = [], 1
        while True:
            batch = _get(f"{GITHUB_API}/users/{username}/repos",
                         params={"per_page": 100, "page": page, "sort": "updated"})
            if not batch:
                break
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        logger.info("github.get_repos username=%s count=%d", username, len(repos))
        return repos


def get_repo_metadata(owner: str, repo: str) -> dict:
    with tracer.start_as_current_span("github.get_repo_metadata",
                                      attributes={"github.repo": f"{owner}/{repo}"}):
        data = {}
        data["info"] = _get(f"{GITHUB_API}/repos/{owner}/{repo}")
        data["languages"] = _get(f"{GITHUB_API}/repos/{owner}/{repo}/languages")
        # Commit history — top 20 commits for context
        data["commits"] = _get(f"{GITHUB_API}/repos/{owner}/{repo}/commits",
                                params={"per_page": 20})
        return data


def get_commit_summary(commits: list) -> str:
    """Build a short commit activity summary for the Gemini prompt."""
    if not commits:
        return ""
    lines = []
    for c in commits[:10]:
        msg = c.get("commit", {}).get("message", "").split("\n")[0][:80]
        date = c.get("commit", {}).get("author", {}).get("date", "")[:10]
        lines.append(f"  {date}: {msg}")
    return "Recent commits (newest first):\n" + "\n".join(lines)


# ── File filtering ──────────────────────────────────────────────────────────

_SOURCE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".bash",
    ".sql", ".proto", ".html", ".css", ".scss",
}
_SKIP_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "package.json", "tsconfig.json", "tsconfig.node.json", ".eslintrc",
    ".eslintrc.json", ".eslintrc.js", ".prettierrc", ".prettierrc.json",
    ".babelrc", "babel.config.js", "jest.config.js", "jest.config.ts",
    "vite.config.js", "vite.config.ts", "next.config.js", "next.config.ts",
    "webpack.config.js", "rollup.config.js", "postcss.config.js",
    "tailwind.config.js", "tailwind.config.ts", "netlify.toml",
    "vercel.json", ".gitignore", ".dockerignore", "dockerfile",
    "docker-compose.yml", "docker-compose.yaml", "makefile",
    "requirements.txt", "setup.py", "setup.cfg", "pyproject.toml",
    "cargo.toml", "cargo.lock", "go.mod", "go.sum", "pom.xml", "build.gradle",
}
_SKIP_DIRS = {"node_modules/", ".git/", "dist/", "build/", ".next/",
              "__pycache__/", "vendor/", "venv/"}


def _is_source(path: str) -> bool:
    lower = path.lower()
    filename = lower.split("/")[-1]
    if filename.startswith("readme"):
        return True
    if filename in _SKIP_NAMES:
        return False
    if any(lower.startswith(d) for d in _SKIP_DIRS):
        return False
    return os.path.splitext(lower)[1] in _SOURCE_EXTS


# ── Async concurrent file fetching ─────────────────────────────────────────

async def _fetch_file_async(session: aiohttp.ClientSession, owner: str,
                             repo: str, item: dict) -> dict | None:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{item['path']}"
    try:
        async with session.get(url, headers=HEADERS) as resp:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining:
                github_ratelimit_gauge.set(int(remaining))
            if resp.status != 200:
                api_error_counter.add(1, {"service": "github", "status": str(resp.status)})
                return None
            data = await resp.json()
            if data.get("encoding") == "base64":
                content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            else:
                content = data.get("content", "")
            loc = content.count("\n") + 1
            files_processed_counter.add(1, {"repo": f"{owner}/{repo}"})
            loc_processed_counter.add(loc, {"repo": f"{owner}/{repo}"})
            logger.info("github.file_fetched path=%s loc=%d", item["path"], loc)
            return {"path": item["path"], "content": content, "loc": loc}
    except Exception as e:
        logger.warning("github.fetch_file failed path=%s error=%s", item["path"], str(e))
        return None


async def _fetch_all_async(owner: str, repo: str, items: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession() as session:
        async def bounded(item):
            async with sem:
                return await _fetch_file_async(session, owner, repo, item)
        results = await asyncio.gather(*[bounded(i) for i in items])
    return [r for r in results if r is not None]


def get_all_files(owner: str, repo: str, branch: str,
                  progress_cb=None) -> list[dict]:
    cache_key = f"{owner}/{repo}@{branch}"
    if cache_key in _file_cache:
        logger.info("github.cache_hit repo=%s", cache_key)
        return _file_cache[cache_key]

    with tracer.start_as_current_span("github.get_all_files",
                                      attributes={"github.repo": f"{owner}/{repo}",
                                                  "github.branch": branch}):
        span = trace.get_current_span()
        data = _get(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}",
                    params={"recursive": "1"})
        tree = [i for i in data.get("tree", []) if i["type"] == "blob"]
        source_files = [f for f in tree if _is_source(f["path"])]

        span.set_attribute("github.file_count.total", len(tree))
        span.set_attribute("github.file_count.source", len(source_files))
        logger.info("github.get_all_files repo=%s/%s total=%d source=%d",
                    owner, repo, len(tree), len(source_files))

        if progress_cb:
            progress_cb({"stage": "fetching", "total": len(source_files), "done": 0})

        results = asyncio.run(_fetch_all_async(owner, repo, source_files))

        span.set_attribute("github.file_count.fetched", len(results))
        _file_cache[cache_key] = results
        return results


def clear_cache():
    _file_cache.clear()
