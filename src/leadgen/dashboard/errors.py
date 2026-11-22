"""Friendly error messages for the dashboard: what went wrong and what to do, instead of a traceback.

Only the standard library and Streamlit are imported here, on purpose: this module must still work when
the rest of leadgen can't be imported (e.g. a dashboard left running while the code was updated).
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass

log = logging.getLogger("leadgen.dashboard")

RESTART = "Restart the dashboard: press **Ctrl+C** in its terminal, then run `uv run leads dashboard`."
DB_MODULES = ("psycopg", "psycopg_pool", "sqlite3", "gspread", "google.auth")
DB_ERROR_NAMES = {
    "OperationalError",
    "InterfaceError",
    "PoolTimeout",
    "PoolClosed",
    "DatabaseError",
    "APIError",
}


@dataclass
class FriendlyError:
    title: str
    hint: str
    details: str  # full traceback, shown folded
    can_retry: bool = True


def describe_error(exc: BaseException) -> FriendlyError:
    """Classify an exception into a message a dashboard user can act on."""
    details = "".join(traceback.format_exception(exc)).strip()
    name, module, text = type(exc).__name__, type(exc).__module__ or "", str(exc).strip()

    if isinstance(exc, ImportError) or (isinstance(exc, AttributeError) and "leadgen" in text):
        return FriendlyError(
            "The app's code was updated while the dashboard was running.",
            "The page reloaded with the new code, but parts of the old code are still in memory. " + RESTART,
            details,
            can_retry=False,
        )
    if name == "ConfigError" and "cannot connect" in text.lower():  # often temporary: Neon waking up
        return FriendlyError(
            "Couldn't connect to the database.",
            f"{text}\n\nIf you use Neon, it may be waking up: click **Retry** in a few seconds. If it keeps "
            "failing, check `DATABASE_URL` in `.env` and run `uv run leads doctor`.",
            details,
        )
    if name == "ConfigError":
        return FriendlyError(
            "Configuration problem.",
            f"{text}\n\nFix `config/config.yaml` or `.env`, then " + RESTART[0].lower() + RESTART[1:],
            details,
            can_retry=False,
        )
    if module.startswith(DB_MODULES) or name in DB_ERROR_NAMES:
        return FriendlyError(
            "Couldn't read from or write to the database.",
            "If you use Neon, it may be waking up after an idle pause, or the connection dropped: click "
            "**Retry**. If it keeps failing, check your internet connection and `DATABASE_URL` in `.env`, "
            "or run `uv run leads doctor`.",
            details,
        )
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return FriendlyError(
            "A network request failed.",
            "Check your internet connection and click **Retry**. `uv run leads doctor` tests every service.",
            details,
        )
    return FriendlyError(
        f"Something went wrong ({name}).",
        (text or "No details.") + "\n\nClick **Retry**. If it happens again, the details below and "
        "`logs/leadgen.log` show what failed.",
        details,
    )


def render_error(exc: BaseException, *, where: str = "", key: str = "retry") -> None:
    """Show the friendly message (with a Retry button when retrying can help) and log the traceback."""
    import streamlit as st

    err = describe_error(exc)
    log.error("dashboard error%s: %s", f" ({where})" if where else "", err.title, exc_info=exc)
    st.error(f"**{(where + ': ') if where else ''}{err.title}**\n\n{err.hint}", icon="⚠️")
    if err.can_retry and st.button("Retry", key=key, type="primary"):
        st.rerun()
    with st.expander("Technical details"):
        st.code(err.details, language=None)
