# Dispatch calibration

One row per finished dispatch, appended by `/retro` from the dispatch record and the handoff.
The rubric it calibrates is `harness/dispatch-rubric.md` in the harness repo. `outcome` is
`clean` (one attempt, done), `resumed` (needed the owner), or `wasted` (the work did not
survive). `note` says what the retro concluded about the choice. `Effort` is `default` when
the dispatch left it to the vendor. `Resolved` is the model id the worker actually ran.

| Dispatch | Row | Model | Resolved | Effort | Budget | Cost | Attempts | Outcome | Note |
|---|---|---|---|---|---|---|---|---|---|
