import os
import json
import logging
import time
from google import genai
from telemetry import (
    tracer, gemini_tokens_counter, gemini_prompt_tokens_counter,
    gemini_completion_tokens_counter, api_error_counter, assessment_duration_histogram
)

logger = logging.getLogger(__name__)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash"
BATCH_CHAR_LIMIT = 800_000


def _track_usage(usage, repo: str):
    if not usage:
        return
    pt = getattr(usage, "prompt_token_count", 0) or 0
    ct = getattr(usage, "candidates_token_count", 0) or 0
    gemini_prompt_tokens_counter.add(pt, {"repo": repo})
    gemini_completion_tokens_counter.add(ct, {"repo": repo})
    gemini_tokens_counter.add(pt + ct, {"repo": repo})
    logger.info("gemini.tokens repo=%s prompt=%d completion=%d total=%d", repo, pt, ct, pt + ct)


def _call_gemini(prompt: str, repo: str) -> str:
    with tracer.start_as_current_span("gemini.generate",
                                      attributes={"gemini.repo": repo,
                                                  "gemini.prompt_chars": len(prompt)}):
        for attempt in range(5):
            try:
                response = client.models.generate_content(model=MODEL, contents=prompt)
                _track_usage(response.usage_metadata, repo)
                return response.text
            except Exception as e:
                api_error_counter.add(1, {"service": "gemini", "repo": repo})
                logger.error("gemini.error attempt=%d repo=%s error=%s", attempt + 1, repo, str(e))
                if attempt == 4:
                    raise
                wait = 10 * (attempt + 1)
                logger.info("gemini.retry waiting=%ds", wait)
                time.sleep(wait)


def _build_file_batches(files: list[dict]) -> list[str]:
    batches, current, current_len = [], [], 0
    for f in files:
        chunk = f"### FILE: {f['path']} ({f['loc']} lines)\n```\n{f['content']}\n```\n\n"
        if current_len + len(chunk) > BATCH_CHAR_LIMIT and current:
            batches.append("".join(current))
            current, current_len = [], 0
        current.append(chunk)
        current_len += len(chunk)
    if current:
        batches.append("".join(current))
    return batches


ASSESSMENT_PROMPT = """
You are a senior software engineer performing a deep code review.

Languages (from GitHub API): {languages}

{commit_summary}

Analyze the repository files and return a JSON object with this exact structure:
{{
  "scores": {{
    "code_quality": <1-10>,
    "engineering_practices": <1-10>,
    "test_coverage": <1-10>,
    "documentation": <1-10>,
    "complexity": <1-10>,
    "security": <1-10>,
    "architecture": <1-10>,
    "overall": <1-10>
  }},
  "summary": "<2-3 sentence project overview>",
  "strengths": ["<specific strength with file reference>", "<strength 2>", "<strength 3>"],
  "red_flags": ["<specific issue with file reference>", "<issue 2>", "<issue 3>"],
  "assessment": "<full markdown assessment covering all dimensions with reasoning>"
}}

Return ONLY valid JSON. No markdown fences around the JSON itself.

Repository: {repo}
Batch: {batch_num} of {total_batches}

--- FILES BEGIN ---
{files}
--- FILES END ---
"""

SYNTHESIS_PROMPT = """
You are a senior software engineer. Synthesize these partial assessments of the same repository into one final JSON with this structure:
{{
  "scores": {{
    "code_quality": <1-10>,
    "engineering_practices": <1-10>,
    "test_coverage": <1-10>,
    "documentation": <1-10>,
    "complexity": <1-10>,
    "security": <1-10>,
    "architecture": <1-10>,
    "overall": <1-10>
  }},
  "summary": "<2-3 sentence project overview>",
  "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "red_flags": ["<issue 1>", "<issue 2>", "<issue 3>"],
  "assessment": "<full markdown assessment>"
}}

Return ONLY valid JSON.

Repository: {repo}

--- PARTIAL ASSESSMENTS ---
{partials}
"""

DEVELOPER_PROFILE_PROMPT = """
You are a senior engineering manager. Based on these repository assessments, produce a developer profile JSON:
{{
  "maturity_level": "<Junior|Mid|Senior|Staff>",
  "dominant_languages": ["<lang1>", "<lang2>"],
  "overall_score": <1-10>,
  "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "weaknesses": ["<weakness 1>", "<weakness 2>", "<weakness 3>"],
  "most_impressive_repo": "<repo name>",
  "most_impressive_reason": "<one sentence>",
  "growth_areas": ["<area 1>", "<area 2>"],
  "summary": "<3-4 sentence developer profile narrative>"
}}

Return ONLY valid JSON.

--- REPO ASSESSMENTS ---
{assessments}
"""


def _parse_json(text: str, fallback_key: str = "assessment") -> dict:
    """Parse JSON from Gemini response, stripping any accidental markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except Exception:
        logger.warning("gemini.json_parse_failed, returning raw text")
        return {fallback_key: text, "scores": {}, "strengths": [], "red_flags": []}


def assess_repo(owner: str, repo: str, files: list[dict],
                metadata: dict, commit_summary: str = "") -> dict:
    repo_id = f"{owner}/{repo}"
    start = time.time()
    with tracer.start_as_current_span("gemini.assess_repo",
                                      attributes={"github.repo": repo_id,
                                                  "file_count": len(files)}):
        batches = _build_file_batches(files)
        logger.info("gemini.assess_repo repo=%s batches=%d files=%d",
                    repo_id, len(batches), len(files))

        partials = []
        for i, batch_content in enumerate(batches):
            prompt = ASSESSMENT_PROMPT.format(
                repo=repo_id,
                batch_num=i + 1,
                total_batches=len(batches),
                languages=", ".join(metadata.get("languages", {}).keys()) or "unknown",
                commit_summary=commit_summary,
                files=batch_content,
            )
            result = _call_gemini(prompt, repo_id)
            partials.append(result)
            logger.info("gemini.batch_done repo=%s batch=%d/%d", repo_id, i + 1, len(batches))

        if len(partials) == 1:
            parsed = _parse_json(partials[0])
        else:
            synthesis_prompt = SYNTHESIS_PROMPT.format(
                repo=repo_id,
                partials="\n\n---\n\n".join(partials),
            )
            parsed = _parse_json(_call_gemini(synthesis_prompt, repo_id))

        duration = time.time() - start
        assessment_duration_histogram.record(duration, {"repo": repo_id})
        logger.info("gemini.assess_repo_done repo=%s duration=%.2fs", repo_id, duration)
        return parsed


def build_developer_profile(repo_assessments: dict[str, dict]) -> dict:
    with tracer.start_as_current_span("gemini.developer_profile",
                                      attributes={"repo_count": len(repo_assessments)}):
        combined = "\n\n".join(
            f"## {repo}\n{json.dumps(data, indent=2)}"
            for repo, data in repo_assessments.items()
        )
        prompt = DEVELOPER_PROFILE_PROMPT.format(assessments=combined)
        return _parse_json(_call_gemini(prompt, "developer-profile"), "summary")
