# Remote access (relay v1 — Tailscale)

By default the phone reaches your computer over the **local network** (same
Wi‑Fi). To use ConductLoop on your phone from **anywhere** — without exposing
ports or routing your data through any cloud — use Tailscale.

## How it works
- Tailscale puts your computer and phone on a private encrypted network (a
  "tailnet") with stable `100.x.y.z` addresses.
- The desktop pairing QR already advertises this machine's **Tailscale IP** in
  addition to its LAN IP (`scripts`: `_tailscale_ips()` in `server.py`).
- The iOS app stores all candidate hosts and, on launch, auto-picks the first
  reachable one: **LAN at home, Tailscale when you're away** — no re‑pairing.

## Setup (one time)
1. Install Tailscale on this computer: <https://tailscale.com/download> and sign in.
2. Install Tailscale on your iPhone (App Store) and sign in with the **same account**.
3. Start ConductLoop with mobile access:
   ```bash
   OPS_ALLOW_MOBILE=1 make app
   ```
4. Open <http://127.0.0.1:7777/pair>, generate the QR, scan it from the app.

That's it. The QR now contains both your LAN and Tailscale addresses; the app
falls back to Tailscale automatically when off your home network.

## Privacy
- Traffic is end‑to‑end encrypted by Tailscale (WireGuard). It does **not** pass
  through ConductLoop servers — there are none.
- Your data never leaves your computer except to your own paired devices.

## Notes
- If `_tailscale_ips()` returns nothing, Tailscale isn't installed/running, or
  the CLI isn't on PATH (the app also tries `/Applications/Tailscale.app/...`).
- A fully managed relay (no Tailscale install on the phone) is a future option;
  Tailscale is the zero‑infra v1.
