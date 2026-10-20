# Windows candidate validation — 2026-10-20

Current continuation result: **portable offline tests and installed-browser smoke pass** at `311c95e0810d31520067d3338d9848e17cdefd60`. See the continuation evidence below. The earlier native PowerShell `--code-file` BOM failure remains assigned to the login owner and was not changed or re-tested here.

Initial result (historical, `fe3a01b`): **not green**. Windows offline tests had six reported failures; installed Edge launched correctly. A separate native PowerShell probe reproduced a BOM incompatibility in `--code-file`. No production source changes, fresh login, Duo request, push, or PR.

## Candidate and environment

- Local isolated branch `t3/b3742555` fast-forwarded with `git merge --ff-only fix/browser-login` from `31d28c8` to **`fe3a01be1e41e25dff30d04f963651f8e5058466`**.
- `fix/cli-commands` resolves to `5384627b2f6f981b3616063fe7cfef3341e5fdee`; it was **not combined or tested** in this lane.
- Read `AGENTS.md`, coding standards, `CONTEXT.md`, ADRs 0001–0005, and the Blackboard operational skill. Earlier Windows evidence informed scope only; all results below are from this run.
- SSH: `win-lan`, host `LAPTOP-OPDFF0S5`; no fallback alias needed. Native Windows PowerShell **5.1.26100.9711**, Python **3.13.15** (non-Store install). Free C: space approximately 628 GiB.
- New owned root: **`C:\Users\jason\bb-test-codex-20261020`**; checkout: **`C:\Users\jason\bb-test-codex-20261020\repo`**. The target did not exist before setup.
- Reused `/tmp/bb-login.bundle` after `git bundle verify` confirmed complete history and the exact candidate head. Transfer contained tracked Git history, no auth/profile payload.
- Bundle SHA-256 matched on both machines: `4898521335f17146481b9d1daedd3a54e42ead56a8e6dfd37f9a02bae0154ce8`. Remote clone head matched; `git fsck --full` passed.
- Created checkout-local `.venv`; installed `-e '.[dev]'` without browser downloads. Playwright **1.64.0**, pytest **9.1.1**, Ruff **0.17.1**. Python commands used UTF-8 mode.

## Offline validation

Actual command, in the remote checkout:

```powershell
$env:PYTHONUTF8 = '1'
$env:PLAYWRIGHT_BROWSERS_PATH = 'C:\Users\jason\bb-test-codex-20261020\empty-playwright-cache'
& .\.venv\Scripts\python.exe -X utf8 -m pytest tests -q -rs --durations=10
& .\.venv\Scripts\python.exe -X utf8 -m ruff check .
```

- Pytest: **6 failed, 348 passed, 3 skipped, 95 subtests passed in 50.48s**; process elapsed **51.68s**, exit **1**. Browser fixtures used synthetic/local responses; the opt-in real-login fixture stayed disabled. No real SSO was initiated.
- Ruff: **all checks passed**, elapsed **4.73s**, exit **0**.
- Skips: opt-in `test_browser_smoke.py` fixture; POSIX terminal polling in `test_login_reliability.py`; macOS/rumps menubar in `test_v2_features.py`.
- Native spawned-process login-lock exclusion and wait/reacquisition tests passed. Windows console synthetic-key tests (editing, extended keys, masked echo, cancellation) and plain UTF-8 file freshness/deletion tests passed. Actual SSH non-TTY terminal reader returned immediately (0.000s); interactive `msvcrt` input was not exercised with a real console.

Reported failures and evidence for the owner:

1. `test_browser_selection.py::TestCandidateOrder::test_macos_chrome_first_then_edge_brave_then_bundled_last` and `test_uninstalled_default_is_skipped`: simulated macOS tests clear the environment and call host-native Windows `Path.home()`, raising `RuntimeError: Could not determine home directory.`
2. Same class, `test_linux_path_chromium_precedes_bundled`: mocked POSIX path `/opt/bin/chromium` becomes a Windows path string; its fragment-based `is_file` stub misses it. First candidate is bundled, producing `KeyError: 'executable_path'`.
3. `test_http_refresh.py::TestRefresh::test_full_chain_refreshes_and_replays_idp_storage`: refresh/cookie/storage assertions succeed, but POSIX permission assertion fails: `st_mode & 0o077` is **54**, expected 0. Windows mode bits do not establish ACL privacy; a portable acceptance check is needed. This run did not assess ACL privacy.
4. `test_login_flow_safety.py::TestLoginSafety::test_sms_listener_never_prints_passcode_sender_or_message`: both `sqlite` and `imsg` subcases fail (`None` instead of the fixture code). The test simulates `darwin` and mocks message getters, but leaves `check_sms_access()` real; Windows has no macOS Messages prerequisite. These two subcases contribute to the reported failure count.

These failures are test portability problems on the inspected paths, not proof of real Windows browser-login failure. No test/source edits or broad debugging followed.

