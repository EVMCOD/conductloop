# Chat channels

Talk to your agents from a chat app, and get their answers back. Telegram is
the paired channel (Channels page). Discord still uses the older Boardroom
webhook bridge; free-text there goes to the Holding CEO you chose.

## Telegram

1. In Telegram, talk to **@BotFather**, `/newbot`, and copy the token.
2. In ConductLoop, **Credentials → Channels → Telegram bot token**: paste it.
   It is kept in the keychain (or the encrypted vault when there is no
   keychain). It is never written to the database, `config.json` or a log,
   and no API answer contains it.
3. Open **Channels** (`/channels`, also linked from Setup) and turn it on
   (or `POST /api/channels/telegram/enable`). The token is
   checked with Telegram (`getMe`) first; a rejected token leaves it off.
4. Create a pairing code on that page (or `POST /api/channels/telegram/pairing`).
   Send `/pair <code>` to your bot within 10 minutes, from a private chat
   or from a group the bot is in. The code works once, closes
   after 5 wrong guesses, and only its hash is stored.
5. In the chat, just write: the **Holding CEO** answers by default (the
   member set as Holding CEO, or the Boardroom's Holding CEO reply).
   `/agents` and `/use <id>` pick another agent; `/use ceo` goes back. The
   page (or `POST /api/channels/telegram/default-agent {"agent_id": n}`)
   changes the default for every chat. Long answers are split; very long
   ones are cut.
6. **Run results**: when a task run finishes successfully, its result is
   sent once — to each paired private chat, and privately (DM) to each
   person on a paired group's list. Only runs that finish after the channel
   was turned on; failed runs and approval requests are never sent. URL
   alerts and the daily digest never go to Telegram (Slack/Discord webhooks
   only).

| Route | What it does |
|---|---|
| `GET /channels` | the page |
| `GET /api/channels` | status: on/off, where the token is, bot name, default agent, paired chats (a group with its list), last error; the agents |
| `POST /api/channels/telegram/enable` / `disable` | on / off (off also closes an open code) |
| `POST /api/channels/telegram/pairing` | a new one-time code |
| `POST /api/channels/telegram/default-agent` | the default agent (`null` for none) |
| `DELETE /api/channels/telegram/peers/<id>` | unpair a chat |
| `POST /api/channels/telegram/peers/<id>/members` | `{"user_id": "123"}`: put someone on a group's list |
| `DELETE /api/channels/telegram/peers/<id>/members/<user_id>` | take them off |


### How it behaves

- **Off by default.** While off, no call is made to Telegram.
- **Local only.** Messages are fetched by long polling (`getUpdates`) from
  this machine. No webhook, no open port, nothing to expose.
- **Chats you paired, nobody else.** An unpaired chat gets one fixed
  sentence (at most every 10 minutes) and never reaches an agent, so it
  costs nothing. Telegram channels are ignored.
- **Groups.** A group is paired with the same one-time code from the app.
  In a group the bot answers only commands and messages that mention it
  (`@yourbot …`), and only from the people on that group's list. Someone not
  on the list who addresses the bot gets a short fixed notice (no agent, no
  provider call), once ever for that person in that group (`denied_notice_at`).
- **A group's list.** Whoever sent the pairing code in the group is put on
  it. Add or remove others on the Channels page by Telegram user id. The
  easy way to find one: they mention the bot in the group; they get the
  notice above, and show up on the page under the group with an **Allow**
  button (the 20 newest are kept; only people who wrote *to the bot*, never
  the rest of the group). A list with nobody on it means nobody can talk to
  the agents there. Bots and anonymous admins are always ignored. Re-pairing
  the same group keeps its list; only **Unpair** deletes it.
- **Run results to groups are private.** Each allowed member receives the
  result as a DM. Telegram only lets a bot message a user who has opened a
  chat with it: if a DM fails with 403 (cannot initiate / blocked /
  deactivated), that member is marked *unreachable* on the Channels page
  (ask them to send `/start` to the bot) and is not retried until they write
  to the bot privately. The group chat itself never gets a result.
- **One answer per message.** The update offset is saved before the message
  is handled, so a crash or a restart never answers (and pays for) the same
  message twice.
- **The agent wake is the usual one** (`scheduler._wake_agent`): it has a
  deadline (`OPS_AGENT_WAKE_TIMEOUT`) and is logged in the agent's log.
- **Errors are explained.** A rejected token or a webhook still set on the
  bot (Telegram refuses polling then) shows in `last_error` and the poller
  waits a minute before trying again.
- `OPS_DISABLE_CHANNELS=1` keeps every channel stopped.

The older Boardroom bridge (`telegram_bridge.py`, webhook, `/api/telegram/*`)
was removed on 2026-11-06, with its daily-recap broadcast, the Telegram
option of the Boardroom CEO channels and its `telegram_chats` table
(migration 32). The Telegram notifications set with `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID` were removed too: alerts and digests go to Slack/Discord
webhooks only. If its webhook is still set on your bot,
the status says so: remove it (`deleteWebhook`) and the channel starts reading.

## Discord (Channels page)

Same design as Telegram above: bot token in Credentials → Channels
(`channel.discord.bot_token`), off by default, pairing codes from the app,
DMs plus server text channels (answers only mentions/commands, per-member
allowlist on `/channels`), run results only to allowed members privately,
unreachable flag, once-ever not-on-list notice with no token spend, default
agent = Holding CEO with `/use` to change.

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application → Bot → copy the token. Enable Message Content Intent
   (required so the bot can read message bodies).
2. In ConductLoop, **Credentials → Channels → Discord bot token**: paste it.
3. On **Channels**, turn Discord on (the token is checked with Discord
   `GET /users/@me` first).
4. Create a pairing code and send `/pair <code>` to the bot in a DM or a
   server text channel.

**Live inbound (step 2).** While the channel is on, ConductLoop opens a
Discord Gateway WebSocket from this machine (`status.inbound` is `gateway`
when connected, `connecting` while it retries, `off` when the channel is
off). No public webhook and no open port. Enable the *Message Content
Intent* on the bot (Developer Portal → Bot). Tests inject Gateway payloads
or a fake socket; they never call Discord.

### Discord (Boardroom webhook) — separate

The Boardroom Discord bridge (`ops_dashboard/discord_bridge.py`) is **not**
this channel: one outbound webhook URL (`DISCORD_WEBHOOK_URL`), optional
inbound POSTs to `/api/discord/webhook`. Free-text there still goes to the
chosen Holding CEO (same rule as Telegram). Slash commands (`/inbox`,
`/recap`, `/vault`, `/portfolio`) are unchanged. Keep using it for Boardroom
broadcasts; use the Channels page Discord bot for paired chats and run results.
