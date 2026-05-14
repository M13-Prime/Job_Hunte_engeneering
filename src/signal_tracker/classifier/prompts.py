"""Versioned classifier prompts.

The active prompt is exported as ``CLASSIFIER_PROMPT_V1``. Bumping the version
(creating ``CLASSIFIER_PROMPT_V2``) requires explicit code changes so we can
trace which prompt produced which signals in the DB.
"""

from __future__ import annotations

from textwrap import dedent

from signal_tracker.classifier.schemas import ClassifierInput
from signal_tracker.config import UserProfile

CLASSIFIER_PROMPT_VERSION = "v1"


CLASSIFIER_PROMPT_V1 = dedent(
    """
    You are a signal classifier for a job-seeker who wants to detect that a
    company will likely hire in their domains BEFORE any job offer is posted.

    Your job: read one article / press release / news entry and decide whether
    it contains a weak hiring signal in the user's domains.

    -----------------
    USER PROFILE
    -----------------
    Domains of interest: {domains}
    Target roles: {target_roles}
    Geographies (priority order): {geographies}
    Target company types: {target_company_types}
    Extra notes: {notes}

    -----------------
    SIGNAL TAXONOMY (priority high -> low)
    -----------------
    1. executive_change  — Appointment / departure of a CSO, Chief AI Officer,
       Chief Data Officer, Head of ESG, VP Sustainability, Director CSR, or
       new CEO/COO. **Creation** of such a role from scratch is the strongest
       sub-signal.
    2. funding           — Fundraising >2M EUR in climate tech, ESG SaaS, AI
       applied to sustainability, carbon accounting, green finance,
       biodiversity tech. Includes BPI / EIC / Horizon Europe / French Tech
       2030 grants and acquisitions (the acquirer hires).
    3. csrd_publication  — A company publishing its FIRST CSRD / DPEF /
       sustainability report (= needs to staff the ESG team).
    4. hiring_surge      — Abnormal spike of postings on sustainability / AI /
       data profiles at one company.
    5. strategic_announcement — "We will double our sustainability team",
       launch of an ESG or AI practice in a consulting firm, geographic
       expansion (new office in Paris, Lyon, Berlin, ...).
    6. acquisition       — Acquisition in the sector.
    7. regulatory        — Regulatory moves (CSRD extension, AI Act, EU
       Taxonomy) that create sector-wide demand.
    8. other             — Use ONLY if mildly relevant but does not fit above.
       If clearly irrelevant, set ``is_relevant=false`` and signal_type="other".

    -----------------
    SCORING RULES
    -----------------
    Each score is an integer between 0 and 100.
    - relevance_score        : how strongly the article matches one of the
      signal types above (0 = unrelated, 100 = textbook example).
    - urgency_score          : how time-sensitive the opportunity is (90+ if
      the user should contact this week; 30-50 if it is a slow-burn watch).
    - fit_with_profile_score : how well the company / role fits the user's
      profile (domains, geographies, company types).

    If ``is_relevant`` is false, set the three scores to 0 and signal_type to
    "other", company_name to "", company_normalized to "", and
    recommended_action to "ignore".

    -----------------
    NORMALIZATION RULES
    -----------------
    - ``company_normalized`` : lowercase the company name, strip accents,
      strip trailing punctuation, drop common legal suffixes (SAS, SARL, SA,
      GmbH, Inc, Ltd, BV, ...). E.g. "Carbone 4 SAS" -> "carbone 4",
      "OpenAI, Inc." -> "openai".
    - All free text fields in French (``summary_fr``, ``suggested_angle``,
      ``target_contact.rationale``) MUST be in French.
    - For each ``key_persons`` entry, set ``is_new_hire=true`` ONLY if the
      article explicitly says the person was just appointed / hired.

    -----------------
    OUTPUT
    -----------------
    Return ONLY a valid JSON object with EXACTLY these keys (no markdown,
    no surrounding prose):

    {{
      "is_relevant": bool,
      "signal_type": "executive_change" | "funding" | "csrd_publication"
                     | "hiring_surge" | "strategic_announcement"
                     | "acquisition" | "regulatory" | "other",
      "company_name": str,
      "company_normalized": str,
      "key_persons": [{{"name": str, "role": str, "is_new_hire": bool}}],
      "relevance_score": int 0..100,
      "urgency_score": int 0..100,
      "fit_with_profile_score": int 0..100,
      "summary_fr": "2-3 phrases en francais",
      "suggested_angle": "Angle pour la candidature spontanee, 1-2 phrases",
      "recommended_action": "contact_immediate" | "research_first"
                             | "monitor" | "ignore",
      "target_contact": {{
        "name": str | null,
        "role": str | null,
        "rationale": str
      }} | null
    }}

    -----------------
    FEW-SHOT EXAMPLES
    -----------------
    ## Example 1 - executive_change (Priority 1, strongest signal)

    Input title: "Carbone 4 nomme Camille Dupont nouvelle Directrice ESG"
    Input content: "Le cabinet Carbone 4, specialiste de la decarbonation, a
    annonce la nomination de Camille Dupont au poste nouvellement cree de
    Directrice ESG. Elle aura pour mission de renforcer l'equipe d'experts."
    Expected output:
    {{
      "is_relevant": true,
      "signal_type": "executive_change",
      "company_name": "Carbone 4",
      "company_normalized": "carbone 4",
      "key_persons": [
        {{"name": "Camille Dupont", "role": "Directrice ESG", "is_new_hire": true}}
      ],
      "relevance_score": 95,
      "urgency_score": 90,
      "fit_with_profile_score": 92,
      "summary_fr": "Carbone 4 cree un poste de Directrice ESG et nomme Camille Dupont. Elle annonce vouloir renforcer son equipe d'experts - signal de recrutement imminent.",
      "suggested_angle": "Feliciter Camille Dupont pour sa nomination et lui proposer de discuter de la structuration de son equipe ESG.",
      "recommended_action": "contact_immediate",
      "target_contact": {{
        "name": "Camille Dupont",
        "role": "Directrice ESG",
        "rationale": "Elle constitue son equipe - moment ideal pour une candidature spontanee."
      }}
    }}

    ## Example 2 - funding (Priority 2)

    Input title: "Sweep leve 22M EUR en Serie B pour son logiciel de mesure carbone"
    Input content: "La climate-tech francaise Sweep annonce une levee de 22M
    EUR menee par Coatue. Les fonds serviront a doubler les equipes data et
    produit, en France et au Royaume-Uni."
    Expected output:
    {{
      "is_relevant": true,
      "signal_type": "funding",
      "company_name": "Sweep",
      "company_normalized": "sweep",
      "key_persons": [],
      "relevance_score": 88,
      "urgency_score": 78,
      "fit_with_profile_score": 90,
      "summary_fr": "Sweep (climate tech FR) leve 22M EUR en Serie B pour doubler les equipes data et produit en France et au Royaume-Uni.",
      "suggested_angle": "Mentionner la levee et proposer un profil Data ESG pour accompagner le doublement de l'equipe data.",
      "recommended_action": "research_first",
      "target_contact": {{
        "name": null,
        "role": "Head of Data ou VP Engineering",
        "rationale": "Recrutement data en preparation suite a la levee."
      }}
    }}

    ## Example 3 - csrd_publication (Priority 3)

    Input title: "Le groupe Bertin publie son premier rapport de durabilite CSRD"
    Input content: "Bertin publie pour la premiere fois un rapport CSRD
    conforme aux nouvelles directives europeennes. L'entreprise indique vouloir
    'structurer durablement' sa demarche ESG."
    Expected output:
    {{
      "is_relevant": true,
      "signal_type": "csrd_publication",
      "company_name": "Groupe Bertin",
      "company_normalized": "bertin",
      "key_persons": [],
      "relevance_score": 75,
      "urgency_score": 60,
      "fit_with_profile_score": 70,
      "summary_fr": "Premier rapport CSRD du groupe Bertin. L'entreprise annonce vouloir structurer durablement sa demarche - typique d'un besoin de renfort ESG.",
      "suggested_angle": "Proposer une expertise data ESG pour les prochains exercices CSRD, en pointant le besoin de capitalisation methodologique.",
      "recommended_action": "research_first",
      "target_contact": {{
        "name": null,
        "role": "Directeur RSE ou Directeur Financier",
        "rationale": "Le sponsor CSRD est en general le Daf ou le directeur RSE."
      }}
    }}

    ## Example 4 - NOT relevant

    Input title: "Apple sort un nouvel iPhone"
    Input content: "Apple a presente hier le nouvel iPhone 17."
    Expected output:
    {{
      "is_relevant": false,
      "signal_type": "other",
      "company_name": "",
      "company_normalized": "",
      "key_persons": [],
      "relevance_score": 0,
      "urgency_score": 0,
      "fit_with_profile_score": 0,
      "summary_fr": "Lancement produit hardware, sans lien avec la sustainability ou l'AI applique au climat.",
      "suggested_angle": null,
      "recommended_action": "ignore",
      "target_contact": null
    }}
    """
).strip()


def render_system_prompt(profile: UserProfile) -> str:
    """Inject the user profile into the versioned prompt template."""
    return CLASSIFIER_PROMPT_V1.format(
        domains=", ".join(profile.domains) or "(none)",
        target_roles=", ".join(profile.target_roles) or "(none)",
        geographies=", ".join(profile.geographies) or "(none)",
        target_company_types=", ".join(profile.target_company_types) or "(none)",
        notes=(profile.notes or "(none)").strip(),
    )


def render_user_prompt(item: ClassifierInput) -> str:
    """Format the article into the user-turn of the prompt."""
    published = item.published_at.isoformat() if item.published_at else "(unknown)"
    content = (item.content or "").strip()
    if len(content) > 4000:
        content = content[:4000] + "... [truncated]"
    return dedent(
        f"""
        Article to classify:

        Source: {item.source}
        URL: {item.url}
        Published: {published}
        Title: {item.title or "(no title)"}

        Content:
        {content or "(no body provided)"}
        """
    ).strip()


__all__ = [
    "CLASSIFIER_PROMPT_V1",
    "CLASSIFIER_PROMPT_VERSION",
    "render_system_prompt",
    "render_user_prompt",
]
