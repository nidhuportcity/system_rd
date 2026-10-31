from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import BigInteger, Integer, Numeric, SmallInteger, Text

from app.structured.pipeline import InvalidFile, infer_type, normalize_headers, parse_file, prepare
from app.structured.router import get_ingestion_service, router
from app.structured.service import compatible


def process(content: bytes, name="employees.csv"):
    parsed = parse_file(content, name, 1000, 200)
    return parsed, *prepare(parsed)


def test_normalize_collision_and_injection():
    names = normalize_headers(["Employee ID", "Joining Date", "Monthly Salary (LKR)", "a b", "a-b", "a_b_2", 'x"; DROP TABLE users;--', "1", "", "a" * 90, "a" * 90])
    assert names[:6] == ["employee_id", "joining_date", "monthly_salary_lkr", "a_b", "a_b_2", "a_b_2_2"]
    assert all(len(name) <= 63 and name.replace("_", "").isalnum() for name in names)
    assert len(names) == len(set(names))


def test_types_nulls_and_precise_values():
    parsed, schema, rows, errors = process(
        b"Employee ID,Joining Date,Salary,Active,Event,Large\n001,2026-01-01,123.45,yes,2026-01-01T12:30:00,2147483648\n002,, 456.10 ,no,2026-01-02T01:00:00,2147483649\n"
    )
    assert [c["data_type"] for c in schema] == ["TEXT", "DATE", "NUMERIC", "BOOLEAN", "TIMESTAMP", "BIGINT"]
    assert rows[0][1] == dict(employee_id="001", joining_date=date(2026, 1, 1), salary=Decimal("123.45"), active=True, event=datetime(2026, 1, 1, 12, 30), large=2147483648)
    assert rows[1][1]["joining_date"] is None
    assert schema[1]["profile"]["null_count"] == 1
    assert parsed.received == 2 and not errors


def test_minority_bad_values_reject_whole_row():
    _, schema, rows, errors = process(b"amount\n" + b"42\n" * 19 + b"oops\n")
    assert schema[0]["data_type"] == "INTEGER"
    assert len(rows) == 19
    assert errors == [{"row_number": 21, "column_name": "amount", "stage": "VALIDATE", "message": "Invalid INTEGER value."}]


def test_numeric_widens_instead_of_rejecting_valid_outliers():
    assert infer_type("amount", ["1"] * 99 + ["1.25"]) == "NUMERIC"
    assert infer_type("amount", ["1"] * 99 + [str(2**40)]) == "BIGINT"
    assert infer_type("amount", ["1"] * 99 + [str(2**70)]) == "NUMERIC"
    assert infer_type("code", ["001", "002"]) == "TEXT"
    assert infer_type("employee_id", ["1", "2"]) == "TEXT"
    assert infer_type("amount", ["1"] * 19 + ["9" * 5000]) == "INTEGER"


def test_structural_errors_and_nul_do_not_abort_valid_rows():
    parsed, _, rows, errors = process(b"name,value\nAlice,1\nBob,2,extra\nBad\x00Name,3\n")
    assert parsed.received == 3
    assert len(rows) == 1
    assert {e["row_number"] for e in errors} == {3, 4}


def test_excel_leading_zero_display_and_formula_rejection():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Employee ID", "Amount", "Joined"])
    sheet.append([1, 1.25, datetime(2026, 1, 1)])
    sheet["A2"].number_format = "000"
    sheet.append(["002", "=1+1", datetime(2026, 1, 2)])
    buffer = BytesIO()
    workbook.save(buffer)
    _, schema, rows, errors = process(buffer.getvalue(), "employees.xlsx")
    assert rows[0][1]["employee_id"] == "001"
    assert rows[0][1]["joined"] == date(2026, 1, 1)
    assert len(rows) == 1 and errors[0]["row_number"] == 3


@pytest.mark.parametrize("content,name", [(b"", "x.csv"), (b"name\n", "x.csv"), (b"bad", "x.xlsx"), (b"\xff", "x.csv")])
def test_unparseable_inputs(content, name):
    with pytest.raises(InvalidFile):
        parse_file(content, name, 100, 20)


def test_limits_and_type_compatibility():
    with pytest.raises(InvalidFile):
        parse_file(b"a\n1\n2\n", "x.csv", 1, 1)
    with pytest.raises(InvalidFile):
        parse_file(b"a,b\n1,2", "x.csv", 10, 1)
    assert compatible("INTEGER", BigInteger())
    assert compatible("INTEGER", Integer())
    assert not compatible("INTEGER", SmallInteger())
    assert not compatible("BIGINT", Integer())
    assert not compatible("NUMERIC", Numeric(10, 2))
    assert compatible("NUMERIC", Numeric())
    assert not compatible("TEXT", Integer())
    assert compatible("TEXT", Text())


def test_route_validation_and_delegation():
    app = FastAPI()
    app.include_router(router)

    class FakeService:
        def ingest(self, content, filename):
            assert content == b"name\nAlice" and filename == "x.csv"
            return dict(status="COMPLETED", ingestion_id="job", target_table="structured_data.x", action="CREATE", rows_received=1, rows_inserted=1, rows_rejected=0, schema=[], errors=[], error_count=0)

    app.dependency_overrides[get_ingestion_service] = FakeService
    client = TestClient(app)
    result = client.post("/structured/ingest", files={"file": ("x.csv", b"name\nAlice")})
    assert result.status_code == 201
    assert result.json()["rows_inserted"] == 1 and "schema" in result.json()
    assert client.post("/structured/ingest", files={"file": ("x.txt", b"a")}).status_code == 415
    assert client.post("/structured/ingest", files={"file": ("x.csv", b"")}).status_code == 400
