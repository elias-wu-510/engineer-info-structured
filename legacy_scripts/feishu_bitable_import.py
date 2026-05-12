#!/usr/bin/env python3
import csv
import io
import json
import os
import sys
import time
import urllib.request

AUTH_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
CREATE_RECORD_URL = "https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
BATCH_CREATE_URL = "https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
UPDATE_RECORD_URL = "https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"

REQUIRED_ENV = [
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_BITABLE_APP_TOKEN",
    "FEISHU_BITABLE_TABLE_ID",
]

CSV_COLUMNS = ["發布用戶", "發送時間", "日期", "分區", "樓棟", "樓層", "分判", "工序", "人數"]
PRIMARY_FIELD = "發布用戶"
BATCH_SIZE = 200


def fail(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def require_env():
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        fail("Missing environment variables: " + ", ".join(missing))


def request_json(url: str, payload: dict | None = None, headers: dict | None = None, method: str | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method or ("POST" if payload is not None else "GET"))
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def get_tenant_access_token() -> str:
    payload = {
        "app_id": os.environ["FEISHU_APP_ID"],
        "app_secret": os.environ["FEISHU_APP_SECRET"],
    }
    result = request_json(AUTH_URL, payload)
    if result.get("code") != 0:
        fail(f"Failed to get tenant access token: {result}")
    token = result.get("tenant_access_token")
    if not token:
        fail(f"tenant_access_token missing in response: {result}")
    return token


def parse_csv_text(csv_text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        fail("CSV is empty")
    if reader.fieldnames != CSV_COLUMNS:
        fail(
            "CSV header mismatch. Expected: " + ",".join(CSV_COLUMNS) +
            " | Got: " + ",".join(reader.fieldnames)
        )

    records = []
    for row in reader:
        fields = {k: (row.get(k, "") or "").strip() for k in CSV_COLUMNS}
        if not any(fields.values()):
            continue
        headcount = fields["人數"]
        if headcount.lower() == "null" or headcount == "":
            fields["人數"] = "null"
        else:
            if not headcount.isdigit():
                fail(f"Invalid 人數 value: {headcount}")
            fields["人數"] = headcount
        records.append(fields)
    return records


def batch_chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def create_record(fields: dict, token: str) -> str:
    app_token = os.environ["FEISHU_BITABLE_APP_TOKEN"]
    table_id = os.environ["FEISHU_BITABLE_TABLE_ID"]
    url = CREATE_RECORD_URL.format(app_token=app_token, table_id=table_id)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"fields": {PRIMARY_FIELD: fields[PRIMARY_FIELD]}}
    result = request_json(url, payload, headers=headers)
    if result.get("code") != 0:
        fail(f"Failed to create record: {result}")
    record_id = result.get("data", {}).get("record", {}).get("record_id")
    if not record_id:
        fail(f"record_id missing in create response: {result}")
    return record_id


def batch_create_records(records: list[dict], token: str) -> list[str]:
    app_token = os.environ["FEISHU_BITABLE_APP_TOKEN"]
    table_id = os.environ["FEISHU_BITABLE_TABLE_ID"]
    url = BATCH_CREATE_URL.format(app_token=app_token, table_id=table_id)
    headers = {"Authorization": f"Bearer {token}"}
    # Create records with full fields in one batch. Older implementation created
    # only the primary field, then issued one PUT per row; that was slow and could
    # block the hook for minutes when Feishu was slow.
    payload = {"records": [{"fields": r} for r in records]}
    start = time.monotonic()
    print(f"Feishu batch_create start rows={len(records)}", flush=True)
    result = request_json(url, payload, headers=headers)
    elapsed = time.monotonic() - start
    print(f"Feishu batch_create done rows={len(records)} elapsed={elapsed:.2f}s code={result.get('code')}", flush=True)
    if result.get("code") != 0:
        # fallback to single-create path if batch create is not accepted for this table
        print(f"Feishu batch_create failed, falling back to per-record create/update: {result}", file=sys.stderr, flush=True)
        return [create_record(r, token) for r in records]
    items = result.get("data", {}).get("records", [])
    record_ids = [item.get("record_id") or item.get("id") for item in items]
    if len(record_ids) != len(records) or any(not rid for rid in record_ids):
        fail(f"Unexpected batch create response: {result}")
    return record_ids


def update_record(record_id: str, fields: dict, token: str):
    app_token = os.environ["FEISHU_BITABLE_APP_TOKEN"]
    table_id = os.environ["FEISHU_BITABLE_TABLE_ID"]
    url = UPDATE_RECORD_URL.format(app_token=app_token, table_id=table_id, record_id=record_id)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"fields": {k: v for k, v in fields.items() if k != PRIMARY_FIELD}}
    if not payload["fields"]:
        return
    result = request_json(url, payload, headers=headers, method="PUT")
    if result.get("code") != 0:
        fail(f"Failed to update record {record_id}: {result}")


def upload_records(records: list[dict], token: str):
    created = 0
    total = len(records)
    print(f"Feishu upload start total={total}", flush=True)
    for chunk_index, chunk in enumerate(batch_chunks(records, BATCH_SIZE), start=1):
        chunk_start = time.monotonic()
        record_ids = batch_create_records(chunk, token)
        created += len(record_ids)
        print(
            f"Feishu upload chunk={chunk_index} created={len(record_ids)} "
            f"elapsed={time.monotonic() - chunk_start:.2f}s progress={created}/{total}",
            flush=True,
        )
    print(f"Feishu upload done total={created}", flush=True)
    return created


def main():
    require_env()

    if len(sys.argv) > 2:
        fail("Usage: python3 scripts/feishu_bitable_import.py [csv-file]\\nIf csv-file is omitted, read CSV from stdin.")

    if len(sys.argv) == 2:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            csv_text = f.read()
    else:
        csv_text = sys.stdin.read()

    if not csv_text.strip():
        fail("No CSV input provided")

    records = parse_csv_text(csv_text)
    if not records:
        fail("No valid rows found in CSV")

    token_start = time.monotonic()
    print("Feishu token start", flush=True)
    token = get_tenant_access_token()
    print(f"Feishu token done elapsed={time.monotonic() - token_start:.2f}s", flush=True)
    created = upload_records(records, token)
    print(f"Imported {created} records to Feishu Bitable")


if __name__ == "__main__":
    main()
