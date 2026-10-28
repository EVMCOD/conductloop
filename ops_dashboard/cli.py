"""ops-dashboard CLI.

Usage:
    ops index                # index everything (incl. live URL health checks)
    ops index obsidian       # only one source
    ops find QUERY           # search
    ops recent [--source S]  # last modified
    ops status               # DB stats
    ops urls                 # live URL health snapshot
    ops projects             # one-line per project rollup
    ops projects <name>      # drill-down on a project
    ops backup               # take a DB backup
    ops daily [YYYY-MM-DD]    # generate daily note in vault
    ops export               # export state to tarball
    ops import <file>        # import from tarball
"""
from __future__ import annotations

import datetime as dt
import json

# Demo mode is opt-in via env var. When set, `index` and `serve` use the
# fixture-data indexer (`demo.run()`) and skip the real sources. The DB
# path is also overridable so the demo build can use a separate file.
import os as _os
import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import backups, brain, daily, db, embeddings, projects, snapshots
from .indexers import code_todos, live_urls, memory, obsidian, playbook, repos
from .indexers import github as github_indexer
from .indexers import todos as todos_indexer

DEMO_MODE = _os.environ.get("CONDUCTLOOP_DEMO") == "1"
if DEMO_MODE:
    from .indexers import demo as _demo
    # Demo also uses a separate DB file so personal data and demo data
    # never mix in the same SQLite file.
    _demo_db = _os.environ.get("CONDUCTLOOP_DB_PATH")
    if _demo_db:
        from pathlib import Path as _Path

        from . import db as _db_mod
        _db_mod.DB_PATH = _Path(_demo_db)  # type: ignore[attr-defined]

app = typer.Typer(
    no_args_is_help=True,
    help="Personal ops dashboard — index & search across your vault, repos, memory, live URLs, and playbooks.",
)
console = Console()

# Register the `serve` subcommand (defined as default in server.py).
from .server import app as serve_typer

app.add_typer(serve_typer, name="serve")


@app.callback()
def _main() -> None:
    db.init_db()
    live_urls.ensure_schema()


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


@app.command()
def index(
    source: str | None = typer.Argument(
        None,
        help="Limit to one source: obsidian | memory | repo | urls | playbook",
    ),
    urls_only: bool = typer.Option(False, "--urls", help="Shortcut: only check live URLs"),
) -> None:
    """Index all sources (or just one)."""
    db.init_db()
    if DEMO_MODE:
        console.print("[bold magenta]▸ DEMO MODE[/bold magenta] — using fixture data, skipping real sources")
        result = _demo.run()
        console.print(f"[green]\u2713[/green] demo data loaded: {result}")
        return
    if urls_only:
        source = "urls"
    if source is None or source == "obsidian":
        obsidian.run()
    if source is None or source == "memory":
        memory.run()
    if source is None or source == "repo":
        repos.run()
    if source is None or source == "urls":
        live_urls.run()
    if source is None or source == "playbook":
        playbook.run()
    if source is None or source == "backlinks":
        from .indexers import backlinks
        result = backlinks.run()
        types_str = ", ".join(f"{k}={v}" for k, v in result['by_type'].items())
        console.print(f"[green]\u2713[/green] {result['scanned']} docs, {result['links']} links ({types_str})")
    if source is None or source == "todos":
        todos_indexer.run()
    if source is None or source == "code_todos":
        r = code_todos.run()
        console.print(f"[dim]code_todos: {r}[/dim]")
    if source is None or source == "github":
        r = github_indexer.run()
        console.print(f"[dim]github: {r}[/dim]")
    if source is None or source == "embeddings":
        embeddings.run()
    if source is None or source == "skills":
        from .indexers import skills as skills_indexer
        skills_indexer.run()
    if source in ("custom", "plugins"):
        from . import plugins as _plugins_mod
        _plugins_mod.discover()
        r = _plugins_mod.run_all()
        console.print(f"[dim]plugins: {r}[/dim]")
    # After every full index, take a daily snapshot (idempotent: UPSERT by day+kind)
    if source is None:
        from datetime import datetime
        snapshots.take(kind="auto")
        console.print(f"[dim]snapshot saved for {datetime.now().strftime('%Y-%m-%d')}[/dim]")
    if source and source not in {"obsidian", "memory", "repo", "urls", "playbook", "todos", "code_todos", "github", "embeddings", "skills", "custom", "plugins"}:
        raise typer.BadParameter(f"unknown source: {source}")


