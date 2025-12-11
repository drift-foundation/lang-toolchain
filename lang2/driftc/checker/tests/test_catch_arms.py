from __future__ import annotations

import pytest

from lang2.driftc.checker.catch_arms import CatchArmInfo, validate_catch_arms


def test_valid_arms_pass():
	arms = [
		CatchArmInfo(event_name="A"),
		CatchArmInfo(event_name="B"),
		CatchArmInfo(event_name=None),  # catch-all last
	]
	validate_catch_arms(arms, known_events={"A", "B"})


def test_duplicate_event_rejected():
	arms = [
		CatchArmInfo(event_name="A"),
		CatchArmInfo(event_name="A"),
	]
	with pytest.raises(RuntimeError):
		validate_catch_arms(arms, known_events={"A"})


def test_unknown_event_rejected():
	arms = [CatchArmInfo(event_name="A")]
	with pytest.raises(RuntimeError):
		validate_catch_arms(arms, known_events=set())


def test_multiple_catch_all_rejected():
	arms = [
		CatchArmInfo(event_name=None),
		CatchArmInfo(event_name=None),
	]
	with pytest.raises(RuntimeError):
		validate_catch_arms(arms, known_events=set())


def test_catch_all_not_last_rejected():
	arms = [
		CatchArmInfo(event_name=None),
		CatchArmInfo(event_name="A"),
	]
	with pytest.raises(RuntimeError):
		validate_catch_arms(arms, known_events={"A"})

