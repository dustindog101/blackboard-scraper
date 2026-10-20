# Candidate read-command validation — 2026-10-20

Mac read-only acceptance using a copied protected session; `/users/me` matched the configured account alias. No browser, auth send, attempt, submission or notification was used. Browser initialization was guarded to fail if attempted. Raw responses and identifiers were not saved. This is command acceptance, not coursework/provider completion.

| Command | Exit | Seconds | JSON parsed |
|---|---:|---:|---|
| `bb grades --summary` | 0 | 1.042 | text |
| `bb grades` | 0 | 0.712 | text |
| `bb grades --json` | 0 | 0.703 | True |
| `bb due` | 0 | 0.747 | text |
| `bb due --json` | 0 | 0.741 | True |
| `bb due overdue` | 0 | 0.651 | text |
| `bb briefing` | 0 | 0.731 | text |
| `bb briefing --json` | 0 | 0.755 | True |
| `bb assignments` | 0 | 1.184 | text |
| `bb assignments --json` | 0 | 1.171 | True |
| `bb announcements` | 0 | 0.354 | text |
| `bb announcements --json` | 0 | 0.352 | True |
| `bb activity` | 0 | 0.198 | text |
| `bb activity --json` | 0 | 0.183 | True |
| `bb terms` | 0 | 0.307 | text |
| `bb terms --json` | 0 | 0.359 | True |
| `bb discussions ENGL100` | 0 | 0.375 | text |
| `bb discussions ENGL100 --json` | 0 | 0.589 | True |
| `bb outline IS410` | 0 | 0.807 | text |
| `bb outline IS410 --json` | 0 | 0.833 | True |
| `bb search Syllabus` | 0 | 2.432 | text |
| `bb search Syllabus --json` | 0 | 2.497 | True |
| `bb courses` | 0 | 0.09 | text |
| `bb courses --json` | 0 | 0.072 | True |
| `bb profile` | 0 | 0.188 | text |
| `bb profile --json` | 0 | 0.199 | True |
| `bb calendar` | 0 | 0.372 | text |
| `bb calendar --json` | 0 | 0.357 | True |

These timings are one observation per invocation, not latency guarantees. The branch still awaits final login and Windows integration checks.

Calendar initially failed the no-browser guard; the CLI eager launch was removed and the two calendar rows above were rechecked successfully.
