# Plan: Baton CLI trial

1. [done] Add the extractable repository-local `tools/baton/baton` executable.
2. [done] Exercise scan, role enforcement, atomic claim races, immutable target
   validation, response publication, signoff, and human stdin decisions in an
   isolated temporary repository.
3. [done] Use it on one real reviewer handoff without manually reading the target.
4. [done] Record operational gaps in `PROGRESS.md` before changing protocol v3.
5. If the trial is sound, propose the tool-backed protocol revision and only
   then distribute the script/protocol pair to peer projects.

No compiler, language spec, stdlib, or compiler-version change is involved.
