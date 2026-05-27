#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
DEFAULT_CONFIG = Path("config/interests.json")
DEFAULT_OUTPUT = Path("web/data/papers.json")


@dataclass(frozen=True)
class Topic:
    id: str
    name: str
    description: str
    keywords: list[str]
    arxiv_categories: list[str]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def parse_topics(config: dict[str, Any]) -> list[Topic]:
    topics = []
    for item in config.get("topics", []):
        topic_id = item.get("id") or slugify(item.get("name", "topic"))
        topics.append(
            Topic(
                id=topic_id,
                name=item["name"],
                description=item.get("description", ""),
                keywords=[str(k) for k in item.get("keywords", [])],
                arxiv_categories=[str(c) for c in item.get("arxiv_categories", [])],
            )
        )
    if not topics:
        raise ValueError("No topics found in configuration.")
    return topics


def github_request(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "paper-daily-collector",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_json_block(markdown: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", markdown, flags=re.S | re.I)
    if fenced:
        return json.loads(fenced.group(1))
    stripped = markdown.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    return None


def load_issue_config(default_config: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("GITHUB_TOKEN", "")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    title = os.getenv("CONFIG_ISSUE_TITLE", "Research Interests")
    if not token or not repository:
        return default_config

    query = urllib.parse.urlencode({"state": "open", "per_page": "30"})
    url = f"https://api.github.com/repos/{repository}/issues?{query}"
    try:
        issues = github_request(url, token)
    except Exception as exc:
        print(f"Warning: cannot read GitHub issues, using config file: {exc}", file=sys.stderr)
        return default_config

    for issue in issues:
        if "pull_request" in issue:
            continue
        if issue.get("title", "").strip().lower() == title.lower():
            body = issue.get("body") or ""
            try:
                issue_config = extract_json_block(body)
            except json.JSONDecodeError as exc:
                print(f"Warning: config issue JSON is invalid, using config file: {exc}", file=sys.stderr)
                return default_config
            if issue_config and issue_config.get("topics"):
                return issue_config
    return default_config


def arxiv_query_for_topic(topic: Topic) -> str:
    keyword_terms = []
    for keyword in topic.keywords[:8]:
        escaped = keyword.replace('"', '\\"')
        keyword_terms.append(f'all:"{escaped}"')

    category_terms = [f"cat:{category}" for category in topic.arxiv_categories[:5]]
    parts = []
    if keyword_terms:
        parts.append("(" + " OR ".join(keyword_terms) + ")")
    if category_terms:
        parts.append("(" + " OR ".join(category_terms) + ")")
    return " AND ".join(parts) if parts else f'all:"{topic.name}"'


def fetch_arxiv(topic: Topic, max_results: int) -> list[dict[str, Any]]:
    params = {
        "search_query": arxiv_query_for_topic(topic),
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "paper-daily-collector/1.0"})
    retry_count = int(os.getenv("ARXIV_RETRIES", "3"))
    last_error: Exception | None = None
    for attempt in range(retry_count):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                xml_data = resp.read()
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt == retry_count - 1:
                raise
            retry_after = exc.headers.get("Retry-After")
            wait_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 20 * (attempt + 1)
            print(f"arXiv rate limited {topic.name}, retrying in {wait_seconds}s", flush=True)
            time.sleep(wait_seconds)
    else:
        raise RuntimeError(f"arXiv request failed: {last_error}")

    root = ET.fromstring(xml_data)
    papers = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        paper_id = entry.findtext("atom:id", default="", namespaces=ARXIV_NS).strip()
        title = normalize_space(entry.findtext("atom:title", default="", namespaces=ARXIV_NS))
        summary = normalize_space(entry.findtext("atom:summary", default="", namespaces=ARXIV_NS))
        published = entry.findtext("atom:published", default="", namespaces=ARXIV_NS)
        updated = entry.findtext("atom:updated", default="", namespaces=ARXIV_NS)
        authors = [
            normalize_space(author.findtext("atom:name", default="", namespaces=ARXIV_NS))
            for author in entry.findall("atom:author", ARXIV_NS)
        ]
        categories = [
            category.attrib.get("term", "")
            for category in entry.findall("atom:category", ARXIV_NS)
            if category.attrib.get("term")
        ]
        pdf_url = ""
        for link in entry.findall("atom:link", ARXIV_NS):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href", "")
                break
        papers.append(
            {
                "id": paper_id.rsplit("/", 1)[-1],
                "source": "arXiv",
                "title": title,
                "authors": [a for a in authors if a],
                "summary": summary,
                "published": published,
                "updated": updated,
                "paper_url": paper_id,
                "pdf_url": pdf_url or paper_id.replace("/abs/", "/pdf/"),
                "categories": categories,
                "seed_topic": topic.id,
            }
        )
    return papers


def days_old(iso_date: str, now: dt.datetime) -> int:
    parsed = dt.datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    return (now.date() - parsed.date()).days


def keyword_score(topic: Topic, paper: dict[str, Any]) -> tuple[float, list[str]]:
    haystack = f"{paper.get('title', '')} {paper.get('summary', '')}".lower()
    hits = []
    weighted = 0.0
    for keyword in topic.keywords:
        normalized = keyword.lower()
        if normalized in haystack:
            hits.append(keyword)
            weighted += min(1.0, max(0.35, len(normalized.split()) / 5))
    score = min(1.0, weighted / max(2.0, min(5.0, len(topic.keywords) / 2)))
    return score, hits[:6]


def category_score(topic: Topic, paper: dict[str, Any]) -> float:
    paper_categories = set(paper.get("categories", []))
    topic_categories = set(topic.arxiv_categories)
    if not paper_categories or not topic_categories:
        return 0.0
    return len(paper_categories & topic_categories) / len(topic_categories)


def lexical_overlap_score(topic: Topic, paper: dict[str, Any]) -> float:
    topic_terms = set(re.findall(r"[a-zA-Z0-9]+", f"{topic.description} {' '.join(topic.keywords)}".lower()))
    paper_terms = set(re.findall(r"[a-zA-Z0-9]+", f"{paper.get('title', '')} {paper.get('summary', '')}".lower()))
    if not topic_terms or not paper_terms:
        return 0.0
    overlap = topic_terms & paper_terms
    return min(1.0, len(overlap) / max(8, len(topic_terms) * 0.18))


def match_level(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.42:
        return "medium"
    return "low"


def score_paper(topic: Topic, paper: dict[str, Any]) -> dict[str, Any]:
    k_score, hits = keyword_score(topic, paper)
    c_score = category_score(topic, paper)
    l_score = lexical_overlap_score(topic, paper)
    base_score = round(0.50 * k_score + 0.25 * c_score + 0.25 * l_score, 3)
    reason_parts = []
    if hits:
        reason_parts.append("关键词命中：" + "、".join(hits))
    if c_score > 0:
        reason_parts.append("arXiv 分类重合：" + "、".join(sorted(set(topic.arxiv_categories) & set(paper.get("categories", [])))))
    if not reason_parts:
        reason_parts.append("文本语义与方向描述存在弱相关，需要人工复核。")
    return {
        "topic_id": topic.id,
        "topic_name": topic.name,
        "score": base_score,
        "level": match_level(base_score),
        "reason": "；".join(reason_parts),
        "keyword_hits": hits,
    }


def fallback_summary(paper: dict[str, Any], best_match: dict[str, Any]) -> dict[str, str]:
    abstract = paper.get("summary", "")
    first_sentence = re.split(r"(?<=[.!?])\s+", abstract)[0] if abstract else ""
    return {
        "problem": "未配置模型 API，当前仅基于标题、摘要和关键词生成基础摘要。",
        "method": first_sentence[:300] if first_sentence else "请打开论文链接查看方法细节。",
        "innovation": "需要接入模型 API 后自动抽取更精确的中文创新点。",
        "evidence": "来源摘要可在论文原文中核验。",
        "limitations": "基础模式不会阅读全文，也不会进行深度技术对比。",
        "why_relevant": best_match.get("reason", "与配置方向存在文本匹配。"),
    }


def llm_enabled() -> bool:
    return bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY"))


def llm_provider_label() -> str:
    if os.getenv("LLM_API_KEY"):
        return "openai-compatible"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    return "disabled"


def llm_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "paper-daily-collector/1.0",
    }


def call_openai_compatible(prompt: str) -> dict[str, Any]:
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
    base_url = os.getenv("LLM_BASE_URL", "")
    if not base_url:
        base_url = "https://api.deepseek.com/v1" if os.getenv("DEEPSEEK_API_KEY") else "https://api.openai.com/v1"
    model = os.getenv("LLM_MODEL", "deepseek-chat" if os.getenv("DEEPSEEK_API_KEY") else "gpt-4o-mini")
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "你是严谨的论文技术分析助手。只输出合法 JSON，不要输出 Markdown。",
            },
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=llm_headers(api_key),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def build_llm_prompt(topic: Topic, paper: dict[str, Any], base_match: dict[str, Any]) -> str:
    return f"""
请根据论文标题、摘要、分类和我的研究方向，输出精确中文分析。不要夸大摘要中没有的信息；如果证据不足，请明确说明。

我的研究方向：
名称：{topic.name}
描述：{topic.description}
关键词：{", ".join(topic.keywords)}

论文信息：
标题：{paper.get("title", "")}
作者：{", ".join(paper.get("authors", [])[:8])}
arXiv 分类：{", ".join(paper.get("categories", []))}
摘要：{paper.get("summary", "")}

基础匹配信息：
分数：{base_match.get("score")}
等级：{base_match.get("level")}
原因：{base_match.get("reason")}

请输出 JSON，字段必须为：
{{
  "problem": "论文要解决的问题，中文，1-2句",
  "method": "核心方法，中文，1-2句",
  "innovation": "相对已有工作的具体创新点，中文，2-3点合并成一段",
  "evidence": "摘要中可核验的实验、理论或系统证据；没有则写证据不足",
  "limitations": "可能局限或需要阅读全文确认的点",
  "why_relevant": "为什么匹配我的研究方向",
  "match_score_adjustment": 0.0,
  "match_level": "high|medium|low"
}}
""".strip()


def summarize_with_llm(topic: Topic, paper: dict[str, Any], base_match: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    if not llm_enabled():
        return fallback_summary(paper, base_match), base_match

    prompt = build_llm_prompt(topic, paper, base_match)
    try:
        data = call_openai_compatible(prompt)
    except Exception as exc:
        print(f"Warning: LLM summary failed for {paper.get('id')}: {exc}", file=sys.stderr)
        return fallback_summary(paper, base_match), base_match

    summary = {
        "problem": str(data.get("problem", "")),
        "method": str(data.get("method", "")),
        "innovation": str(data.get("innovation", "")),
        "evidence": str(data.get("evidence", "")),
        "limitations": str(data.get("limitations", "")),
        "why_relevant": str(data.get("why_relevant", "")),
    }
    adjustment = float(data.get("match_score_adjustment", 0.0) or 0.0)
    adjusted_score = max(0.0, min(1.0, base_match["score"] + adjustment))
    adjusted_level = str(data.get("match_level") or match_level(adjusted_score)).lower()
    if adjusted_level not in {"high", "medium", "low"}:
        adjusted_level = match_level(adjusted_score)
    adjusted_match = dict(base_match)
    adjusted_match["score"] = round(adjusted_score, 3)
    adjusted_match["level"] = adjusted_level
    adjusted_match["llm_reason"] = summary["why_relevant"]
    return summary, adjusted_match


def dedupe_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for paper in papers:
        key = paper.get("id") or paper.get("paper_url")
        if key in seen:
            continue
        seen.add(key)
        unique.append(paper)
    return unique


def collect(config_path: Path, output_path: Path, days: int, max_per_topic: int, max_summaries: int) -> dict[str, Any]:
    default_config = load_json(config_path)
    config = load_issue_config(default_config)
    topics = parse_topics(config)
    now = dt.datetime.now(dt.timezone.utc)
    print(
        f"LLM provider: {llm_provider_label()}, enabled={llm_enabled()}, "
        f"model={os.getenv('LLM_MODEL') or ('deepseek-chat' if os.getenv('DEEPSEEK_API_KEY') else 'gpt-4o-mini')}",
        flush=True,
    )

    all_candidates = []
    successful_fetches = 0
    failed_fetches = 0
    for index, topic in enumerate(topics):
        if index:
            time.sleep(float(os.getenv("ARXIV_DELAY_SECONDS", "6")))
        print(f"Fetching arXiv papers for topic: {topic.name}", flush=True)
        try:
            topic_papers = fetch_arxiv(topic, max_per_topic)
            all_candidates.extend(topic_papers)
            successful_fetches += 1
        except Exception as exc:
            failed_fetches += 1
            print(f"Warning: arXiv request failed for {topic.name}: {exc}", file=sys.stderr)

    if failed_fetches == len(topics) and output_path.exists():
        existing = load_json(output_path)
        if existing.get("papers"):
            print("All sources failed; preserving existing paper data.", file=sys.stderr)
            existing["generated_at"] = email.utils.format_datetime(now)
            existing["generated_at_iso"] = now.isoformat()
            existing.setdefault("stats", {})["last_error"] = "All arXiv requests failed."
            write_json(output_path, existing)
            return existing

    recent_papers = []
    for paper in dedupe_papers(all_candidates):
        published = paper.get("published") or paper.get("updated")
        if not published:
            continue
        if days_old(published, now) <= days:
            matches = [score_paper(topic, paper) for topic in topics]
            matches.sort(key=lambda item: item["score"], reverse=True)
            best_match = matches[0]
            if best_match["score"] <= 0:
                continue
            paper["matches"] = matches
            paper["best_match"] = best_match
            recent_papers.append(paper)

    recent_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
    print(f"Collected {len(recent_papers)} matched papers; max summaries: {max_summaries}", flush=True)
    if not recent_papers:
        print("No matched papers found, so the LLM will not be called.", flush=True)

    summarized = 0
    for paper in recent_papers:
        best_topic = next(topic for topic in topics if topic.id == paper["best_match"]["topic_id"])
        if summarized < max_summaries:
            if llm_enabled():
                print(f"Summarizing with {llm_provider_label()}: {paper.get('id')}", flush=True)
            summary, adjusted_match = summarize_with_llm(best_topic, paper, paper["best_match"])
            paper["chinese_summary"] = summary
            paper["best_match"] = adjusted_match
            paper["matches"] = [adjusted_match if m["topic_id"] == adjusted_match["topic_id"] else m for m in paper["matches"]]
            summarized += 1
            if llm_enabled():
                time.sleep(1)
        else:
            paper["chinese_summary"] = fallback_summary(paper, paper["best_match"])

    payload = {
        "generated_at": email.utils.format_datetime(now),
        "generated_at_iso": now.isoformat(),
        "config_source": "issue" if config is not default_config else "file",
        "topics": [topic.__dict__ for topic in topics],
        "papers": recent_papers,
        "stats": {
            "paper_count": len(recent_papers),
            "days": days,
            "max_per_topic": max_per_topic,
            "llm_enabled": llm_enabled(),
            "llm_provider": llm_provider_label(),
            "successful_fetches": successful_fetches,
            "failed_fetches": failed_fetches,
        },
    }
    write_json(output_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect papers and build static data for paper-daily.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "7")))
    parser.add_argument("--max-per-topic", type=int, default=int(os.getenv("MAX_PER_TOPIC", "25")))
    parser.add_argument("--max-summaries", type=int, default=int(os.getenv("MAX_SUMMARIES", "40")))
    args = parser.parse_args()
    payload = collect(args.config, args.output, args.days, args.max_per_topic, args.max_summaries)
    print(f"Wrote {len(payload['papers'])} papers to {args.output}")


if __name__ == "__main__":
    main()
