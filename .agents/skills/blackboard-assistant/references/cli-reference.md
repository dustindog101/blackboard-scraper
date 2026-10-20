# CLI reference

Use the selected checkout's [maintained CLI reference](../../../../docs/CLI_REFERENCE.md)
and `bb <command> --help`. The executable may point at another checkout, so confirm
its location before using candidate-only flags. This compatibility link replaces a
duplicated command catalog.

For `bb answers`, export a template, fill `changes[]`, and always preview with
`--file FILE` first. Add `--apply` only on the student's explicit save request.
It saves supported answers in an existing attempt and never starts or submits one.
See the maintained reference for the schema, concurrency limitation and exit codes.

Answer-save review safeguards: fresh definition/choice-order checks, per-PATCH bounded
GET confirmation, one final snapshot covering every requested answer, and an OS lock
held through readback. Missing safety/deadline metadata refuses. HTTP 401 rejects with
no persistence and may fall back only before HTTP has persisted any answer; 403/5xx/
timeouts never fall back. Exit 3 is pre-dispatch; exit 4 reports an unsuccessful run
after dispatch. Validate --out before writes; later file failures preserve stdout and
the save exit code. Ctrl-C stops new writes while producing a reconciliation receipt.
