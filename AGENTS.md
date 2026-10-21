# Live VPN operations

- These nodes carry the user's active connection. Diagnose first; use the standby exit
  for experiments and complete an authenticated tunnel check before touching another node.
- Xray runs as `nobody`, not root. A JSON/config test run as root does not prove service
  readability. Preserve the installed access mode; never deploy root-owned `0600` as
  `/usr/local/etc/xray/config.json`.
- Use `xray-safe-apply.py install FILE` to stage without a local restart, and
  `xray-safe-apply.py apply FILE` or `restart` for a checked restart with rollback.
  Verify a stable PID and listeners, then actual VLESS traffic. Immediate `is-active`
  after restart is insufficient. Keep at least one independently working path available.
- `render.sh` also changes remote nodes, AWG config, templates and other services. Do not
  run it as a harmless diagnostic command. Do not roll out an untested routing experiment
  to every subscriber just because one web request gives a different region or status.
- An HTTP 200 or TikTok web-region `NL` is not confirmation of fresh videos in the iPhone
  app. Report network and app-feed verification separately; obtain a phone test for the latter.
- TikTok has a separate optional policy from Google. Keep the existing exit until the same
  iPhone/account has verified the alternative; a prepared failover test is not approval to
  enable it. Regenerate candidates from current users/configs rather than applying old snapshots.
- Windows monitoring uses the existing SafeChillVPN-Healthcheck task and its per-user hidden
  launcher. Do not create a duplicate, switch Clash selectors during a check, or copy the
  Telegram bot token to the PC. A local controller being stopped is not a VPN path failure.
- Read SSH endpoints from the live environment. A node can have both a management address
  and an extra VPN address. Never rotate keys, users, addresses or paid resources to fix a
  simple process/configuration failure.
- Python 3 is a VPN runtime dependency (bot, subscriptions, deployment checks). Remove only
  explicitly unwanted CLI tools or extra runtimes, not the system interpreter.
