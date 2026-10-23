"""Group classified signals by company for the Signaux page.

Pure helpers (no DB access): the route loads ``(Signal, RawItem)`` rows and
this module turns them into one card per company, with the key people found
in the articles merged and de-duplicated.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Person:
    """A key person mentioned for a company."""

    name: str
    role: str = ""
    is_new_hire: bool = False
    source_url: str | None = None


@dataclass
class CompanyGroup:
    """All the signals of one company, ready to render as a card."""

    key: str
    name: str
    best_score: float
    signals: list[tuple[Any, Any]] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    hq_country: str | None = None
    countries: list[str] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    has_new: bool = False

    @property
    def tier(self) -> str:
        if self.best_score >= 80:
            return "hot"
        return "investigate" if self.best_score >= 60 else "watch"

    @property
    def top_signal(self) -> Any:
        return self.signals[0][0]

    @property
    def count(self) -> int:
        return len(self.signals)


def _clean(value: Any) -> str:
    return str(value).strip() if value else ""


def _people_from_signal(signal: Any, source_url: str | None) -> list[Person]:
    found: list[Person] = []
    for entry in signal.key_persons or []:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name"))
        if name:
            found.append(Person(
                name=name,
                role=_clean(entry.get("role")),
                is_new_hire=bool(entry.get("is_new_hire")),
                source_url=source_url,
            ))
    contact = signal.target_contact
    if isinstance(contact, dict):
        name = _clean(contact.get("name"))
        if name:
            found.append(Person(
                name=name, role=_clean(contact.get("role")), source_url=source_url
            ))
    return found


def merge_people(people: list[Person], limit: int = 6) -> list[Person]:
    """De-duplicate by name (case-insensitive), keeping the most informative entry."""
    merged: dict[str, Person] = {}
    for p in people:
        key = p.name.casefold()
        cur = merged.get(key)
        if cur is None:
            merged[key] = p
            continue
        if not cur.role and p.role:
            cur.role = p.role
        cur.is_new_hire = cur.is_new_hire or p.is_new_hire
        if not cur.source_url and p.source_url:
            cur.source_url = p.source_url
    # New hires first, then people with a known role.
    ordered = sorted(merged.values(), key=lambda p: (not p.is_new_hire, not p.role))
    return ordered[:limit]


def group_by_company(
    rows: Sequence[Any], focus_run_id: int | None = None
) -> list[CompanyGroup]:
    """One group per company, best score first; signals sorted by score inside."""
    groups: dict[str, CompanyGroup] = {}
    people: dict[str, list[Person]] = {}
    hq_votes: dict[str, Counter[str]] = {}
    for row in rows:
        signal, raw = row[0], row[1]
        key = signal.company_normalized or (signal.company_name or "").casefold() or "?"
        group = groups.get(key)
        if group is None:
            group = groups[key] = CompanyGroup(
                key=key,
                name=signal.company_name or "(sans nom)",
                best_score=signal.total_score,
            )
            people[key] = []
            hq_votes[key] = Counter()
        group.signals.append((signal, raw))
        group.best_score = max(group.best_score, signal.total_score)
        if signal.signal_type and signal.signal_type not in group.types:
            group.types.append(signal.signal_type)
        if signal.hq_country:
            hq_votes[key][signal.hq_country] += 1
        for c in signal.active_countries or []:
            if c and c not in group.countries:
                group.countries.append(c)
        if focus_run_id and signal.search_run_id == focus_run_id:
            group.has_new = True
        people[key].extend(_people_from_signal(signal, getattr(raw, "url", None)))

    for key, group in groups.items():
        group.signals.sort(key=lambda sr: sr[0].total_score, reverse=True)
        group.people = merge_people(people[key])
        if hq_votes[key]:
            group.hq_country = hq_votes[key].most_common(1)[0][0]
    return sorted(groups.values(), key=lambda g: g.best_score, reverse=True)
