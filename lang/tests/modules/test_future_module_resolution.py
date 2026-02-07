import pytest


@pytest.mark.skip(reason="Deferred: module resolution/import graph/export enforcement not implemented yet")
def test_resolve_reports_correct_span_file():
    pass


@pytest.mark.skip(reason="Deferred: import resolver should include available export candidates in diagnostics")
def test_diagnostic_contains_candidate_list():
    pass


@pytest.mark.skip(reason="Deferred: import cycle detector not implemented yet")
def test_cycle_detector_outputs_minimal_cycle():
    pass

