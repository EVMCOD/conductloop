# Daily recap cron

The Boardroom ships a launchd-based cron that runs every morning at 08:00
local time and:

1. Forces a fresh `recap_cache` rebuild (next `/api/boardroom/recap` regenerates).
2. Calls `boardroom.recap_synthesis()` directly (no http), so it works even
   when the dev web server isn't running.
3. Pipes the synthesis to the Discord channel if `DISCORD_WEBHOOK_URL` is set.
4. Telegram push is wired in code but currently log-only — needs a chat_ids
   table (planned v1.2).

## Files

- `bin/run_recap_cron.sh` — the bash wrapper that does all of the above.
  Lives in the repo.
- `bin/install_recap_cron.sh` — bootstrap helper. Run once after cloning.
  Writes the LaunchAgent plist at `~/Library/LaunchAgents/com.opsdashboard.boardroom.recap.plist`
  (per-user macOS — runs even when no terminal is logged in) and loads it via
  launchctl.

## Install

```bash
bash bin/install_recap_cron.sh install
```

After install, verify:

```bash
bash bin/install_recap_cron.sh status
launchctl list | grep recap
```

You should see `com.opsdashboard.boardroom.recap` listed with status `0`.

## Run manually (debug)

```bash
bash bin/run_recap_cron.sh
# tail -f logs/recap_cron.stdout.log
```

## Uninstall

```bash
bash bin/install_recap_cron.sh uninstall
```

## Logs

- `logs/recap_cron.stdout.log` — print statements (synthesis summary, broadcast results)
- `logs/recap_cron.stderr.log` — anything written to stderr

## Why a Python launchd, not APScheduler in-process

The web server is single-threaded and can be killed/replaced on every code
update. A macOS LaunchAgent is the lightest possible scheduling layer that
survives reboot, doesn't depend on the web server running, and doesn't pull
APScheduler as a dependency.

## What this cron does NOT do (v1.2 candidates)

- Push to Telegram DM (need a `telegram_chats` table tied to `users.id`).
- Cross-project revenue snapshot.
- "CEO blocked > 2h" escalation message.
- Weekly summary digest.
