from __future__ import annotations

import pytest

from lang2.driftc.checker.catch_arms import CatchArmInfo, validate_catch_arms
from lang2.driftc.core.span import Span


def test_valid_arms_pass():
	arms = [
		CatchArmInfo(event_fqn="A"),
		CatchArmInfo(event_fqn="B"),
		CatchArmInfo(event_fqn=None),  # catch-all last
	]
	validate_catch_arms(arms, known_events={"A", "B"})


def test_duplicate_event_rejected():
	arms = [
		CatchArmInfo(event_fqn="A"),
		CatchArmInfo(event_fqn="A"),
	]
	with pytest.raises(RuntimeError):
		validate_catch_arms(arms, known_events={"A"})


def test_unknown_event_rejected():
	arms = [CatchArmInfo(event_fqn="A")]
	with pytest.raises(RuntimeError):
		validate_catch_arms(arms, known_events=set())


def test_multiple_catch_all_rejected():
	arms = [
		CatchArmInfo(event_fqn=None),
		CatchArmInfo(event_fqn=None),
	]
	with pytest.raises(RuntimeError):
		validate_catch_arms(arms, known_events=set())


def test_catch_all_not_last_rejected():
	arms = [
		CatchArmInfo(event_fqn=None),
		CatchArmInfo(event_fqn="A"),
	]
	with pytest.raises(RuntimeError):
		validate_catch_arms(arms, known_events={"A"})


def test_validate_reports_with_span_when_available():
	span = Span(file="test", line=1, column=2)
	arms = [CatchArmInfo(event_fqn="UnknownEvt", span=span)]
	diags: list = []
	validate_catch_arms(arms, known_events=set(), diagnostics=diags)
	assert diags and diags[0].span == span


def test_duplicate_event_reports_note_with_previous_span():
	span1 = Span(file="test", line=1, column=1)
	span2 = Span(file="test", line=2, column=1)
	arms = [CatchArmInfo(event_fqn="A", span=span1), CatchArmInfo(event_fqn="A", span=span2)]
	diags: list = []
	validate_catch_arms(arms, known_events={"A"}, diagnostics=diags)
	assert diags and "previous catch for 'A'" in diags[0].notes[0]
	assert str(span1.line) in diags[0].notes[0]
