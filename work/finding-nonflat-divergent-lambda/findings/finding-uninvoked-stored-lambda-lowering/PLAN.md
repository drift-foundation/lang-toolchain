# PLAN: finding-uninvoked-stored-lambda-lowering

Not started (QUEUED). Sketch when picked up:
1. Red driver regression from the repro; probe the named-fn-body variant too.
2. Trigger scan.
3. Root cause: give uninvoked unannotated stored lambdas a lowering route
   (likely the LambdaFnSpec/fnptr-const path at binding time, or a clean
   checker rejection if the language rules them out) — decide with reviewer.
4. Pins green.
