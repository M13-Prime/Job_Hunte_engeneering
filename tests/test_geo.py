"""Tests for the country → language mapping used by the pre-collect filter."""

from __future__ import annotations

from signal_tracker.utils.geo import (
    geographies_to_languages,
    languages_to_gdelt_clause,
)


def test_french_speaking_geographies_map_to_fr() -> None:
    assert "fr" in geographies_to_languages(["France"])
    assert "fr" in geographies_to_languages(["Belgique"])
    assert "fr" in geographies_to_languages(["Suisse"])


def test_open_label_returns_empty_no_filter() -> None:
    # When the user says Europe / Global, we explicitly do NOT restrict.
    assert geographies_to_languages(["Europe"]) == set()
    assert geographies_to_languages(["Global"]) == set()
    # Even mixed with a known country, an open label wins (intent: open).
    assert geographies_to_languages(["France", "Global"]) == set()


def test_union_across_multiple_countries() -> None:
    out = geographies_to_languages(["France", "UK"])
    assert out == {"fr", "en"}


def test_unknown_only_labels_default_to_no_filter() -> None:
    # We don't want to silently drop articles because someone typed a
    # city or a sector we don't recognize.
    assert geographies_to_languages(["Lyon", "ESG"]) == set()
    assert geographies_to_languages([]) == set()


def test_unknown_alongside_known_keeps_only_known() -> None:
    out = geographies_to_languages(["France", "Lyon"])
    assert out == {"fr"}


def test_case_and_whitespace_insensitive() -> None:
    assert geographies_to_languages(["  france  "]) == {"fr"}
    assert geographies_to_languages(["FRANCE"]) == {"fr"}


def test_gdelt_clause_single_language() -> None:
    assert languages_to_gdelt_clause({"fr"}) == "sourcelang:french"


def test_gdelt_clause_multiple_languages_wrapped() -> None:
    clause = languages_to_gdelt_clause({"fr", "en"})
    # Order is sorted for stability.
    assert clause == "(sourcelang:english OR sourcelang:french)"


def test_gdelt_clause_empty_returns_empty_string() -> None:
    assert languages_to_gdelt_clause(set()) == ""


def test_gdelt_clause_skips_unknown_languages() -> None:
    # We only have a handful of language mappings — unknown ISO codes
    # (eg "zz") shouldn't generate broken GDELT syntax.
    assert languages_to_gdelt_clause({"zz"}) == ""
    assert languages_to_gdelt_clause({"fr", "zz"}) == "sourcelang:french"
