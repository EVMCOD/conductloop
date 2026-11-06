# Chat channels

Talk to your agents from a chat app, and get their answers back. Telegram is
the first channel; Discord comes next.

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
   sent to every paired chat — once. Only runs that finish after the channel
   was turned on; failed runs and approval requests are never sent.

| Route | What it does |
|---|---|
| `GET /channels` | the page |
| `GET /api/channels` | status: on/off, where the token is, bot name, default agent, paired chats, last error; the agents |
| `POST /api/channels/telegram/enable` / `disable` | on / off (off also closes an open code) |
| `POST /api/channels/telegram/pairing` | a new one-time code |
| `POST /api/channels/telegram/default-agent` | the default agent (`null` for none) |
| `DELETE /api/channels/telegram/peers/<id>` | unpair a chat |

### How it behaves

- **Off by default.** While off, no call is made to Telegram.
- **Local only.** Messages are fetched by long polling (`getUpdates`) from
  this machine. No webhook, no open port, nothing to expose.
- **Chats you paired, nobody else.** An unpaired chat gets one fixed
  sentence (at most every 10 minutes) and never reaches an agent, so it
  costs nothing. Telegram channels are ignored.
- **Groups.** A group is paired with the same one-time code from the app.
  In a group the bot answers only commands and messages that mention it
  (`@yourbot …`); anything else in the group is ignored. Everyone in a
  paired group can talk to your agents: pair only groups you trust.
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
was removed on 2026-11-06, with its daily-recap broadcast and the Telegram
option of the Boardroom CEO channels. If its webhook is still set on your bot,
the status says so: remove it (`deleteWebhook`) and the channel starts reading.