## Installed browser smoke

A separate harness called the shared launcher with temporary profiles containing spaces, navigated only to `about:blank`, closed its contexts, and removed its temporary profile tree. It used an absent Playwright browser-cache directory so downloaded browsers were unavailable.

- OS default: **Firefox**. Installed candidates: Microsoft Edge, Google Chrome.
- Selected **Microsoft Edge 155.0.4283.45**.
- Sync headless, sync visible, and async headless launch/navigation/close all passed in **3.56s** total.
- Across all three launches, stderr contained exactly one line:
  `Using Microsoft Edge: your default browser (Firefox) can't run the Blackboard profile.`
- Temporary profile removal verified. No existing browser profile, browser download, or live login page was used by this smoke.

## Native PowerShell code-file bug

A separate bounded probe used synthetic passcodes only. Windows PowerShell 5.1:

```powershell
Set-Content -LiteralPath $path -Value '246810' -Encoding UTF8
```

This writes a UTF-8 BOM. `collect_passcode([file_source(path, poll=0.02)], sent_at=time.time()-1, timeout=0.25)` reports invalid input, returns no code, and deletes the file. Writing the identical content through `.NET UTF8Encoding($false)` succeeds and deletes the file.

Cause: `file_source` reads `encoding='utf-8'`; `.strip()` retains U+FEFF, which `normalize_passcode` rejects. This is a reproducible `--code-file` interoperability problem with a common native Windows writer. Owner should handle the BOM or document a precise BOM-free writer; no fix was applied here.

## Cached auth and installed CLI

`core/config.py` has no `BB_SESSION_DIR`/config-location environment override in this candidate. Source-relative storage was isolated by the new editable installation. Only `config.json`, `.session/cookies.json`, and existing `.session/session_metadata.json` were copied **inside Windows** from `C:\Users\jason\Desktop\school files\tools\blackboard-scraper`. No browser profile or IdP storage was copied; no secret contents were printed or transferred to Mac.

- Candidate `probe_session(timeout=8)` against `/learn/api/public/v1/users/me`: **expired**, **0.14s** (later check probe 0.09s). Account identity not established. No refresh/login attempted; authenticated academic command smoke skipped.
- Installed `bb --help`: exit **0**, help present, **0.957s**, empty stderr. This one cold measurement exceeds the repository's 0.3s help budget; no benchmark/debugging followed.
- Installed `bb courses --json`: exit **0**, exactly one parseable JSON document, five configured courses, **0.429s**, empty stderr. This is local configuration smoke, not live academic-data acceptance.
- Installed `bb check`: expired marker present, no active marker; exit **0**, **0.501s**, empty stderr. Exit 0 does not establish valid auth.
- Before/after hashes of source config, cookies, and metadata matched. Existing synced checkout and earlier test directories were not modified. The new checkout retains ignored config/session copies for the continuation; `git status --short` showed only the editable-install artifact `blackboard_scraper.egg-info/`, with no tracked modifications.

## Continuation invocation

Retained remote harnesses outside tracked source: `run_validation.py`, `file_probe.py`, `cli_smoke.py`, `check_smoke.py` in the owned root. The validation harness prints each child command's exit; its outer exit was 0 despite pytest failing, so use the pytest exit/result above. It has a 300s child timeout; transport used a 400s bound.

Local transport helper `/tmp/bb-windows-ps.py` encodes stdin as UTF-16LE/Base64 and calls `ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 win-lan powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ...`, with a local subprocess timeout. Exact run:

```sh
python3 /tmp/bb-windows-ps.py 400 <<'PS'
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PYTHONUTF8 = '1'
$root = 'C:\Users\jason\bb-test-codex-20261020'
Set-Location (Join-Path $root 'repo')
& .\.venv\Scripts\python.exe -X utf8 (Join-Path $root 'run_validation.py')
exit $LASTEXITCODE
PS
```

Parent can transfer a **new, distinctly named** final combined bundle into this owned root, fetch it in `repo`, verify the intended exact head, and rerun the direct pytest/Ruff commands. The installation is editable, so a source-only update needs no global launcher change. Keep the synced checkout and earlier test directories untouched. Fresh Windows SSO/Duo, terminal input, and final combined-command live smoke remain for the parent-authorized later phase.


## Portable-test continuation — 2026-10-20

### Exact candidate and reproduction

