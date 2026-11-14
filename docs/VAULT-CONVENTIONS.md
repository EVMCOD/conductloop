# Vault conventions

The vault features (handoffs, open work, the daily note, the index
sections) read your notes through a few conventions. They are configuration:
the `vault_conventions` object of `config.json` (`$OPS_CONFIG_PATH`). Every
key is optional; a value of the wrong type is ignored and the default is used,
so a typo never stops indexing. The file is re-read when it changes.

```json
{
  "vault_conventions": {
    "language": "en",
    "handoff_types": ["handoff"],
    "handoff_folders": ["handoff", "handoffs"],
    "ask_headings": [],
    "owners": [],
    "blocking_headings": [],
    "daily_dir": "",
    "unplaced_section": "📥 Sin clasificar",
    "orphan_section": "📚 Documentación",
    "ideas_section": "💡 Ideas",
    "counts_template": ""
  }
}
```

| Key | Default | What it does |
| --- | --- | --- |
| `language` | `"en"` | Language of the text ConductLoop *generates* in the daily note (`"en"` or `"es"`). Also `$OPS_VAULT_LANGUAGE`. |
| `handoff_types` | `["handoff"]` | Frontmatter `type:` values that mark a note as a handoff. |
| `handoff_folders` | `["handoff", "handoffs"]` | Folder names that also mark a note as a handoff. |
| `ask_headings` | `[]` | Headings whose first paragraph is the *ask* of a handoff (e.g. `["Ask", "Brief"]`). Without a match, the first blockquote is the ask. |
| `owners` | `[]` | Names an open item can end with (`- [ ] do X — Ana`). Added to `$OPS_OWNER_NAME` and to Claude, Codex, Cursor, ChatGPT. |
| `blocking_headings` | `[]` | Extra regular expressions (case-insensitive) for headings whose open items are *blocking*. The built-in English/Spanish pattern (blocking, blocker, bloquea…) stays. |
| `daily_dir` | `""` | Folder of the daily notes. Also `$OPS_DAILY_DIR`. |
| `unplaced_section`, `orphan_section`, `ideas_section` | Spanish titles (see below) | Titles of the index sections ConductLoop writes into your own index. |
| `counts_template` | `""` | The counts line of an index, with `{total}`, `{active}` and `{ideas}`, e.g. `"{total} projects ({active} active + {ideas} ideas)"`. Empty = the original Spanish line. |

## What is neutral and what is not

* **Daily note text** is English by default. It sits between
  `<!-- conductloop:daily -->` markers and is regenerated, so the change does
  not touch what you wrote. To keep the old Spanish headings:
  `{"vault_conventions": {"language": "es"}}`.
* **Index section titles and the counts line** keep their Spanish defaults on
  purpose. They are written into *your* index and found again by their title:
  changing the default would add a second section next to the one your
  index already has. For a new vault, set them (and `counts_template`) in the
  config once, before the first run.
* **Owners**: no personal name is built in. The desktop app has no `.env`, so
  put your own name in `owners`.
* **Daily folder**: if `~/Documents/Obsidian/OC/daily` already exists it is
  still used, so an upgrade never moves your daily notes. Otherwise the folder
  is `<vault root>/daily`. Set `daily_dir` to be explicit.

## Example (Spanish vault)

```json
{
  "vault_conventions": {
    "language": "es",
    "handoff_types": ["handoff", "relevo"],
    "ask_headings": ["Encargo"],
    "owners": ["Ana"],
    "blocking_headings": ["frenad"]
  }
}
```
