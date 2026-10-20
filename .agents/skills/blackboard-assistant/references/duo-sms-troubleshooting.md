# Login recovery

Use plain `bb login` for recovery; reserve `--force` for deliberate clean
reauthentication. Never interpret a timeout or service failure as proof of expiry.
An active remembered UMBC session may renew Blackboard over HTTPS without Duo.

When Duo is required, the login detects offered methods. Messages, configured
Telegram, an interactive terminal and `--code-file` can supply a fresh passcode;
accounts without SMS may offer push or a manual passcode. Keep one running login
alive while supplying its response, rather than restarting and resending codes.

If macOS Messages is unavailable in this launcher, use the remaining offered input
sources. Full Disk Access is launcher-specific; a failure here does not establish
that the user's Terminal lacks permission. Never copy message bodies or codes into
logs or notes. On Windows, do not expect the macOS Messages reader.

For file handoff, use a private temporary path, wait for `WAITING_FOR_CODE`, then
write the fresh code while that process waits. Old input is discarded and consumed
input deleted. See [CLI authentication](../../../../docs/CLI_REFERENCE.md) and
`bb login --help` for supported flags and deadlines. Use `bb login --manual` when
the offered verification requires direct user interaction.
