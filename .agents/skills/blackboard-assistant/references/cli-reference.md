# CLI reference

Use the selected checkout's [maintained CLI reference](../../../../docs/CLI_REFERENCE.md)
and `bb <command> --help`. The executable may point at another checkout, so confirm
its location before using candidate-only flags. This compatibility link replaces a
duplicated command catalog.

For `bb answers`, preview repeatable `--set N=A,B` / `--set N=False` / `--set N=clear`,
or export a template, fill `changes[]`, then preview with `--file FILE`. Add `--apply`
only on the student's explicit save request. Letters follow bb's list, which may
differ from Blackboard's shuffled display; never match by text. Runtime shorthand
baselines have no file expiry and share file mode's safe save pipeline.

Only an explicit request to start permits `--new-attempt`. It runs apply's safety,
count and deadline checks, uses an existing IN_PROGRESS attempt, or starts one and
confirms exactly one new IN_PROGRESS attempt by GET. HTTP first, installed browser
next, optional bundled Playwright last. A dispatched start never retries or falls
back (even 401/404); ambiguity exits 4. Timer/access-code prompts refuse. Combined
start/save needs a readable prior question list for preflight; otherwise start
separately. Creation request observed live from the Ultra UI; `bb`'s own start is offline-tested. Uses up one attempt. Never submits.

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded by Ultra as `multipleanswer` with `answerSelectionLimit: 1`);
and true/false (`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank,
matching, ordering, calculated, file response, and anything else are unsupported:
**answer in Blackboard**. Type support does not establish live acceptance of every clear case.

Fresh fingerprint/baseline checks, per-question GET confirmation, final readback and
an OS lock are shared. They do not provide CAS. Exit 3 refuses before start/save
dispatch; exit 4 follows an unsuccessful dispatch. Preserve receipts and inspect
Blackboard before retrying. See the maintained reference for schema and full details.
