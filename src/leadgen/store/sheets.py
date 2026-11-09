"""Google Sheets lead store.

Tabs: Leads, Activities, Suppression, Runs (+ a formula-driven Summary).
- Columns are mapped by header NAME, so users can reorder or add their own columns safely.
- All writes use valueInputOption=RAW so scraped text like "=IMPORTXML(...)" is stored as text,
  never executed as a formula (spreadsheet formula injection).
- Writes are batched to stay well within the Sheets API quota (60 writes/min/user); gspread's
  BackOffHTTPClient retries 429s with exponential backoff.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import gspread
from gspread.utils import rowcol_to_a1

from leadgen.logging_setup import get_logger
from leadgen.models import (
    ACTIVITY_COLUMNS,
    LEAD_COLUMNS,
    RUN_COLUMNS,
    SUPPRESSION_COLUMNS,
    EmailStatus,
    Lead,
    LeadStatus,
    RunStats,
    SuppressionEntry,
    utcnow_iso,
)

log = get_logger("sheets")

LEADS, ACTIVITIES, SUPPRESSION, RUNS, SUMMARY = "Leads", "Activities", "Suppression", "Runs", "Summary"
TABS: dict[str, list[str]] = {
    LEADS: LEAD_COLUMNS,
    ACTIVITIES: ACTIVITY_COLUMNS,
    SUPPRESSION: SUPPRESSION_COLUMNS,
    RUNS: RUN_COLUMNS,
}
APPEND_CHUNK = 100
UPDATE_CHUNK = 500


def col_letter(index_1based: int) -> str:
    return rowcol_to_a1(1, index_1based).rstrip("0123456789")


class SheetsStore:
    name = "sheets"

    def __init__(self, spreadsheet: Any) -> None:
        """`spreadsheet` is a gspread.Spreadsheet (or a test double with the same methods)."""
        self.sh = spreadsheet
        self._ws: dict[str, Any] = {}
        self._headers: dict[str, list[str]] = {}

    @classmethod
    def connect(cls, sheet_id: str, service_account_file: Path) -> SheetsStore:
        gc = gspread.service_account(
            filename=str(service_account_file), http_client=gspread.BackOffHTTPClient
        )
        return cls(gc.open_by_key(sheet_id))

    @property
    def title(self) -> str:
        return getattr(self.sh, "title", "")

    # ------------------------------------------------------------------ worksheet plumbing
    def _worksheet(self, title: str) -> Any:
        if title in self._ws:
            return self._ws[title]
        try:
            ws = self.sh.worksheet(title)
        except gspread.WorksheetNotFound:
            cols = TABS.get(title, [])
            ws = self.sh.add_worksheet(title=title, rows=1000, cols=max(len(cols), 26))
            if cols:
                ws.update([cols], "A1", value_input_option="RAW")
        self._ws[title] = ws
        return ws

    def _header(self, title: str, refresh: bool = False) -> list[str]:
        if refresh or title not in self._headers:
            header = self._worksheet(title).row_values(1)
            if not header:
                self._ensure_header(title)
                header = self._worksheet(title).row_values(1)
            self._headers[title] = header
        return self._headers[title]

    def _ensure_header(self, title: str) -> list[str]:
        ws = self._worksheet(title)
        header = ws.row_values(1)
        wanted = TABS[title]
        missing = [c for c in wanted if c not in header]
        if missing:
            new_header = header + missing
            if len(new_header) > ws.col_count:
                ws.resize(cols=len(new_header))
            ws.update([new_header], "A1", value_input_option="RAW")
            log.info("added columns to sheet", extra={"tab": title, "columns": ",".join(missing)})
            header = new_header
        self._headers[title] = header
        return header

    def _rows_as_dicts(self, title: str) -> list[dict[str, str]]:
        values = self._worksheet(title).get_all_values()
        if not values:
            return []
        header = values[0]
        self._headers[title] = header
        return [dict(zip(header, row + [""] * (len(header) - len(row)), strict=False)) for row in values[1:]]

    def _append(self, title: str, rows: list[dict[str, str]]) -> None:
        if not rows:
            return
        header = self._header(title)
        ws = self._worksheet(title)
        matrix = [[str(r.get(col, "") or "") for col in header] for r in rows]
        for i in range(0, len(matrix), APPEND_CHUNK):
            ws.append_rows(matrix[i : i + APPEND_CHUNK], value_input_option="RAW", table_range="A1")

    def _delete_rows(self, title: str, row_numbers: list[int]) -> None:
        """Delete 1-based row numbers in one API call (descending so indices stay valid)."""
        if not row_numbers:
            return
        ws = self._worksheet(title)
        requests = [
            {
                "deleteDimension": {
                    "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": r - 1, "endIndex": r}
                }
            }
            for r in sorted(set(row_numbers), reverse=True)
            if r > 1
        ]
        if requests:
            self.sh.batch_update({"requests": requests})

    # ------------------------------------------------------------------ setup
    def setup(self) -> None:
        requests: list[dict[str, Any]] = []
        for title in TABS:
            ws = self._worksheet(title)
            header = self._ensure_header(title)
            requests += [
                {"updateSheetProperties": {
                    "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {
                    "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold"}},
            ]  # fmt: skip
            if title == LEADS:
                requests += [
                    _dropdown(ws.id, header.index("status"), [s.value for s in LeadStatus]),
                    _dropdown(ws.id, header.index("email_status"), [s.value for s in EmailStatus]),
                ]
        self.sh.batch_update({"requests": requests})
        self._write_summary()
        self._drop_default_sheet()
        log.info("google sheet ready", extra={"sheet": self.title})

    def _write_summary(self) -> None:
        header = self._header(LEADS, refresh=True)

        def c(name: str) -> str:
            return col_letter(header.index(name) + 1)

        rng = f"{LEADS}!A:{col_letter(len(header))}"
        lid, ind, ctry, city, st, est, src = (c(n) for n in (
            "lead_id", "industry", "country", "city", "status", "email_status", "source"))  # fmt: skip

        def q(select: str) -> str:
            return f'=QUERY({rng}, "{select}", 1)'

        blocks = {
            "A1": "Leads by industry",
            "A2": q(f"select {ind}, count({lid}) where {lid} <> '' group by {ind} order by count({lid}) desc "
                    f"label {ind} 'Industry', count({lid}) 'Leads'"),
            "D1": "Leads by location",
            "D2": q(f"select {ctry}, {city}, count({lid}) where {lid} <> '' group by {ctry}, {city} "
                    f"order by count({lid}) desc label {ctry} 'Country', {city} 'City', count({lid}) 'Leads'"),
            "H1": "Pipeline status",
            "H2": q(f"select {st}, count({lid}) where {lid} <> '' group by {st} label {st} 'Status', count({lid}) 'Leads'"),
            "K1": "Email quality",
            "K2": q(f"select {est}, count({lid}) where {lid} <> '' group by {est} "
                    f"label {est} 'Email status', count({lid}) 'Leads'"),
            "N1": "Source",
            "N2": q(f"select {src}, count({lid}) where {lid} <> '' group by {src} label {src} 'Source', count({lid}) 'Leads'"),
            "Q1": "Industry x status",
            "Q2": q(f"select {ind}, count({lid}) where {lid} <> '' group by {ind} pivot {st} label {ind} 'Industry'"),
        }  # fmt: skip
        try:
            ws = self.sh.worksheet(SUMMARY)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = self.sh.add_worksheet(title=SUMMARY, rows=200, cols=40)
        ws.batch_update(
            [{"range": k, "values": [[v]]} for k, v in blocks.items()], value_input_option="USER_ENTERED"
        )

    def _drop_default_sheet(self) -> None:
        default_names = ("Sheet1", "Tabellenblatt1", "Feuille 1", "Hoja 1")
        for ws in self.sh.worksheets():
            is_empty_default = ws.title in default_names and not any(any(row) for row in ws.get_all_values())
            if is_empty_default:
                self.sh.del_worksheet(ws)

    # ------------------------------------------------------------------ leads
    def load_leads(self) -> list[Lead]:
        return [Lead.from_row(r) for r in self._rows_as_dicts(LEADS) if r.get("lead_id")]

    def add_leads(self, leads: list[Lead]) -> None:
        self._header(LEADS)
        self._append(LEADS, [lead.to_row() for lead in leads])

    def _row_numbers(self, title: str, key_col: str) -> dict[str, list[int]]:
        header = self._header(title, refresh=True)
        if key_col not in header:
            return {}
        values = self._worksheet(title).col_values(header.index(key_col) + 1)
        rows: dict[str, list[int]] = {}
        for i, v in enumerate(values[1:], start=2):
            if v:
                rows.setdefault(v, []).append(i)
        return rows

    def update_leads(self, updates: dict[str, dict[str, str]]) -> int:
        if not updates:
            return 0
        rows = self._row_numbers(LEADS, "lead_id")
        header = self._headers[LEADS]
        data: list[dict[str, Any]] = []
        updated = 0
        now = utcnow_iso()
        for lead_id, fields in updates.items():
            if lead_id not in rows:
                log.warning("lead not found for update", extra={"lead_id": lead_id})
                continue
            fields = {**fields, "updated_at": fields.get("updated_at", now)}
            row = rows[lead_id][0]
            for name, value in fields.items():
                if name in header and name != "lead_id":
                    data.append(
                        {"range": rowcol_to_a1(row, header.index(name) + 1), "values": [[str(value)]]}
                    )
            updated += 1
        ws = self._worksheet(LEADS)
        for i in range(0, len(data), UPDATE_CHUNK):
            ws.batch_update(data[i : i + UPDATE_CHUNK], value_input_option="RAW")
        return updated

    def delete_leads(self, lead_ids: list[str]) -> int:
        rows = self._row_numbers(LEADS, "lead_id")
        targets = [r for i in lead_ids for r in rows.get(i, [])]
        self._delete_rows(LEADS, targets)
        return len({i for i in lead_ids if i in rows})

    # ------------------------------------------------------------------ suppression / activity / runs
    def load_suppression(self) -> set[str]:
        return {r["hash"] for r in self._rows_as_dicts(SUPPRESSION) if r.get("hash")}

    def add_suppression(self, entries: list[SuppressionEntry]) -> None:
        existing = self.load_suppression()
        new = [e.to_row() for e in entries if e.hash not in existing]
        self._append(SUPPRESSION, new)

    def add_activity(self, lead_id: str, type_: str, details: str = "", campaign: str = "") -> None:
        self._append(ACTIVITIES, [{"lead_id": lead_id, "timestamp": utcnow_iso(), "type": type_,
                                   "details": details, "campaign": campaign}])  # fmt: skip

    def delete_activities(self, lead_ids: list[str]) -> int:
        rows = self._row_numbers(ACTIVITIES, "lead_id")
        targets = [r for i in lead_ids for r in rows.get(i, [])]
        self._delete_rows(ACTIVITIES, targets)
        return len(targets)

    def log_run(self, stats: RunStats) -> None:
        self._append(RUNS, [stats.to_row()])

    def list_runs(self, limit: int = 20) -> list[dict[str, str]]:
        return list(reversed(self._rows_as_dicts(RUNS)[-limit:]))


def _dropdown(sheet_id: int, col_index0: int, options: list[str]) -> dict[str, Any]:
    return {
        "setDataValidation": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": col_index0,
                      "endColumnIndex": col_index0 + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": o} for o in options]},
                     "showCustomUi": True, "strict": False},
        }
    }  # fmt: skip
