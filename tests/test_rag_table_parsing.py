import io
import zipfile
from uuid import uuid4
from xml.sax.saxutils import escape

import pytest

from src.engine.models.user_file import UserFile
from src.engine.services.rag_service import RAGService


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_RELS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col_letter(idx: int) -> str:
    label = ""
    n = idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        label = chr(65 + rem) + label
    return label


def _sheet_xml_inline(rows: list[list[str | None]]) -> str:
    max_row = len(rows)
    max_col = max((len(row) for row in rows), default=1)
    row_xml: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for col_index, value in enumerate(row):
            if value is None:
                continue
            ref = f"{_col_letter(col_index)}{row_index}"
            cells.append(
                f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            )
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    dimension = f"A1:{_col_letter(max_col - 1)}{max(max_row, 1)}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN_NS}">'
        f'<dimension ref="{dimension}"/>'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )


def _sheet_xml_shared(rows: list[list[str | None]], shared_indexes: dict[tuple[int, int], int]) -> str:
    max_row = len(rows)
    max_col = max((len(row) for row in rows), default=1)
    row_xml: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for col_index, value in enumerate(row):
            if value is None:
                continue
            ref = f"{_col_letter(col_index)}{row_index}"
            shared_index = shared_indexes[(row_index - 1, col_index)]
            cells.append(f'<c r="{ref}" t="s"><v>{shared_index}</v></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    dimension = f"A1:{_col_letter(max_col - 1)}{max(max_row, 1)}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN_NS}">'
        f'<dimension ref="{dimension}"/>'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )


def _xlsx_bytes(
    sheets: list[tuple[str, list[list[str | None]]]],
    *,
    string_mode: str,
) -> bytes:
    shared_strings: list[str] = []
    shared_indexes_by_sheet: list[dict[tuple[int, int], int]] = []
    for _, rows in sheets:
        sheet_indexes: dict[tuple[int, int], int] = {}
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                if value is None:
                    continue
                if string_mode == "shared":
                    sheet_indexes[(row_index, col_index)] = len(shared_strings)
                    shared_strings.append(value)
        shared_indexes_by_sheet.append(sheet_indexes)

    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    shared_override = (
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        if string_mode == "shared"
        else ""
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{sheet_overrides}{shared_override}</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{RELS_NS}">'
        f'<Relationship Id="rId1" Type="{DOC_RELS_NS}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_sheets = "".join(
        f'<sheet name="{escape(sheet_name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (sheet_name, _) in enumerate(sheets, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{MAIN_NS}" xmlns:r="{DOC_RELS_NS}">'
        f"<sheets>{workbook_sheets}</sheets>"
        "</workbook>"
    )
    workbook_relationships = "".join(
        f'<Relationship Id="rId{i}" Type="{DOC_RELS_NS}/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    styles_rid = len(sheets) + 1
    shared_rid = len(sheets) + 2
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{RELS_NS}">'
        f"{workbook_relationships}"
        f'<Relationship Id="rId{styles_rid}" Type="{DOC_RELS_NS}/styles" Target="styles.xml"/>'
        + (
            f'<Relationship Id="rId{shared_rid}" Type="{DOC_RELS_NS}/sharedStrings" Target="sharedStrings.xml"/>'
            if string_mode == "shared"
            else ""
        )
        + "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<styleSheet xmlns="{MAIN_NS}">'
        '<fonts count="1"><font/></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '<dxfs count="0"/>'
        '<tableStyles count="0" defaultTableStyle="TableStyleMedium9" defaultPivotStyle="PivotStyleLight16"/>'
        "</styleSheet>"
    )
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{MAIN_NS}" count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">'
        + "".join(f"<si><t>{escape(value)}</t></si>" for value in shared_strings)
        + "</sst>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        if string_mode == "shared":
            archive.writestr("xl/sharedStrings.xml", shared_xml)
        for sheet_index, (_, rows) in enumerate(sheets, start=1):
            if string_mode == "inline":
                sheet_xml = _sheet_xml_inline(rows)
            else:
                sheet_xml = _sheet_xml_shared(rows, shared_indexes_by_sheet[sheet_index - 1])
            archive.writestr(f"xl/worksheets/sheet{sheet_index}.xml", sheet_xml)
    return buffer.getvalue()


def _service() -> RAGService:
    return RAGService.__new__(RAGService)


