# Skills

ConductLoop uses the [agentskills.io](https://agentskills.io/specification)
format: a folder with a `SKILL.md` file (YAML frontmatter, then a Markdown
body) and optional helper files (`scripts/`, `references/`, `assets/`, …).
The existing library already lives this way. Skills you own are stored in
`<vault>/skills/<name>/` (or `OPS_SKILLS_DIR` if set). Agents see only the
`name` and `description` until they activate one; helper files are listed,
not loaded, and **scripts are never executed**.

This page describes importing a skill from a folder already on this
machine, importing a published skill from a URL after a review, and
saving a finished agent run as a skill after a review. The catalog, the
indexer and the local-folder importer still never talk to the network;
only the URL-import path fetches, and only over https to a public
address.

## Format

A skill folder looks like this:

```
my-skill/
├── SKILL.md
├── references/            # optional
└── scripts/               # optional; copied as files, never run
```

`SKILL.md` must start with YAML frontmatter (the opening `---` is on line 1),
then a Markdown body:

```markdown
---
name: my-skill
description: What this skill does and when to use it.
---

# My skill

Instructions for the agent…
```

`name` and `description` are required. Optional frontmatter fields from the
specification (`license`, `compatibility`, `metadata`, `allowed-tools`) are
accepted; `allowed-tools` is ignored and never grants tools. YAML is parsed
with `safe_load` only; anchors and aliases are refused.

## Import from a folder (step 1)

On **Learning → Skills**, paste the absolute path of the folder and click
**Import from a folder**. Or call the local API:

```
POST /api/learning/skills/import
{"path": "/absolute/path/to/my-skill"}
```

The folder is validated, then copied into the vault under the skill's
`name`. The catalog is reindexed so it appears on the page (enabled by
default, like any other vault skill). A skill that already exists under
that name is left untouched (the API returns 409).

### Validation (clear errors)

The import is strict. Any of these stops the copy and returns an error
code plus a message:

| Code | When |
|---|---|
| `bad_path` | `path` is missing, not a string, or not absolute |
| `not_a_folder` | `path` points at a file, not a folder |
| `not_found` | there is no folder at that path |
| `missing_skill_md` | the folder has no `SKILL.md` |
| `no_frontmatter` / `yaml_unparseable` / `yaml_alias_not_allowed` / `frontmatter_not_mapping` | frontmatter is missing or not valid YAML |
| `name_missing` / `name_format` | `name` is missing, or is not 1–64 lowercase letters, digits and single hyphens |
| `name_dir_mismatch` | `name` does not match the folder name (the specification requires they match) |
| `description_missing` / `description_too_long` | `description` is missing, empty, or longer than 1024 characters |
| `too_large` | `SKILL.md` is over 256 KiB, or the folder is over 500 files / 10 MiB |
| `too_deep` | a helper file is nested more than 4 levels |
| `symlink` | the folder, `SKILL.md`, or any helper file or subdirectory is a symlink |
| `path_traversal` | a path would resolve outside the skill folder |
| `exists` | a vault skill with that name already exists |

Dot-files and dot-directories (for example `.git`) are skipped and not
copied. Execute bits are stripped from copied files. The import never
starts a process, never runs a script, and never follows a symlink.

The same storage and catalog already used for vault skills (`skills` /
`skill_state` in the local database, plus the folder on disk) receive the
import. No new tables.

## What is already there

- **Learning → Skills**: list, enable/disable, reindex, opt-in read of
  `~/.agents/skills` and `~/.claude/skills` (off by default, read only),
  export a vault skill to `~/.agents/skills`, trusted project repos
  (`<repo>/.agents/skills`).
- **Skill proposals**: the agent can *propose* a skill (including "from a
  local folder" of notes). Nothing is written until you approve. That is
  not the same as this import, which copies an existing `SKILL.md` folder
  as-is after validation.
- **Agent packs**: role packs that ship with the app are installed per
  agent on the Boardroom; they are separate from the vault library.

## Import from a URL (step 2)

On **Learning → Skills**, paste a public **https** address and click
**Review from a URL**. Two phases:

1. ConductLoop downloads the skill into a *staging* folder inside the app
   data directory (`$OPS_DATA_DIR/skill-import-staging/`), **outside** the
   vault. It is validated with the same rules as a local folder. The page
   shows a review: `name`, `description`, the `SKILL.md` body, each helper
   file with its size and sha256, any warnings (including *scripts
   present*), the source URL, and a review hash. Nothing is written to the
   vault yet.
2. **Approve** (and only Approve) copies the reviewed folder into the
   vault and reindexes, the same way as a local import. The call must
   include the staging id and the exact review hash that was shown. A
   mismatch, an expired preview, or a staging folder that changed on disk
   is refused. **Discard** deletes the staging folder. Previews expire
   after 30 minutes.

```
POST /api/learning/skills/import-url
{"url": "https://github.com/owner/repo/tree/main/my-skill"}

POST /api/learning/skills/import-url/approve
{"id": "<staging id>", "hash": "<review hash>"}

POST /api/learning/skills/import-url/discard
{"id": "<staging id>"}
```

These routes are local-only, like the rest of `/api/learning`.

### Addresses that work

- A GitHub *tree* or *blob* URL:
  `https://github.com/<owner>/<repo>/tree/<ref>/<path>`
  (a blob URL that ends in `SKILL.md` uses that file's parent folder).
  The first path segment after `tree/` or `blob/` is the ref (branch, tag
  or commit). Branches whose names contain a slash should use a direct
  archive URL instead. The download uses GitHub's `codeload` tarball; it
  never sends a token.
- A direct `.zip`, `.tar.gz` or `.tgz` URL. The archive must contain
  exactly one `SKILL.md` (or point at one folder via a GitHub path).

http addresses are refused.

### Security

- **https only.** Userinfo (credentials in the URL) is refused. No
  `Authorization` or cookie header is ever sent.
- **SSRF.** The host must resolve only to *public* addresses. Loopback,
  private, link-local, multicast and reserved IPs are refused, including
  after each redirect hop. `localhost` and `*.local` names are refused.
  At most three redirects; each hop is checked again.
- **Size and time.** The download is capped at 12 MiB; extraction uses
  the step-1 caps (500 files, 10 MiB, depth). The request times out after
  15 seconds.
- **Safe extraction.** Archive entries that would traverse (`..`), use an
  absolute path, or are a symlink (or other non-regular file) are
  refused. Dot-files are skipped. Scripts are copied as ordinary files
  and are **never executed**.
- A name that already exists in the vault is left untouched (409 on
  approve).

## Save a run as a skill

On a **task's run detail**, a finished run (status `done` or `error`)
has a **Save as skill** button. ConductLoop builds a *draft* skill from
that run and shows it for review. You can edit the name, the
description and the body. **Save** (and only Save) writes it to the
vault through the same validator and copy path as
[Import from a folder](#import-from-a-folder-step-1), then reindexes.
**Discard** closes the preview without writing anything. A skill is
never saved automatically.

The draft is deterministic: a template filled from the run's recorded
prompt, tool calls and output. It does not call a model. Secrets,
tokens and paths under your home directory are replaced (`[REDACTED:…]`
or `~`) before you see the preview, and again if you click Save.

```
POST /api/learning/skills/from-run
{"run_id": 12}

POST /api/learning/skills/from-run/save
{"run_id": 12, "name": "my-skill", "description": "…", "body": "# …"}
```

These routes are local-only, like the rest of `/api/learning`. The run
must already be finished; a running run is refused. Name and description
follow the same rules as a folder import (1–64 lowercase letters, digits
and single hyphens; description required, at most 1024 characters). A
name that already exists in the vault is left untouched (409).

### Suggest after a successful multi-step run

When a run finishes successfully (`done`) and recorded **at least two
tool calls**, the run detail shows a suggestion to save it as a skill.
A single tool call is treated as a one-off lookup, not a procedure.
**Review** opens the same draft panel as **Save as skill** above
(preview, then an explicit Save). The suggestion never writes to the
vault on its own, never auto-saves, and does not call a model.

**Dismiss** hides the suggestion for that run. The dismissal is stored
on this machine (`$OPS_DATA_DIR/skill-from-run-suggestions.json`) and
the suggestion is not shown again. A run that has already been saved as
a skill through the Save endpoint is also not suggested.

```
GET /api/learning/skills/from-run/suggest?task_id=12
GET /api/learning/skills/from-run/suggest?run_id=34

POST /api/learning/skills/from-run/dismiss
{"run_id": 34}
```

These routes are local-only.
