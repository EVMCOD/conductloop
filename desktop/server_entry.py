"""PyInstaller entry point for the bundled ConductLoop server.

Starts the local dashboard the same way `ops_dashboard serve` does, but without
Typer (simpler to freeze). The packaged desktop app runs this binary as a Tauri
sidecar and points its window at http://127.0.0.1:<port>/.

ConductLoop is free (since v1.1.0): there is no license gate, so the sidecar no
longer sets OPS_LICENSE_ENFORCE (licensing.enforce() ignores it anyway).
"""
import os
import sys


def main() -> None:
    from ops_dashboard import db, media, server
    from ops_dashboard.indexers import live_urls

    # The app inherits a soft limit of 256 open files from launchd, and the
    # one-file bundle keeps its extracted files open: spawning a subprocess
    # then failed with "Too many open files" (2026-10-23).
    media._ensure_fd_headroom()

    host = os.environ.get("OPS_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "7777"))

    db.init_db()
    live_urls.ensure_schema()

    scheduler = None
    if os.environ.get("CONDUCTLOOP_DEMO") != "1":
        try:
            from ops_dashboard import scheduler as sched_mod
            scheduler = sched_mod.Scheduler(interval=30.0)
            scheduler.start()
        except Exception as e:  # scheduler is best-effort; app still serves
            print(f"[server_entry] scheduler not started: {e}", file=sys.stderr)

    # Same guard as `ops_dashboard serve`: if the port is taken, say so
    # instead of dying with a bare OSError. A local process that grabs the
    # port first would otherwise receive the app's boot nonce (2026-10-23).
    try:
        httpd = server._bind_server(host, port)
    except SystemExit as e:
        print(f"[server_entry] {e}", file=sys.stderr)
        raise
    parent_gone = False
    parent = os.environ.get("CONDUCTLOOP_PARENT_PID", "")
    if parent.isdigit():
        # Exit with the desktop app, however it ends (Cmd+Q, crash, force quit).
        from ops_dashboard import parent_watch

        def _stop() -> None:
            nonlocal parent_gone
            parent_gone = True
            httpd.shutdown()
        parent_watch.watch(int(parent), _stop)
    print(f"ConductLoop serving on http://{host}:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        if scheduler:
            try:
                scheduler.stop()
            except Exception:
                pass
    if parent_gone:
        os._exit(0)   # don't wait for agent-session threads: the app is gone


if __name__ == "__main__":
    main()
