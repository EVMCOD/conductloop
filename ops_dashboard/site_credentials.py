"""Where the Google credentials of a property / site live.

They used to sit in the database, in clear text: ``ga_sites.service_account_json``
and ``gsc_sites.credentials_json`` both hold an OAuth *authorized_user* blob
with a refresh token — enough to act as the Google account and read its
Analytics and Search Console data. On the owner's machine that was 14 rows
(7 GA properties, 7 Search Console sites) with a live refresh token in each.

They now live in the secrets store (the macOS keychain, or a 0600 file when
``OPS_DISABLE_KEYCHAIN=1``) and the row keeps only the reference: the column is
emptied by the migration and stays empty afterwards.

The column is still read as a fallback, for two reasons: a database that has
not been migrated yet keeps working, and the migration itself needs the value
before it can clear the column. Nothing else writes to the column.
"""
from __future__ import annotations

from typing import Any

from . import secrets_store

GA_PREFIX = "ga-site-credentials-"
GSC_PREFIX = "gsc-site-credentials-"


def ga_secret_name(site_id: Any) -> str:
    return f"{GA_PREFIX}{site_id}"


def gsc_secret_name(site_id: Any) -> str:
    return f"{GSC_PREFIX}{site_id}"


def _site_id(row: dict) -> int | None:
    try:
        return int(row.get("id"))
    except (TypeError, ValueError):
        return None


def _stored(prefix: str, row: dict, column: str) -> str:
    """The credential of a site: from the secrets store, or from the column."""
    sid = _site_id(row)
    if sid is not None:
        value = secrets_store.get_secret(f"{prefix}{sid}")
        if value:
            return value
    return (row.get(column) or "").strip()


def get_ga_credentials(site_row: dict) -> str:
    """The OAuth blob of a GA property (empty string when there is none)."""
    return _stored(GA_PREFIX, site_row, "service_account_json")


def get_gsc_credentials(site_row: dict) -> str:
    """The OAuth blob of a Search Console site (empty string when none)."""
    return _stored(GSC_PREFIX, site_row, "credentials_json")


def set_ga_credentials(site_id: Any, blob: str) -> None:
    secrets_store.set_secret(ga_secret_name(site_id), blob)


def set_gsc_credentials(site_id: Any, blob: str) -> None:
    secrets_store.set_secret(gsc_secret_name(site_id), blob)


def delete_ga_credentials(site_id: Any) -> None:
    secrets_store.delete_secret(ga_secret_name(site_id))


def delete_gsc_credentials(site_id: Any) -> None:
    secrets_store.delete_secret(gsc_secret_name(site_id))