def test_parse_table_rows_reads_inline_string_cells():
    data = _xlsx_bytes(
        [("Sheet", [["input", "formal_structure", "diagnosis"], ["visit 1", "ok", "I25"], ["visit 2", "warn", "J02"]])],
        string_mode="inline",
    )

    headers, rows, sheets = _service()._parse_table_rows(data, "inline.xlsx")

    assert headers == ["Sheet.input", "Sheet.formal_structure", "Sheet.diagnosis"]
    assert [row["row_index"] for row in rows] == [2, 3]
    assert rows[0]["text"] == "Sheet:Sheet, A:input=visit 1, B:formal_structure=ok, C:diagnosis=I25"
    assert sheets == [{"sheet": "Sheet", "min_row": 2, "max_row": 3}]


def test_parse_table_rows_reads_shared_string_cells():
    data = _xlsx_bytes(
        [("Sheet", [["input", "formal_structure", "diagnosis"], ["visit 1", "ok", "I25"]])],
        string_mode="shared",
    )

    headers, rows, sheets = _service()._parse_table_rows(data, "shared.xlsx")

    assert headers == ["Sheet.input", "Sheet.formal_structure", "Sheet.diagnosis"]
    assert len(rows) == 1
    assert rows[0]["row_index"] == 2
    assert sheets == [{"sheet": "Sheet", "min_row": 2, "max_row": 2}]


def test_parse_table_rows_header_only_sheet_has_no_data_rows():
    data = _xlsx_bytes(
        [("Sheet", [["input", "formal_structure", "diagnosis"]])],
        string_mode="inline",
    )

    headers, rows, sheets = _service()._parse_table_rows(data, "header-only.xlsx")

    assert headers == ["Sheet.input", "Sheet.formal_structure", "Sheet.diagnosis"]
    assert rows == []
    assert sheets == []


def test_parse_table_rows_blank_sheet_has_no_headers_or_rows():
    data = _xlsx_bytes([("Sheet", [])], string_mode="inline")

    headers, rows, sheets = _service()._parse_table_rows(data, "blank.xlsx")

    assert headers == []
    assert rows == []
    assert sheets == []


def test_parse_table_rows_keeps_sheet_names_and_excel_row_numbers():
    data = _xlsx_bytes(
        [
            ("First", [["name", "score"], ["Ada", "10"]]),
            ("Second", [[], ["item", "qty"], ["Pen", "2"], ["Pencil", "3"]]),
        ],
        string_mode="inline",
    )

    headers, rows, sheets = _service()._parse_table_rows(data, "multi.xlsx")

    assert headers == ["First.name", "First.score", "Second.item", "Second.qty"]
    assert [(row["sheet_name"], row["row_index"]) for row in rows] == [
        ("First", 2),
        ("Second", 3),
        ("Second", 4),
    ]
    assert sheets == [
        {"sheet": "First", "min_row": 2, "max_row": 2},
        {"sheet": "Second", "min_row": 3, "max_row": 4},
    ]


class FakeFileStorage:
    def __init__(self, file_record: UserFile):
        self.file_record = file_record
        self.status_calls: list[tuple] = []
        self.update_status_calls: list[dict] = []

    async def get_by_id(self, file_id):
        return self.file_record

    async def change_rag_status(self, file_id, status):
        self.status_calls.append((file_id, status))
        return self.file_record

    async def update_rag_status(self, **kwargs):
        self.update_status_calls.append(kwargs)
        return self.file_record


@pytest.mark.asyncio
async def test_table_ingest_failure_marks_failed_and_persists_rag_error():
    file_id = uuid4()
    file_record = UserFile(
        id=file_id,
        original_filename="blank.xlsx",
        file_type="xlsx",
        is_table=True,
    )
    file_storage = FakeFileStorage(file_record)
    service = _service()
    service._file_storage = file_storage
    data = _xlsx_bytes([("Sheet", [])], string_mode="inline")

    with pytest.raises(ValueError, match="table_parsing: No table rows extracted from file."):
        await service.ingest(
            org_id="org-1",
            user_id="user-1",
            filename="blank.xlsx",
            data=data,
            file_id=file_id,
        )

    assert file_storage.status_calls == [(file_id, "indexing")]
    assert file_storage.update_status_calls == [
        {
            "file_id": file_id,
            "rag_status": "failed",
            "rag_error": "table_parsing: No table rows extracted from file.",
            "indexed_at": None,
        }
    ]