@app.command()
def find(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(15, "--limit", "-n"),
    source: str | None = typer.Option(None, "--source", "-s"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Search across all indexed documents."""
    rows = db.search(query, limit=limit, source=source)
    if not rows:
        console.print(f"[yellow]no results for:[/yellow] {query}")
        raise typer.Exit()

    if json_out:
        console.print(json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False, default=str))
        return

    for i, r in enumerate(rows, 1):
        meta = {}
        try:
            meta = json.loads(r["metadata"] or "{}")
        except Exception:
            pass
        title = r["title"] or Path(r["path"]).name
        header = f"[bold cyan]{i}.[/bold cyan] [magenta]{r['source']:<8}[/magenta]  {title}"
        console.print(header)
        console.print(f"   [dim]{r['path']}[/dim]")
        if r["excerpt"]:
            console.print(f"   {r['excerpt'].strip()[:300]}")
        if meta.get("wikilinks"):
            wl = ", ".join(f"[[{x}]]" for x in meta["wikilinks"][:5])
            console.print(f"   [blue]links:[/blue] {wl}{' …' if len(meta['wikilinks']) > 5 else ''}")
        console.print()


@app.command()
def recent(
    limit: int = typer.Option(15, "--limit", "-n"),
    source: str | None = typer.Option(None, "--source", "-s"),
) -> None:
    """Most recently modified documents."""
    rows = db.recent(source=source, limit=limit)
    table = Table(title="Recent documents", show_lines=False)
    table.add_column("source", style="magenta")
    table.add_column("title", style="cyan")
    table.add_column("mtime", style="dim")
    table.add_column("path", style="dim", overflow="fold")
    for r in rows:
        ts = _fmt_ts(r["mtime"])
        table.add_row(r["source"], r["title"] or "—", ts, r["path"])
    console.print(table)


@app.command()
def status() -> None:
    """Show DB stats and last index runs."""
    s = db.stats()
    console.print(Panel.fit(
        f"[bold]{s['total']}[/bold] documents indexed",
        title="ops-dashboard",
    ))
    table = Table(title="By source")
    table.add_column("source", style="magenta")
    table.add_column("count", justify="right", style="cyan")
    for r in s["per_source"]:
        table.add_row(r["source"], str(r["c"]))
    console.print(table)

    if s["last_runs"]:
        rt = Table(title="Recent index runs")
        rt.add_column("source")
        rt.add_column("when")
        rt.add_column("indexed", justify="right")
        rt.add_column("skipped", justify="right")
        rt.add_column("errors", justify="right")
        for r in s["last_runs"]:
            rt.add_row(r["source"], _fmt_ts(r["started_at"]), str(r["indexed"]), str(r["skipped"]), str(r["errors"]))
        console.print(rt)

    # live URL snapshot
    snap = live_urls.latest()
    if snap:
        ut = Table(title="Live URLs (latest check)")
        ut.add_column("status")
        ut.add_column("name")
        ut.add_column("http")
        ut.add_column("ms", justify="right")
        ut.add_column("when")
        for r in snap:
            icon = "🟢" if r["ok"] else "🔴"
            sc = str(r["status_code"]) if r["status_code"] else "—"
            ut.add_row(icon, r["name"], sc, str(r["latency_ms"]), _fmt_ts(r["checked_at"]))
        console.print(ut)


@app.command()
def revenue(
    action: str = typer.Argument("summary", help="summary | events | sources | poll | seed-mock"),
    project: str | None = typer.Option(None, "--project", "-p", help="Filter by project key"),
    source: str | None = typer.Option(None, "--source", "-s", help="Source name (for poll)"),
    limit: int = typer.Option(20, "--limit", "-n", help="Limit events"),
) -> None:
    """Revenue / billing dashboard: summary, events, sources, poll, seed-mock."""
    from .indexers import revenue as rev
    if action == "summary":
        s = db.revenue_summary(project_key=project)
        m = db.revenue_margin_by_project()
        console.print(Panel.fit(
            f"[bold]€{s['total_cents']/100:,.2f}[/bold] from {s['count']} events",
            title="Revenue (all time, filtered)",
        ))
        for label, rows in [("by source", s["by_source"]), ("by project", s["by_project"]),
                            ("by type", s["by_type"]), ("by status", s["by_status"])]:
            if not rows: continue
            t = Table(title=label)
            t.add_column("name", style="cyan")
            t.add_column("total", justify="right")
            t.add_column("count", justify="right")
            for r in rows:
                name = r.get("source_type") or r.get("project_key") or r.get("event_type") or r.get("status")
                t.add_row(str(name), f"€{r['total']/100:,.2f}", str(r["cnt"]))
            console.print(t)
        if m:
            t = Table(title="Margin by project (revenue − costs)")
            t.add_column("project", style="cyan")
            t.add_column("revenue", justify="right")
            t.add_column("costs", justify="right")
            t.add_column("margin", justify="right")
            t.add_column("margin %", justify="right")
            for r in m:
                if r["revenue_cents"] == 0 and r["cost_cents"] == 0: continue
                pct = f"{r['margin_pct']:.1f}%" if r.get("margin_pct") is not None else "—"
                color = "green" if r["margin_cents"] >= 0 else "red"
                t.add_row(r["project_key"],
                          f"€{r['revenue_cents']/100:,.2f}",
                          f"€{r['cost_cents']/100:,.2f}",
                          f"[{color}]€{r['margin_cents']/100:,.2f}[/{color}]", pct)
            console.print(t)
    elif action == "events":
        rows = db.list_revenue_events(project_key=project, limit=limit)
        if not rows:
            console.print("[yellow]no events. run `ops revenue seed-mock` or `ops revenue poll`.[/yellow]")
            raise typer.Exit()
        t = Table(title=f"Recent revenue events ({len(rows)})")
        t.add_column("date", style="dim")
        t.add_column("source", style="magenta")
        t.add_column("project", style="cyan")
        t.add_column("type", style="dim")
        t.add_column("amount", justify="right")
        t.add_column("status", style="dim")
        for r in rows:
            d = time.strftime("%Y-%m-%d", time.gmtime(r["occurred_at"]))
            t.add_row(d, r["source_type"], r["project_key"], r["event_type"],
                      f"{r['amount_cents']/100:+.2f}€", r["status"])
        console.print(t)
    elif action == "sources":
        rows = db.list_revenue_sources(project_key=project)
        if not rows:
            console.print("[yellow]no sources. run `ops revenue seed-mock`.[/yellow]")
            raise typer.Exit()
        t = Table(title=f"Revenue sources ({len(rows)})")
        t.add_column("id", justify="right")
        t.add_column("name", style="cyan")
        t.add_column("type", style="magenta")
        t.add_column("project", style="dim")
        t.add_column("enabled", justify="center")
        t.add_column("last poll", style="dim")
        t.add_column("status", style="dim")
        for r in rows:
            t.add_row(str(r["id"]), r["name"], r["source_type"], r["project_key"],
                      "✓" if r["enabled"] else "✗",
                      _fmt_ts(r["last_poll_at"]) if r.get("last_poll_at") else "—",
                      r.get("last_poll_status") or "—")
        console.print(t)
    elif action == "poll":
        result = rev.run(source_name=source)
        console.print(f"[green]✓[/green] {result['sources']} sources, {result['fetched']} fetched, {result['inserted']} inserted, {result['duplicates']} duplicates")
    elif action == "seed-mock":
        # Create one mock source per known project
        from . import projects as proj_mod
        project_keys = [p.key for p in proj_mod.PROJECTS]
        for key in project_keys:
            name = f"mock-{key}"
            if not db.get_revenue_source_by_name(name):
                db.add_revenue_source(name=name, source_type="mock", project_key=key)
                console.print(f"  + created {name}")
        result = rev.run()
        console.print(f"[green]✓[/green] seeded: {result['inserted']} events across {result['sources']} sources")
    else:
        console.print(f"[red]unknown action: {action}[/red]")
        raise typer.Exit(1)


@app.command()
def ga(
    action: str = typer.Argument("summary", help="summary | sites | metrics | poll | seed-mock"),
    project: str | None = typer.Option(None, "--project", "-p", help="Filter by project key"),
    site: int | None = typer.Option(None, "--site", help="GA site id (for poll)"),
    days: int = typer.Option(7, "--days", "-n", help="Days to fetch / show"),
) -> None:
    """Google Analytics 4 dashboard: summary, sites, metrics, poll, seed-mock."""
    from .indexers import ga as ga_idx
    if action == "summary":
        s = db.ga_summary(project_key=project)
        t = s["totals"]
        console.print(Panel.fit(
            f"[bold]{t.get('users', 0):,}[/bold] users · [bold]{t.get('sessions', 0):,}[/bold] sessions · [bold]{t.get('page_views', 0):,}[/bold] page views",
            title="GA4 (all time, filtered)",
        ))
        if s.get("by_site"):
            tt = Table(title="By site")
            tt.add_column("site", style="cyan")
            tt.add_column("project", style="dim")
            tt.add_column("users", justify="right")
            tt.add_column("sessions", justify="right")
            tt.add_column("page views", justify="right")
            for r in s["by_site"][:15]:
                tt.add_row(r["display_name"], r["project_key"] or "—",
                            f"{r['users']:,}", f"{r['sessions']:,}", f"{r['page_views']:,}")
            console.print(tt)
    elif action == "sites":
        rows = db.list_ga_sites(project_key=project)
        if not rows:
            console.print("[yellow]no sites. run `ops ga seed-mock`.[/yellow]")
            raise typer.Exit()
        t = Table(title=f"GA4 sites ({len(rows)})")
        t.add_column("id", justify="right")
        t.add_column("property", style="cyan")
        t.add_column("name", style="dim")
        t.add_column("project", style="dim")
        t.add_column("url", style="dim")
        t.add_column("last poll", style="dim")
        t.add_column("status", style="dim")
        for r in rows:
            t.add_row(str(r["id"]), r["property_id"], r["display_name"], r["project_key"] or "—",
                      r.get("url") or "—",
                      _fmt_ts(r["last_poll_at"]) if r.get("last_poll_at") else "—",
                      r.get("last_poll_status") or "—")
        console.print(t)
    elif action == "metrics":
        rows = db.list_ga_metrics_daily(project_key=project, since=(dt.date.today() - dt.timedelta(days=days)).isoformat() if days else None)
        if not rows:
            console.print("[yellow]no metrics. run `ops ga poll` first.[/yellow]")
            raise typer.Exit()
        t = Table(title=f"Recent metrics (last {days}d, {len(rows)} rows)")
        t.add_column("date", style="dim")
        t.add_column("site", style="cyan")
        t.add_column("project", style="dim")
        t.add_column("users", justify="right")
        t.add_column("sessions", justify="right")
        t.add_column("page views", justify="right")
        t.add_column("eng%", justify="right", style="dim")
        t.add_column("top country", style="dim")
        for r in rows[:30]:
            eng = (r.get("engagement_rate") or 0) * 100
            t.add_row(r["date"], r["property_id"], r["project_key"],
                      f"{r['users']:,}", f"{r['sessions']:,}", f"{r['page_views']:,}",
                      f"{eng:.0f}%", r.get("top_country") or "—")
        console.print(t)
    elif action == "poll":
        result = ga_idx.run(site_id=site, days=days)
        console.print(f"[green]✓[/green] {result['sites']} sites, {result['fetched']} fetched, {result['upserted']} upserted")
    elif action == "seed-mock":
        from . import projects as proj_mod
        for p in proj_mod.PROJECTS:
            prop_id = f"mock-{p.key}"
            existing = next((r for r in db.list_ga_sites() if r["property_id"] == prop_id), None)
            if existing:
                continue
            db.add_ga_site(property_id=prop_id, display_name=p.name, project_key=p.key, url=f"https://{p.key}.example.com")
            console.print(f"  + created {prop_id}")
        result = ga_idx.run(days=days)
        console.print(f"[green]✓[/green] seeded: {result['upserted']} metric-days across {result['sites']} sites")
    else:
        console.print(f"[red]unknown action: {action}[/red]")
        raise typer.Exit(1)


@app.command()
def gsc(
    action: str = typer.Argument("summary", help="summary | sites | poll | test"),
    project: str | None = typer.Option(None, "--project", "-p", help="Filter by project key"),
    site: int | None = typer.Option(None, "--site", help="GSC site id (for poll/test)"),
    days: int = typer.Option(28, "--days", "-n", help="Days to fetch / show"),
) -> None:
    """Google Search Console: summary, sites, poll, test."""
    from .indexers import gsc as gsc_idx
    if action == "summary":
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        s = db.gsc_summary(project_key=project, since=since)
        t = s["totals"]
        console.print(Panel.fit(
            f"[bold]{t.get('clicks', 0):,}[/bold] clicks · [bold]{t.get('impressions', 0):,}[/bold] impressions"
            f" · CTR [bold]{(t.get('ctr') or 0) * 100:.1f}%[/bold] · pos [bold]{t.get('position') or 0:.1f}[/bold]",
            title=f"Search Console (last {days}d)",
        ))
        if s.get("by_site"):
            tt = Table(title="By site")
            tt.add_column("site", style="cyan")
            tt.add_column("project", style="dim")
            tt.add_column("clicks", justify="right")
            tt.add_column("impressions", justify="right")
            tt.add_column("ctr", justify="right")
            tt.add_column("pos", justify="right")
            for r in s["by_site"][:15]:
                tt.add_row(r["display_name"], r["project_key"] or "—",
                           f"{r['clicks']:,}", f"{r['impressions']:,}",
                           f"{(r['ctr'] or 0) * 100:.1f}%", f"{r['position'] or 0:.1f}")
            console.print(tt)
    elif action == "sites":
        rows = db.list_gsc_sites(project_key=project)
        if not rows:
            console.print("[yellow]no GSC sites registered.[/yellow]")
            raise typer.Exit()
        t = Table(title=f"GSC sites ({len(rows)})")
        t.add_column("id", justify="right")
        t.add_column("site_url", style="cyan")
        t.add_column("name", style="dim")
        t.add_column("project", style="dim")
        t.add_column("last poll", style="dim")
        t.add_column("status", style="dim")
        for r in rows:
            t.add_row(str(r["id"]), r["site_url"], r["display_name"], r["project_key"] or "—",
                      _fmt_ts(r["last_poll_at"]) if r.get("last_poll_at") else "—",
                      r.get("last_poll_status") or "—")
        console.print(t)
    elif action == "poll":
        results = gsc_idx.poll_all(days=days, project_key=project, site_id=site)
        fetched = sum(r.get("fetched", 0) for r in results)
        upserted = sum(r.get("upserted", 0) for r in results)
        errors = [r for r in results if not r.get("ok")]
        console.print(f"[green]✓[/green] {len(results)} sites, {fetched} fetched, {upserted} upserted")
        for r in errors:
            console.print(f"  [red]✗ {r['site']}[/red]: {r.get('error')}")
    elif action == "test":
        if site is None:
            console.print("[red]--site <id> required for test[/red]")
            raise typer.Exit(1)
        ok, msg = gsc_idx.test_site(site)
        console.print(f"[{'green' if ok else 'red'}]{'✓' if ok else '✗'}[/] {msg}")
    else:
        console.print(f"[red]unknown action: {action}[/red]")
        raise typer.Exit(1)


@app.command()
def note_ai(
    doc_id: int = typer.Argument(..., help="Note document ID"),
    action: str = typer.Argument(..., help="Action: summarize|expand|translate|key_points|action_items|title_suggest"),
    lang: str = typer.Option("es", "--lang", "-l", help="Target language"),
    apply: bool = typer.Option(False, "--apply", help="Append result to the note (otherwise print)"),
    provider: str | None = typer.Option(None, "--provider", "-p"),
) -> None:
    """Run an AI transformation on a note (summarize, translate, expand, etc)."""
    from .indexers import note_ai as note_ai_mod
    result = note_ai_mod.run_action(doc_id, action, target_lang=lang, provider_name=provider)
    if "error" in result:
        console.print(f"[red]error:[/red] {result['error']}")
        raise typer.Exit(1)
    console.print(f"[bold]\U0001f4dd {result['action']}[/bold] on doc {doc_id} [dim]\"{result['doc_title']}\"[/dim]")
    console.print(f"[dim]model: {result['model']} · took {result['took_ms']}ms · input {result['input_chars']} chars[/dim]")
    console.print()
    console.print(result["result"])
    if apply:
        from . import db as _db
        with _db.db() as conn:
            row = conn.execute("SELECT content FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            console.print(f"[red]doc {doc_id} not found[/red]")
            raise typer.Exit(1)
        new_content = (row["content"] or "") + "\n\n## AI: " + action + " (" + lang + ")\n\n" + result["result"] + "\n"
        ok = _db.update_document_content(doc_id, new_content)
        console.print()
        if ok:
            console.print(f"[green]\u2713[/green] appended to doc {doc_id} (new size: {len(new_content)} chars)")
        else:
            console.print(f"[red]failed to update doc {doc_id}[/red]")


@app.command()
def digest(
    date: str | None = typer.Option(None, "--date", "-d", help="YYYY-MM-DD (default: yesterday)"),
    send: bool = typer.Option(True, "--send/--no-send", help="Send via webhook (Slack/Discord/Telegram)"),
    ai: bool = typer.Option(True, "--ai/--no-ai", help="Also generate AI-written exec summary"),
    ai_only: bool = typer.Option(False, "--ai-only", help="Skip standard digest, only generate AI summary"),
) -> None:
    """Build + send (optional) the daily digest."""
    import datetime as dt

    from .indexers import ai_summary as ai_mod
    from .indexers import digest as digest_mod
    target = None
    if date:
        target = dt.date.fromisoformat(date)
    if ai_only:
        console.print("[cyan]Generating AI daily summary (no standard digest)…[/cyan]")
        result = ai_mod.run(target_date=target)
        console.print(f"[green]✓[/green] AI summary saved: {result.get('path')}")
        console.print(f"  Model: {result.get('model')}")
        console.print()
        console.print(result.get("markdown", ""))
        return
    md = digest_mod.build_markdown(target)
    path = digest_mod.save_digest(target)
    ai_result = None
    if ai:
        try:
            ai_result = ai_mod.run(target_date=target)
        except Exception as e:
            console.print(f"[yellow]AI summary failed: {e}[/yellow]")
    if send:
        send_result = digest_mod.send_digest(md, target_date=target)
        channels = ", ".join(k for k, v in send_result.items() if v.get("ok")) if isinstance(send_result, dict) else str(send_result)
        console.print(f"[green]✓[/green] digest saved: {path}")
        console.print(f"  Sent to: {channels or '(no webhook configured)'}")
    else:
        console.print(f"[green]✓[/green] digest saved (not sent): {path}")
    if ai_result and not ai_result.get("error"):
        console.print(f"[green]✓[/green] AI summary saved: {ai_result.get('path')} (model: {ai_result.get('model')})")
        console.print()
        console.print(ai_result.get("markdown", ""))
    else:
        console.print()
        console.print(md)


@app.command()
def backlinks(
    doc_id: int = typer.Argument(..., help="Document ID to inspect"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Show backlinks (incoming) and outgoing links for a doc."""
    incoming = db.get_backlinks(doc_id, limit=limit)
    outgoing = db.get_outgoing_links(doc_id, limit=limit)
    src = db.recent_documents(limit=20000)
    doc = next((d for d in src if d["id"] == doc_id), None)
    title = doc['title'] if doc else 'unknown'
    console.print(f"[bold]\U0001f517 Doc {doc_id}: {title}[/bold]")
    path_str = doc['path'] if doc else '—'
    console.print(f"  path: {path_str}")
    console.print()
    if incoming:
        t = Table(title=f"Backlinks ({len(incoming)} docs link TO this)")
        t.add_column("from", style="cyan")
        t.add_column("type", style="dim")
        t.add_column("context", style="dim")
        for r in incoming[:limit]:
            t.add_row(r["source_title"] or r["source_path"][:30], r["link_type"], (r.get("context") or "")[:60])
        console.print(t)
    else:
        console.print("[dim]no backlinks yet[/dim]")
    if outgoing:
        t = Table(title=f"Outgoing links ({len(outgoing)} docs this links TO)")
        t.add_column("to", style="cyan")
        t.add_column("type", style="dim")
        for r in outgoing[:limit]:
            t.add_row(r["target_title"] or r["target_path"][:30], r["link_type"])
        console.print(t)


@app.command()
def risk(
    project: str | None = typer.Option(None, "--project", "-p", help="Filter by project key"),
) -> None:
    """Assess AI workflows for common risks (EU AI Act, GDPR, SOC 2, etc)."""
    from .indexers import risk as risk_mod
    result = risk_mod.run_assessment(project_key=project)
    if "error" in result:
        console.print("[red]error:[/red] " + str(result['error']))
        raise typer.Exit(1)
    if result["total"] == 0:
        console.print("[yellow]no AI workflows detected in playbook entries.[/yellow]")
        console.print("Add workflow metadata like 'autonomous: true' to playbook entries to scan them.")
        raise typer.Exit(0)
    console.print("[bold]Risk assessment: " + str(result['total']) + " workflows[/bold]")
    bl = result["by_level"]
    parts = []
    for lvl, n in bl.items():
        if n > 0:
            color = {"high": "red", "medium": "yellow", "low": "green"}.get(lvl, "white")
            parts.append("[" + color + "]" + str(n) + " " + lvl + "[/" + color + "]")
    console.print(" · ".join(parts))
    console.print()
    for w in result["workflows"]:
        color = {"high": "red", "medium": "yellow", "low": "blue"}.get(w["level"], "white")
        console.print("[" + color + "]●[/[" + color + "] [" + w["level"] + ":6] " + w["name"] + " (" + w["project_key"] + "/" + w["phase"] + ") score " + str(w["score"]))
        for f in w["findings"]:
            console.print("   [dim]•[/dim] [bold]" + f["title"] + "[/bold] [" + f["severity"] + "]")
            console.print("     [dim]" + f["guidance"] + "[/dim]")
        console.print()


@app.command()
def urls() -> None:
    """Show latest health snapshot of known live URLs."""
    live_urls.ensure_schema()
    snap = live_urls.latest()
    if not snap:
        console.print("[yellow]no checks yet. run `ops index urls` first.[/yellow]")
        raise typer.Exit()
    table = Table(title="Live URLs")
    table.add_column("")
    table.add_column("name", style="cyan")
    table.add_column("project", style="dim")
    table.add_column("kind", style="dim")
    table.add_column("http", justify="right")
    table.add_column("ms", justify="right")
    table.add_column("when", style="dim")
    for r in snap:
        icon = "🟢" if r["ok"] else "🔴"
        sc = str(r["status_code"]) if r["status_code"] else "—"
        table.add_row(icon, r["name"], r["project"] or "—", r["kind"] or "—", sc, str(r["latency_ms"]), _fmt_ts(r["checked_at"]))
    console.print(table)


@app.command("projects")
def projects_cmd(
    name: str | None = typer.Argument(None, help="Project key or alias. Omit to list all."),
    limit: int = typer.Option(20, "--limit", "-n"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """One-line per project, or drill-down for a single project."""
    if name:
        roll = projects.rollup(name)
        if not roll:
            console.print(f"[red]no project matches[/red] {name}")
            console.print("[dim]Try one of:[/dim]", ", ".join(p.key for p in projects.PROJECTS))
            raise typer.Exit(1)
        if json_out:
            console.print(json.dumps(roll, indent=2, ensure_ascii=False, default=str))
            return
        p = roll["project"]
        console.rule(f"[bold {p['color']}]{p['name']}[/bold {p['color']}]  [dim]({p['key']})[/dim]")
        console.print(f"[dim]aliases:[/dim] {', '.join(p['aliases'])}")

        if roll["urls"]:
            t = Table(title="Live URLs", show_lines=False)
            t.add_column("")
            t.add_column("name")
            t.add_column("http")
            t.add_column("ms", justify="right")
            for u in roll["urls"]:
                icon = "🟢" if u["ok"] else "🔴"
                sc = str(u["status_code"]) if u["status_code"] else "—"
                t.add_row(icon, u["name"], sc, str(u["latency_ms"]))
            console.print(t)

        if roll["repos"]:
            t = Table(title="Repos on disk", show_lines=False)
            t.add_column("name", style="cyan")
            t.add_column("last touched")
            for r in roll["repos"]:
                t.add_row(r["name"], _fmt_ts(r["mtime"]))
            console.print(t)

        if roll["docs"]:
            t = Table(title=f"Mentions in vault/memory (showing {len(roll['docs'])})", show_lines=False)
            t.add_column("source", style="magenta")
            t.add_column("title", style="cyan")
            t.add_column("when")
            for d in roll["docs"]:
                t.add_row(d["source"], d["title"] or "—", _fmt_ts(d["mtime"]))
            console.print(t)
        else:
            console.print("[dim]no documents mention this project yet[/dim]")
        return

    # No name: list all projects
    rows = projects.all_projects_summary()
    if json_out:
        console.print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return
    table = Table(title="Projects — last activity")
    table.add_column("key", style="cyan")
    table.add_column("name")
    table.add_column("urls", justify="center")
    table.add_column("mentions", justify="right")
    table.add_column("repos", justify="right")
    table.add_column("last activity")
    for r in rows:
        url_icon = "🟢" if r["all_urls_ok"] else ("🔴" if r["all_urls_ok"] is False else "—")
        table.add_row(
            r["key"],
            r["name"],
            url_icon,
            str(r["mention_count"]),
            str(len(r["repos"])),
            _fmt_ts(r["last_activity"]),
        )
    console.print(table)


@app.command("playbook")
def playbook_cmd(
    project: str | None = typer.Argument(None, help="Project key. Omit to list all."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show playbook state for one project (or summary for all)."""
    if not project:
        # No arg: list all
        rows = db.all_playbooks_summary()
        if not rows:
            console.print("[yellow]no playbooks yet: add markdown files to ./playbook/ and run `ops index playbook`.[/yellow]")
            raise typer.Exit()
        if json_out:
            console.print(json.dumps(rows, indent=2, ensure_ascii=False))
            return
        table = Table(title="Playbook summary")
        table.add_column("project", style="cyan")
        table.add_column("phase")
        table.add_column("total", justify="right")
        table.add_column("done", justify="right")
        table.add_column("last update")
        for r in rows:
            updated = _fmt_ts(r["phase_updated_at"])
            table.add_row(r["project_key"], r["phase"] or "—", str(r["total"]), str(r["done"]), updated)
        console.print(table)
        return

    proj = projects.find_project(project)
    if not proj:
        console.print(f"[red]no project matches[/red] {project}")
        raise typer.Exit(1)
    data = db.get_playbook(proj.key)
    if not data["entries"]:
        console.print(f"[yellow]no playbook for {proj.key}. file:[/yellow] ./playbook/{proj.key}.md")
        raise typer.Exit()

    if json_out:
        console.print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return

    console.rule(f"[bold {proj.color}]{proj.name}[/bold {proj.color}]  [dim]({proj.key})[/dim]  ·  phase [cyan]{data['phase'] or '—'}[/cyan]")
    by_kind: dict[str, list[dict]] = {}
    for e in data["entries"]:
        if e["entry_kind"] == "phase":
            continue
        by_kind.setdefault(e["entry_kind"], []).append(e)

    KIND_LABELS = {
        "agent_in_use":    "AI agents in use",
        "proposed_agent":  "Proposed agents",
        "risk":            "Risk & compliance",
        "blocker":         "Open blockers",
        "win":             "Wins to date",
        "note":            "Notes",
        "cross_ref":       "Cross-references",
    }
    if not by_kind:
        console.print("[dim]no entries parsed from playbook file[/dim]")
        return
    for kind, label in KIND_LABELS.items():
        if kind not in by_kind:
            continue
        items = by_kind[kind]
        done = sum(1 for e in items if e["done"])
        console.print(f"\n[bold]{label}[/bold]  [dim]({done}/{len(items)})[/dim]")
        for e in items:
            mark = "✅" if e["done"] else "☐ "
            console.print(f"  {mark}  {e['content']}")


@app.command("todos")
def todos_cmd(
    project: str | None = typer.Option(None, "-p", "--project", help="Filter by project key"),
    limit: int = typer.Option(50, "-n", "--limit"),
    all_done: bool = typer.Option(False, "--all", help="Include done items"),
) -> None:
    """List open TODOs / pendientes (cross-source)."""
    rows = db.list_todos(project=project, only_open=not all_done, limit=limit)
    if not rows:
        console.print("[dim]no open todos[/dim]")
        raise typer.Exit()

    table = Table(title=f"Open TODOs ({len(rows)})", show_lines=False)
    table.add_column("project", style="cyan")
    table.add_column("source", style="magenta")
    table.add_column("content", overflow="fold")
    table.add_column("when")
    for r in rows:
        proj = r["project_key"] or "—"
        src = r["source"]
        content = r["content"]
        # Find context: doc path for obsidian/memory/repo
        ctx = ""
        if r["doc_id"]:
            with db.db() as conn:
                drow = conn.execute("SELECT path FROM documents WHERE id = ?", (r["doc_id"],)).fetchone()
                if drow:
                    from pathlib import Path
                    ctx = Path(drow["path"]).name
        cell = content
        if ctx:
            cell = f"{content}  [dim]({ctx})[/dim]"
        table.add_row(proj, src, cell, _fmt_ts(r["mtime"]))
    console.print(table)

    # Summary
    summary = db.todo_summary()
    console.print(f"\n[dim]total open: {summary['open']} · by project:[/dim]")
    for r in summary["by_project"][:5]:
        console.print(f"  [cyan]{r['project_key'] or '—':<22}[/cyan] {r['c']} open")


@app.command("brain")
def brain_cmd(
    question: str | None = typer.Argument(None, help="Question to ask the brain"),
    provider: str | None = typer.Option(None, "-p", "--provider", help="anthropic | openai | ollama | mock"),
    briefing: bool = typer.Option(False, "--briefing", help="Show morning briefing"),
    status: bool = typer.Option(False, "--status", help="Show provider status"),
    conversation: int | None = typer.Option(None, "-c", "--conversation"),
) -> None:
    """Ask the ops brain a question (BYOK RAG over your knowledge)."""
    if status:
        providers = brain.list_providers_status()
        t = Table(title="Brain providers")
        t.add_column("name")
        t.add_column("available", justify="center")
        t.add_column("model")
        for r in providers:
            icon = "🟢" if r["available"] else "🔴"
            t.add_row(r["name"], icon, r["model"] or "—")
        console.print(t)
        console.print("\n[dim]set provider via env: BRAIN_PROVIDER=anthropic OPENAI_API_KEY=sk-...[/dim]")
        console.print("[dim]or store: ops brain config anthropic_api_key sk-...[/dim]")
        return

    if briefing:
        console.rule("[bold]Morning briefing[/bold]")
        text = brain.briefing()
        console.print(text)
        return

    if not question:
        # Show recent conversations
        rows = db.list_conversations(limit=20)
        if not rows:
            console.print("[dim]no conversations yet[/dim]")
        else:
            t = Table(title="Recent conversations")
            t.add_column("id", justify="right")
            t.add_column("title")
            t.add_column("updated")
            for r in rows:
                t.add_row(str(r["id"]), r["title"] or "—", _fmt_ts(r["updated_at"]))
            console.print(t)
            console.print("\n[dim]usage: ops brain 'your question here'[/dim]")
            console.print("[dim]        ops brain --briefing[/dim]")
        return

    conv_id = conversation
    if not conv_id:
        conv_id = db.create_conversation(title=question[:80])
    # Optional :project prefix
    prefix, q_clean = db.parse_project_prefix(question)
    if prefix:
        p = projects.find_project(prefix)
        if p:
            ans = brain.ask(q_clean, conversation_id=conv_id, provider_name=provider)
        else:
            console.print(f"[yellow]unknown project '{prefix}', using auto-detect[/yellow]")
            ans = brain.ask(question, conversation_id=conv_id, provider_name=provider)
    else:
        ans = brain.ask(question, conversation_id=conv_id, provider_name=provider)
    db.add_message(conv_id, "user", question)
    db.add_message(conv_id, "assistant", ans.answer,
                   citations=ans.citations, provider=ans.provider, model=ans.model)
    console.rule(f"[bold]brain · {ans.provider} · {ans.model} · {ans.duration_ms}ms[/bold]")
    if ans.project_key:
        console.print(f"[dim]detected project: {ans.project_key}[/dim]")
    console.print()
    console.print(ans.answer)
    if ans.citations:
        console.print("\n[bold]Citations:[/bold]")
        for c in ans.citations:
            console.print(f"  [dim][{c['n']}] {c['source']}: {c['title']}  (sim {c['score']:.3f})[/dim]")
            console.print(f"      [dim]{c['path']}[/dim]")
    # Auto-export to markdown
    try:
        from . import app_paths
        export_path = app_paths.path("conversations") / f"{conv_id}.md"
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(db.export_conversation_markdown(conv_id), encoding="utf-8")
        console.print(f"\n[dim]saved → {export_path}[/dim]")
    except Exception:
        pass


@app.command("brain-config")
def brain_config_cmd(
    key: str = typer.Argument(..., help="Config key (anthropic_api_key, openai_api_key, ollama_model, ...)"),
    value: str = typer.Argument(..., help="Value to store"),
) -> None:
    """Store a brain config value locally."""
    brain.set_config(key, value)
    console.print(f"[green]✓[/green] saved {key} (not echoed)")


@app.command("find-semantic")
def find_semantic_cmd(
    query: str = typer.Argument(..., help="Search query (semantic)"),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Search by meaning (local sentence-transformers embeddings)."""
    rows = embeddings.search(query, limit=limit)
    if not rows:
        console.print(f"[yellow]no semantic matches for:[/yellow] {query}")
        raise typer.Exit()
    for i, r in enumerate(rows, 1):
        title = r.get("title") or Path(r["path"]).name
        score = r.get("score", 0.0)
        console.print(
            f"[bold cyan]{i}.[/bold cyan] [magenta]{r['source']:<8}[/magenta]  {title}  [dim](sim {score:.3f})[/dim]"
        )
        console.print(f"   [dim]{r['path']}[/dim]")


@app.command("plugins")
def plugins_cmd(action: str = typer.Argument("list", help="list | run")) -> None:
    """List or run user plugins from ~/.ops-dashboard/plugins/."""
    from . import plugins
    if action == "list":
        d = plugins.discover()
        if not d: console.print("[dim]no plugins. drop a .py file in ~/.ops-dashboard/plugins/[/dim]"); return
        for name, _ in d.items(): console.print(f"  {name}")
    elif action == "run":
        r = plugins.run_all()
        console.print(f"[green]\u2713[/green] ran {r['plugins']} plugins ({r['indexed']} indexed, {r['errors']} errors)")
    else: raise typer.BadParameter(f"unknown action: {action}")

@app.command("daily")
def daily_cmd(day: str | None = typer.Argument(None, help="YYYY-MM-DD (default: today)")) -> None:
    """Generate a daily note in the vault capturing today's activity."""
    p = daily.write_daily(day=day)
    console.print(f"[green]\u2713[/green] daily note saved: {p}")

@app.command("backup")
def backup_cmd(reason: str = typer.Option("manual", "--reason", help="Tag for the backup")) -> None:
    """Take a consistent backup of the DB to the backups folder (see backups.backup_dir)."""
    p = backups.backup_now(reason=reason)
    console.print(f"[green]\u2713[/green] backup saved: {p}")

@app.command("restore")
def restore_cmd(name: str = typer.Argument(..., help="File name of the backup (see `backups`)"),
                yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation")) -> None:
    """Put a backup back as the live DB. The current DB is backed up first ("pre-restore")."""
    if not yes and not typer.confirm(f"Replace the current database with {name}? (it is backed up first)"):
        raise typer.Abort()
    try:
        r = backups.restore(name)
    except backups.BackupError as e:
        raise typer.BadParameter(str(e)) from e
    console.print(f"[green]\u2713[/green] restored {r['restored']}; the previous state is in {r['safety_backup']}")

@app.command("export")
def export_cmd(out: Path = typer.Option(Path.home() / "ops-dashboard-export.tar.gz", "--out", help="Output tarball path")) -> None:
    """Export the entire ops-dashboard state (DB + snapshots + config) to a tarball."""
    import tarfile
    out.parent.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve().parent.parent
    with tarfile.open(out, "w:gz") as tf:
        tf.add(src / "data" / "ops.db", arcname="data/ops.db")
        snaps = src / "snapshots"
        if snaps.exists():
            tf.add(snaps, arcname="snapshots")
        cfg = src.parent / "config.json"
        if cfg.exists():
            tf.add(cfg, arcname="config.json")
        playbook_dir = src / "playbook"
        if playbook_dir.exists():
            tf.add(playbook_dir, arcname="playbook")
    console.print(f"[green]\u2713[/green] exported to {out}  ({out.stat().st_size // 1024} KB)")

@app.command("import")
def import_cmd(src: Path = typer.Argument(..., help="Tarball to import"), force: bool = typer.Option(False, "--force", help="Overwrite existing DB")) -> None:
    """Restore ops-dashboard from a tarball. DESTRUCTIVE: overwrites the current DB."""
    import tarfile
    if not src.exists():
        raise typer.BadParameter(f"file not found: {src}")
    if not force:
        if not typer.confirm(f"This will REPLACE your current DB with {src}. Continue?"):
            raise typer.Abort()
    target = Path(__file__).resolve().parent.parent
    with tarfile.open(src, "r:gz") as tf:
        # filter="data" (PEP 706, Python 3.12+) blocks path traversal / absolute
        # paths / device files \u2014 never trust member paths from an arbitrary tarball.
        tf.extractall(target, filter="data")
    console.print(f"[green]\u2713[/green] imported from {src}")

@app.command("backups")
def backups_list_cmd() -> None:
    """List recent DB backups."""
    rows = backups.list_backups()
    if not rows:
        console.print("[dim]no backups yet[/dim]")
        return
    t = Table(title="Recent backups")
    t.add_column("name")
    t.add_column("size")
    t.add_column("modified")
    for r in rows:
        from datetime import datetime as _dt
        ts = _dt.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
        t.add_row(r["name"], f"{r['size_kb']} KB", ts)
    console.print(t)

@app.command("snapshot")
def snapshot_cmd(
    action: str = typer.Argument("diff", help="take | diff | weekly"),
    day: str | None = typer.Option(None, "--day", help="YYYY-MM-DD"),
    write_file: bool = typer.Option(False, "--write", help="Write markdown to the snapshots folder"),
) -> None:
    """Take a snapshot, show diff vs previous, or render a weekly summary."""
    if action == "take":
        d = snapshots.take(kind="auto")
        console.print(f"[green]\u2713[/green] snapshot saved for {d['day']}")
    elif action == "diff":
        d = snapshots.diff(day=day, kind="auto")
        if write_file:
            p = snapshots.write_snapshot_file(d)
            console.print(f"[green]\u2713[/green] wrote {p}")
        else:
            console.print(snapshots.render_markdown(d))
    elif action == "weekly":
        if write_file:
            p = snapshots.write_weekly_file(day)
            console.print(f"[green]\u2713[/green] wrote {p}")
        else:
            console.print(snapshots.render_weekly())


@app.command("templates")
def templates_cmd(
    action: str = typer.Argument("list", help="list | init | instantiate"),
    key: str | None = typer.Option(None, "--key", help="Template key (e.g. launch_checklist)"),
    project: str | None = typer.Option(None, "--project", help="Project key to attach todos to"),
) -> None:
    """List, init, or instantiate task templates."""
    if action == "list":
        for t in db.list_templates():
            console.print(f"[{t['icon']}] [bold]{t['name']}[/bold] ({t['key']})  [dim]{t['description']}[/dim]")
            for i, item in enumerate(t["items"][:6], 1):
                console.print(f"    {i}. {item['title']}")
            if len(t["items"]) > 6:
                console.print(f"    [dim]... and {len(t['items']) - 6} more[/dim]")
    elif action == "init":
        db.init_default_templates()
        console.print("[green]\u2713[/green] default templates loaded")
    elif action == "instantiate":
        if not key:
            raise typer.BadParameter("--key required for instantiate")
        ids = db.instantiate_template(key, project_key=project)
        console.print(f"[green]\u2713[/green] instantiated {len(ids)} todos from '{key}' (project={project or 'none'})")


@app.command("decision")
def decision_cmd(
    ctx: typer.Context,
    title: str | None = typer.Argument(None, help="Decision title (omit to use --list)"),
    decision: str | None = typer.Argument(None, help="What was decided"),
    project: str | None = typer.Option(None, "--project", help="Project key"),
    context: str | None = typer.Option(None, "--context", help="Why we were deciding"),
    consequences: str | None = typer.Option(None, "--consequences", help="Expected outcomes/risks"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    list: bool = typer.Option(False, "--list", "-l", help="List recent decisions"),
    search: str | None = typer.Option(None, "--search", help="Search by keyword"),
) -> None:
    """Log a decision (or list recent ones)."""
    if list or search is not None or title is None:
        rows = db.list_decisions(project=project, search=search, limit=20)
        if not rows:
            console.print("[dim]no decisions yet — use `ops decision 'title' 'decision'` to log one[/dim]")
            return
        for d in rows:
            console.print(f"[bold]{d['title']}[/bold] [dim]({d['project_key'] or 'general'})[/dim]")
            console.print(f"  [green]\u2192[/green] {d['decision']}")
            if d.get('context'):
                console.print(f"  [dim]why:[/dim] {d['context']}")
            console.print()
        return
    if not title or not decision:
        raise typer.BadParameter("provide title and decision, or use --list/--search")
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    did = db.create_decision(title=title, decision=decision, project_key=project,
                             context=context, consequences=consequences, tags=tag_list)
    console.print(f"[green]\u2713[/green] decision #{did} logged: {title}")


@app.command("cost")
def cost_cmd(
    action: str = typer.Argument("list", help="list | add | summary"),
    provider: str | None = typer.Option(None, "--provider", help="Provider name (e.g. cloudflare)"),
    amount: float | None = typer.Option(None, "--amount", help="Amount in major units (e.g. 5.0 for €5)"),
    currency: str = typer.Option("EUR", "--currency", help="EUR / USD"),
    project: str | None = typer.Option(None, "--project", help="Project key"),
    category: str = typer.Option("infra", "--category", help="infra | api | domain | dev_tool | subscription | one_time"),
    description: str | None = typer.Option(None, "--description", help="What's this charge for"),
    period: str | None = typer.Option(None, "--period", help="YYYY-MM (defaults to current month)"),
) -> None:
    """Track infrastructure/API/dev costs across providers."""
    if action == "add":
        if not provider or amount is None:
            raise typer.BadParameter("--provider and --amount required for add")
        cents = int(round(amount * 100))
        cid = db.add_cost(provider=provider, amount_cents=cents, currency=currency,
                          project_key=project, category=category, description=description, period=period)
        console.print(f"[green]\u2713[/green] cost #{cid}: {amount} {currency} {provider} ({category})")
    elif action == "list":
        rows = db.list_costs(project=project, provider=provider, limit=40)
        if not rows:
            console.print("[dim]no costs logged yet — use `ops cost add --provider cloudflare --amount 5.0`[/dim]")
            return
        table = Table(title="Costs (last 40)")
        table.add_column("period", style="cyan")
        table.add_column("provider", style="magenta")
        table.add_column("amount", justify="right")
        table.add_column("cat")
        table.add_column("project")
        table.add_column("description")
        for r in rows:
            amt = f"{r['amount_cents']/100:.2f} {r['currency']}"
            table.add_row(r["period"], r["provider"], amt, r["category"], r.get("project_key") or "—", (r.get("description") or "")[:40])
        console.print(table)
    elif action == "summary":
        s = db.cost_summary(period=period)
        if not s["by_provider"]:
            console.print("[dim]no costs yet[/dim]")
            return
        table = Table(title=f"Cost summary{' — ' + period if period else ''}")
        table.add_column("provider", style="magenta")
        table.add_column("category")
        table.add_column("count", justify="right")
        table.add_column("total", justify="right")
        for r in s["by_provider"]:
            table.add_row(r["provider"], r["category"], str(r["c"]), f"{r['total_cents']/100:.2f} {r['currency']}")
        console.print(table)
    else:
        raise typer.BadParameter(f"unknown action: {action}")


@app.command("mcp")
def mcp_cmd(
    print_config: bool = typer.Option(False, "--print-config", help="Print the MCP client config snippet and exit"),
) -> None:
    """Run the MCP server over stdio — exposes the brain to AI agents (Claude, etc.).

    Add it to your MCP client with:  ops mcp --print-config
    """
    from . import mcp_server
    if print_config:
        import json as _json
        console.print_json(_json.dumps(mcp_server.client_config()))
        console.print("\n[dim]Paste into ~/Library/Application Support/Claude/claude_desktop_config.json (Claude Desktop), then restart it.[/dim]")
        return
    mcp_server.serve_stdio()


@app.command("sync-export")
def sync_export_cmd(
    out: Path = typer.Option(Path.home() / "conductloop.cbundle", "--out", help="Bundle output path"),
    passphrase: str = typer.Option(..., "--passphrase", prompt=True, hide_input=True, help="Encryption passphrase"),
) -> None:
    """Export brain data to an end-to-end encrypted bundle (for another device)."""
    from . import sync_bundle
    r = sync_bundle.export_bundle(passphrase, out)
    console.print(f"[green]✓[/green] encrypted bundle → {r['path']} ({r['bytes']} bytes)")


@app.command("sync-import")
def sync_import_cmd(
    path: Path = typer.Argument(..., help="Encrypted .cbundle to import"),
    passphrase: str = typer.Option(..., "--passphrase", prompt=True, hide_input=True, help="Encryption passphrase"),
) -> None:
    """Import brain data from an encrypted bundle (same passphrase)."""
    from . import sync_bundle
    r = sync_bundle.import_bundle(path, passphrase)
    console.print(f"[green]✓[/green] imported {r['memory']} memory + {r['decisions']} decisions")


@app.command("briefing")
def briefing_cmd(
    channels: str = typer.Option("", "--channels", help="Comma list: telegram,discord"),
) -> None:
    """Compose the morning briefing and write it to today's Obsidian daily note."""
    from . import briefing
    chans = [c.strip() for c in channels.split(",") if c.strip()]
    out = briefing.deliver(channels=chans)
    console.print(f"[green]✓[/green] briefing → {out['daily_note']['note']}")
    for ch in chans:
        console.print(f"  {ch}: {out.get(ch)}")


@app.command("calendar-import")
def calendar_import_cmd(source: str = typer.Argument(..., help="Path or URL to an .ics feed")) -> None:
    """Import calendar events from an ICS file/URL (for the briefing agenda)."""
    from . import calendar_sync
    r = calendar_sync.import_ics(source)
    console.print(f"[green]✓[/green] imported {r['imported']} events from {source}")


@app.command("capture")
def capture_cmd(
    content: str = typer.Argument(..., help="What to capture (note/url/idea/task text)"),
    kind: str = typer.Option("note", "--kind", help="note | url | idea | task"),
) -> None:
    """Drop something into the quick-capture inbox to triage later."""
    from . import capture
    cid = capture.add(content, kind=kind)
    console.print(f"[green]✓[/green] captured #{cid} ({kind})")


@app.command("setup")
def setup_cmd() -> None:
    """Show the 10-point setup checklist (what to configure for a healthy install)."""
    from .onboarding import checklist
    r = checklist()
    state = "[green]ready[/green]" if r["ready"] else "[yellow]incomplete[/yellow]"
    console.print(f"[bold]Setup: {r['passed']}/{r['total']} ready[/bold] ({state})")
    for c in r["checks"]:
        mark = "[green]✓[/green]" if c["ok"] else "[red]✗[/red]"
        console.print(f"  {mark} {c['step']:>2}. {c['label']} — [dim]{c['detail']}[/dim]")


@app.command("obsidian-sync")
def obsidian_sync_cmd(
    import_: bool = typer.Option(False, "--import", help="Import memory FROM the vault instead of exporting to it"),
) -> None:
    """Sync Brain memory with the Obsidian vault (export by default)."""
    from . import obsidian_sync
    if import_:
        r = obsidian_sync.import_memory()
        console.print(f"[green]✓[/green] imported {r['imported']} memory notes from {r['dir']}")
    else:
        r = obsidian_sync.export_all()
        console.print(f"[green]✓[/green] exported {r['memory']['written']} memory + "
                      f"{r['decisions']['written']} decision notes to the vault")


def main() -> None:
    """Console-script entry point (see pyproject [project.scripts])."""
    app()


if __name__ == "__main__":
    app()
