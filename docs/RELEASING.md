# Releasing the desktop app

The macOS app is built, signed, notarized and published **by hand, on the
maintainer's Mac**, with `scripts/release-desktop.sh` (see
[desktop/docs/DISTRIBUTION.md](../desktop/docs/DISTRIBUTION.md) for the pipeline
and the channels). CI never signs or publishes anything.

## Step 0: pre-flight check (dry run)

Run this before `release-desktop.sh`. It only reads files and git state: it does
not build, sign, publish, tag, or call any network API, so it is safe to run as
often as you like.

```bash
python3 scripts/release_check.py --version 1.2.1
# optional: --notes-file notes.md   release notes from a file instead of CHANGELOG.md
# optional: --strict                a WARN also fails the run
```

Each check prints `OK`, `WARN` or `FAIL`; the exit status is `1` if anything
failed (`2` for a bad `--version`), `0` otherwise. Fix every `FAIL`, then read
the `WARN` lines.

| Check | What it verifies |
|---|---|
| `version` | `X.Y.Z` is the version in `pyproject.toml`, `ops_dashboard/__init__.py`, `desktop/src-tauri/tauri.conf.json`, `desktop/src-tauri/Cargo.toml` and `desktop/package.json` (if present). `Cargo.lock` (the `conductloop-desktop` entry) and `desktop/package-lock.json` only `WARN`. |
| `changelog` | `CHANGELOG.md` has a `## ` heading naming the version **and** an ISO date. `WARN` if the date is in the future or entries are still under `## Unreleased`. |
| `notes` | The release notes are not empty: the body of that CHANGELOG section, or the file passed with `--notes-file`. |
| `updater-key` | The updater public key in `tauri.conf.json` has key id `770EA4C292DC6BD2`, and `desktop/UPDATER_KEY_ID` pins the same id. |
| `macos` | `bundle.macOS.minimumSystemVersion` is `13.0`. |
| `endpoint` | The updater endpoints include `https://github.com/EVMCOD/conductloop-releases/releases/latest/download/update-macos-aarch64.json`. |
| `git` | The working tree is clean (nothing modified, staged or untracked). `WARN` if the branch is not `main` or the `vX.Y.Z` tag already exists locally. |

### Preparing what the check expects

`release-desktop.sh` only bumps the version in `Cargo.toml` and
`tauri.conf.json`, so bump the rest yourself first and commit it, otherwise the
check (rightly) fails:

1. Set the new version in `pyproject.toml`, `ops_dashboard/__init__.py`,
   `desktop/src-tauri/tauri.conf.json`, `desktop/src-tauri/Cargo.toml` and
   `desktop/package.json` (let `cargo` refresh `Cargo.lock`).
2. In `CHANGELOG.md`, turn `## Unreleased` into the release section, with the
   version and the release date, for example
   `## 1.2.1 — 2026-11-02 — Short title`. Its body is what the check treats as
   the release notes.
3. Commit, make sure the tree is clean, and run the check.

### How the updater key id is checked

`plugins.updater.pubkey` in `tauri.conf.json` is the base64 of a minisign `.pub`
file:

```
untrusted comment: minisign public key: 770EA4C292DC6BD2
RWTSa9yS...                          <- base64 of 42 bytes
```

The second line decodes to 42 bytes: the 2-byte algorithm `Ed`, an **8-byte key
id stored little-endian**, and the 32-byte Ed25519 public key. The key id is
those 8 bytes reversed and written as upper-case hex; it is the same value
minisign puts in every signature it makes, which is what the Tauri updater
compares. So the id is derived from the key bytes themselves, not read from the
comment (a label anyone can edit). If the comment disagrees with the bytes, or
the value does not decode, the check fails. You can do the same by hand:

```bash
python3 scripts/check_updater_pubkey.py --expected 770EA4C292DC6BD2
```

`release_check.py` reuses that decoder and compares against the id written in
the script (`EXPECTED_KEY_ID`), so a change of the pinned key has to be made, on
purpose, in `desktop/UPDATER_KEY_ID`, `scripts/release_check.py` and the key
itself. If a key id could ever not be derived, the check fails rather than
trusting the comment.

## After the check passes

Follow [desktop/docs/DISTRIBUTION.md](../desktop/docs/DISTRIBUTION.md):
`./scripts/release-desktop.sh <VERSION>`. That script runs its own key checks
against the signing key and the produced signature, which this dry run cannot
do because it never sees the private key or the `.sig`.

## Linux and Windows: one command

`release-desktop.sh` builds, signs and publishes the **macOS** app only. The
Linux and Windows builds were done by hand, one step at a time: dispatch the
two bundle workflows, wait, download the artifacts, write `SHA256SUMS`,
upload the files to the release, copy the previous release's notes and edit
them. That is six steps to remember, and forgetting one leaves a release with
a broken download.

`scripts/release-platforms.sh <VERSION>` does all of it:

```bash
./scripts/release-platforms.sh 1.2.28           # publish Linux and Windows
./scripts/release-platforms.sh 1.2.28 --dry-run # print the plan, touch nothing
```

It dispatches `linux-bundles.yml` and `windows-bundles.yml` on `main`, waits
for both runs, downloads the four bundles, writes `SHA256SUMS`, uploads them
to the tag `v<VERSION>` with `gh release upload --clobber`, rewrites the
release notes and checks that every download URL answers 200.

**It never creates the tag and never creates a release.** The tag belongs to
the macOS release: run `scripts/release-desktop.sh <VERSION>` first, and this
script adds the other platforms to that same release. That is also why the
Linux and Windows assets must go in the same tag as the macOS ones: the
updater endpoint (`releases/latest/download/update-macos-aarch64.json`) must
keep answering.

The notes are built by `scripts/release_notes.py`, out of the `## <VERSION>`
section of `CHANGELOG.md` and the real download URLs:

```bash
python3 scripts/release_notes.py 1.2.28                # print the notes
python3 scripts/release_notes.py 1.2.28 --out notes.md # write them
python3 scripts/release_notes.py 1.2.28 --urls         # the download URLs
```

It fails (exit 1) when the changelog has no section for that version, or when
the section is empty, so a release is never published with notes that say
nothing. `--dry-run` prints the notes and writes nothing.

The whole sequence, in order:

```bash
python3 scripts/release_check.py --version 1.2.28   # pre-flight
./scripts/release-desktop.sh 1.2.28                 # macOS: builds, signs, publishes
./scripts/release-platforms.sh 1.2.28               # Linux and Windows
```

