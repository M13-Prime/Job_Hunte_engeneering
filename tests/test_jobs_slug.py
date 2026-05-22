"""Tests for the slug heuristic."""

from __future__ import annotations

from signal_tracker.jobs.slug import slug_candidates


def test_simple_one_word_company() -> None:
    assert slug_candidates("Sweep") == ["sweep"]


def test_multi_word_dash_underscore_join_and_first_word() -> None:
    out = slug_candidates("Carbone 4")
    assert "carbone-4" in out
    assert "carbone4" in out
    assert "carbone_4" in out
    assert "carbone" in out


def test_strips_accents_and_legal_suffix() -> None:
    out = slug_candidates("Bénéfik SAS")
    assert "benefik" in out


def test_empty_input_returns_empty_list() -> None:
    assert slug_candidates("") == []


def test_dedup_keeps_order() -> None:
    out = slug_candidates("Plan")  # one word -> all variants collapse to same value
    assert out == ["plan"]