- Own local worktree: `/Users/king/.t3/worktrees/blackboard-scraper/t3-7b85c1d3`; branch **`test/windows-portability-2026-10-20`**.
- First operation was `git merge --ff-only review/blackboard-candidate-2026-10-20`. That ref had advanced to **`788286708ccf4bbf6413d6971924a2b6f0c4f341`**, the documentation-only skill update immediately after the requested integrated candidate **`a9686cb`**. No older Windows branch was merged.
- Copied the preceding investigation document from the prior worktree, retaining its historical results above.
- Transferred a complete tracked-history bundle, verified SHA-256 on both hosts (`357610ff5dacfd48e0953af67a5fa842482e187dfeb80f68336382f1238f9cec`), and verified it with remote `git bundle verify`.
- In the existing owned remote repo, created **`test/windows-portability-2026-10-20`** at `7882867`; no reset, clean, or changes to the synced Windows source checkout.
- Before any test edits, ran `python -X utf8 -m pytest tests/test_browser_selection.py tests/test_login_flow_safety.py tests/test_http_refresh.py -q -rs` on native Windows: **6 failed, 43 passed, 16 subtests passed in 1.26s**. All six previously reported failures reproduced: two missing-home exceptions, the Linux separator stub mismatch, the two Messages access subcases, and the POSIX mode assertion (`54 != 0`).

### Task-owned corrections

Code/test commit: **`311c95e0810d31520067d3338d9848e17cdefd60`** (`test: make browser and login fixtures portable to Windows`). Only the four authorized files changed:

- `tests/test_browser_selection.py`: supply a deterministic `Path.home()` fixture while clearing host environment variables. Normalize separators only inside the simulated installed-file predicate; existing exact executable-path and candidate-order assertions remain intact.
- `tests/test_login_flow_safety.py`: mock successful `check_sms_access()` for both simulated macOS message sources. Both code-return and private-content suppression assertions still execute, independently of actual Messages permissions.
- `tests/test_http_refresh.py`: retain refresh, replay, cookie, and persisted-storage checks on every OS; additionally assert exact persisted IdP JSON and absence of leftover temporary files. Move mode protection into a separate POSIX-only test and require **0600 for both cookies and IdP storage**. This does not claim Windows ACL privacy; no class or portable behavior test is skipped.
- `.gitignore`: ignore generated `*.egg-info/` from the existing editable install.

Local macOS focused validation: **48 passed, 20 subtests passed in 0.33s**; full-repo Ruff passed; `git diff --check` passed. No production source edits.

### Final native Windows validation

Transferred `/tmp/bb-windows-portability-fixes-20261020.bundle` to the owned root as `portability-fixes.bundle`. SHA-256 matched on both hosts: **`b01a6e89ba6c40d5ec02a0f38968b4c550b8334ea2515e70a431ea5c6c021378`**. Remote bundle verification passed; `git fetch` followed by `git merge --ff-only FETCH_HEAD` produced exactly **`311c95e0810d31520067d3338d9848e17cdefd60`**. Existing ignored config/session copies remained in place.

Reused the existing outside-repo `run_validation.py` harness with native Python **3.13.15**, UTF-8 mode, and `PLAYWRIGHT_BROWSERS_PATH` pointing at the absent owned `empty-playwright-cache` directory:

- `python -X utf8 -m pytest tests -q -rs --durations=10`: **435 passed, 4 skipped, 97 subtests passed in 34.96s**; child exit **0**, process elapsed **35.77s**.
- `python -X utf8 -m ruff check .`: **all checks passed**; child exit **0**, elapsed **0.19s**.
- Four skips: opt-in `test_browser_smoke.py`; the new POSIX mode-only test; POSIX terminal polling; macOS/rumps menubar. Portable refresh persistence and both SMS privacy subcases passed on Windows.
- The harness's outer exit does not aggregate child exits; both child exits above were read explicitly and were zero. Only one full-suite run was performed after the test corrections.

Installed-browser smoke in the same harness:

- OS default **Firefox**; installed candidates **Microsoft Edge, Google Chrome**.
- **Microsoft Edge 155.0.4283.45** passed sync headless, sync visible, and async headless `about:blank` navigation/close using separate temporary profiles with spaces.
- Total browser smoke **5.08s**. Exactly one stderr fallback explanation across all launches: `Using Microsoft Edge: your default browser (Firefox) can't run the Blackboard profile.`
- Temporary profile removal verified. `empty-playwright-cache` still did not exist after validation; no browser download or real login/Duo was initiated.

### Preservation and remaining scope

Before/after SHA-256 matched for all three files in **both** the synced original checkout and the owned test checkout, and the two copies matched each other:

- `config.json`: `6c71a3c5f1722638d420c95eb3729123c4cb80674cec0ae580cafe551a774702`
- `.session/cookies.json`: `3b345841861b6b389b527a33ce65f964975953d3921a7bdb07d901201d868410`
- `.session/session_metadata.json`: `b75c125c1e87733a0fef12e9cbd9720996668656dd2b52c29e7a6319a757d589`

Final remote worktree status was clean on `test/windows-portability-2026-10-20`; editable egg-info was correctly ignored. No push, PR, main update, source fix, new subagent, fresh SSO/Duo, or repeated full-suite run. No actual code failure occurred in this round's tests/smoke. The previously reproduced PowerShell BOM bug is still outside this lane, and these results do not establish final combined-candidate login acceptance after the login owner's remaining fixes.
