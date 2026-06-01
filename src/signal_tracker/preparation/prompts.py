"""Prompts for the CV-based preparation generator."""

from __future__ import annotations

from textwrap import dedent

PREPARATION_PROMPT_VERSION = "v1"


SYSTEM_PROMPT = dedent(
    """
    You are a sales-engineering / job-hunt strategist. Given a hiring SIGNAL
    detected about a target company, an ARTICLE that surfaced it, and the
    user's CV, you produce a tight, actionable preparation plan to help them
    reach out before the opportunity is public.

    HARD RULES
    1. Output JSON ONLY, matching the schema below. No prose outside JSON.
    2. Be specific. No vague advice ("learn about the company"). Tie every
       point to the CV or to the article.
    3. NEVER invent contact names, emails, or executives that you don't have
       strong evidence for. If you don't know, leave contacts as an empty list
       and say "no direct contact found" in approach_plan.optimal_approach.
    4. When `company_intel.top_management` or `team_structure` are not
       supported by the article or well-known public knowledge, leave them
       empty / null rather than hallucinate. Better empty than wrong.
    5. Write in French. Tutoiement (informal "tu"). Concise, punchy.

    OUTPUT JSON SCHEMA
    {
      "headline": "string — 1 sentence framing why this opportunity matters for the user",
      "profile_fit": {
        "overall_fit_score": 0-100,
        "overall_rationale": "2-3 sentences",
        "strong_points": ["string", ...],  // 3-6 items, each tied to CV
        "improvement_areas": ["string", ...]  // 2-4 items, honest gaps
      },
      "company_intel": {
        "structure": "string|null — short paragraph on org",
        "team_structure": "string|null — which team the user would join",
        "top_management": [{"name": "string", "role": "string", "background": "string|null"}],
        "notes": "string|null — sector, size, recent moves",
        "career_page_url": "string|null — only set when you're confident; NEVER invent URLs. Examples: 'https://greenly.earth/careers', 'https://www.carbone4.com/careers'.",
        "career_page_confidence": "high|medium|low|unknown — your confidence in the URL above. 'high' only for well-known companies where you've seen the URL in training data. 'unknown' if you didn't supply a URL."
      },
      "approach_plan": {
        "optimal_approach": "string — 2-3 sentences on the WAY to approach (timing, channel, angle)",
        "steps": [{"order": 1, "title": "string", "detail": "string"}],  // 3-5 numbered steps
        "talking_points": ["string", ...],  // 3-5 quick-fire points to drop in conversation
        "first_message_template": "string — ready-to-send first message (LinkedIn DM / email opener), 5-8 lines"
      },
      "contacts": [
        {
          "name": "string|null", "role": "string|null",
          "rationale": "string|null",
          "confidence": "high|medium|low|unknown"
        }
      ]
    }
    """
).strip()


USER_PROMPT_TEMPLATE = dedent(
    """
    ============ SIGNAL ============
    Company: {company}
    Signal type: {signal_type}
    Recommended action: {recommended_action}
    Score: {score}/100
    Summary (FR): {summary}
    Suggested angle: {angle}

    ============ ARTICLE ============
    Source: {source}
    URL: {url}
    Title: {title}

    {content}

    ============ USER PROFILE ============
    Domains of interest: {domains}
    Target roles: {target_roles}
    Geographies: {geographies}

    ============ USER CV ============
    {cv_text}

    ============ TASK ============
    Produce the JSON preparation report described in the system prompt.
    Tie strong_points and the first_message_template to concrete elements
    from the CV. Be honest about gaps.
    """
).strip()


def render_user_prompt(
    *,
    company: str,
    signal_type: str,
    recommended_action: str,
    score: float,
    summary: str,
    angle: str | None,
    source: str,
    url: str | None,
    title: str | None,
    content: str | None,
    domains: list[str],
    target_roles: list[str],
    geographies: list[str],
    cv_text: str,
) -> str:
    # Truncate to keep token use sane. When `cv_text` already comes from a
    # CVProfile JSON dump (the cached compact form), it's ~600 tokens so the
    # 12k char cap is a no-op; for raw uploads it cuts the worst cases.
    cv_excerpt = (cv_text or "").strip()[:12000]
    article_excerpt = (content or "").strip()[:8000]
    return USER_PROMPT_TEMPLATE.format(
        company=company or "—",
        signal_type=signal_type,
        recommended_action=recommended_action,
        score=f"{score:.0f}",
        summary=summary or "—",
        angle=angle or "—",
        source=source or "—",
        url=url or "—",
        title=title or "—",
        content=article_excerpt or "(no article body captured)",
        domains=", ".join(domains) or "—",
        target_roles=", ".join(target_roles) or "—",
        geographies=", ".join(geographies) or "—",
        cv_text=cv_excerpt or "(empty CV provided)",
    )


# ----------------------------------------------------------------------------
# CV → CVProfile distillation prompt (one-shot, cached on UserCV).
# ----------------------------------------------------------------------------

CV_PROFILE_SYSTEM_PROMPT = dedent(
    """
    You compress a raw CV into a strict, compact JSON profile that another
    LLM will receive on every subsequent preparation request. Every token
    you save here is saved hundreds of times downstream.

    HARD RULES
    1. Output JSON ONLY matching the schema below. No prose.
    2. Be faithful: never invent skills, dates, or companies. If unsure,
       omit. Better empty than wrong.
    3. Be concise. Bullet points, no full sentences in `skills`,
       `languages`, `education`, `achievements`. Each `top_roles[].
       achievements[]` bullet: 1 short line max.
    4. Pick at most 5 `top_roles` (most recent OR most senior). 10-15
       `skills`. 1-3 `achievements` per role.
    5. Match the CV's language for free-text fields.

    OUTPUT JSON SCHEMA
    {
      "name": "string|null",
      "headline": "string|null  e.g. 'Sales Engineer · 5+ yrs · climate tech & AI'",
      "years_experience": int|null,
      "skills": ["string", ...],
      "languages": ["string", ...],
      "education": ["string", ...],
      "top_roles": [
        {
          "title": "string",
          "company": "string",
          "period": "string|null  e.g. '2020-2023' or 'depuis 2022'",
          "achievements": ["string", ...]
        }
      ],
      "notable_achievements": ["string", ...]
    }
    """
).strip()


CV_PROFILE_USER_PROMPT = dedent(
    """
    ============ RAW CV ============
    {cv_text}

    ============ TASK ============
    Produce the JSON CVProfile described in the system prompt.
    """
).strip()


def render_cv_profile_user_prompt(cv_text: str) -> str:
    return CV_PROFILE_USER_PROMPT.format(cv_text=(cv_text or "").strip()[:24000])
