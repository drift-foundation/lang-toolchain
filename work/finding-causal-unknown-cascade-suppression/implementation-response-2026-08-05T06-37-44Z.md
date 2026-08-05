# Baton message

Timestamp: 2026-08-05T06-37-44Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: b49addf2e8d5

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T05-38-40Z-e72c577d1cb3-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T05-38-40Z`
Full-suite report (run-all-tests.sh): ONE related failure, root-caused to the
slice's receiver-position causal suppression; the suite aborted at that point
per set -e, so the run is INCOMPLETE.  State: perf-protocols OK; memcheck
lane failed in the driver stage at 1 failed / 2396 passed / 10 skipped
(43m32s); the ASAN lane never started.  Not rerun pending this ruling.

Failure: lang/tests/driver/test_std_json_regressions.py::
test_std_json_legacy_node_mutation_helpers_are_rejected.

The fixture calls all four REMOVED std.json legacy helpers and asserts each
NAME appears in the diagnostics:

    var arr = json.JsonNode::new_array();      // primary: names new_array
    arr.array_push(json.JsonNode::Number("1")); // was: "no matching method
                                                //  'array_push' for receiver
                                                //  Unknown" — a CASCADE
    var obj = json.JsonNode::new_object();     // primary: names new_object
    obj.object_set("k", move arr);             // same cascade shape

new_array / new_object still diagnose (their assoc-call rejections are the
primaries and both still fire).  array_push / object_set were only ever
named by the method-resolution CASCADE over the poisoned Unknown receivers —
exactly the "no matching method for receiver Unknown" noise this slice's
caused-receiver suppression now correctly withholds (the receiver's Unknown
is fully explained by the new_array/new_object primaries; method resolution
on an Unknown receiver cannot legitimately claim array_push itself is
invalid).  The test encodes the pre-causal cascade UX as if it were the
rejection contract.

My recommendation: update the test to the causal-presentation contract —
split the fixture so each removed helper is exercised against a VALID
receiver construction (e.g. `var arr = json.new_array_node();`-style current
constructors, or two fixtures: one pinning the two assoc-fn primaries with
NO method-cascade noise, one calling array_push/object_set on legitimately
constructed nodes so their own rejections are PRIMARY diagnostics that do
name them).  That preserves the test's migration-UX intent (every removed
helper gets an actionable message) while dropping the dependence on
suppressed cascades.  This is an EXISTING-test edit outside my current
authorization, so I am requesting approval for it (or an alternative ruling
— e.g. if you judge the method-name-bearing cascade worth keeping for
unknown receivers, I would instead carve method suppression to skip
receivers whose cause category is an assoc-call rejection, though that
weakens the one-primary contract this slice just pinned).

On ruling I will: apply the approved edit, rerun the failing file focused,
then restart run-all-tests.sh from the top and report the complete result on
this thread.
