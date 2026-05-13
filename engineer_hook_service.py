#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LEGACY_DIR = SCRIPT_DIR / 'legacy_scripts'
DEFAULT_ENV_FILE = SCRIPT_DIR / '.env'
DEFAULT_LOG_DIR = None
DEFAULT_STATE_FILE = SCRIPT_DIR / '.state' / 'engineer-info-structured-hook-state.json'
DEFAULT_IMPORT_STATE_FILE = SCRIPT_DIR / '.state' / 'feishu-import-state.json'
DEFAULT_POLICY_FILE = SCRIPT_DIR / '.state' / 'import-policy.json'
DEFAULT_TARGET_GROUP = None
DEFAULT_SEND_URL = None
DEFAULT_REACT_URL = None
SUMMARY_RE = re.compile(r'(?:总结|總結|summary)', re.I)
LOG_MSG_RE = re.compile(r'^\[(?P<ts>[^\]]+)\] \[LOG\] 文本消息内容: (?P<content>.*)$')
RECEIVED_RE = re.compile(r'^\[(?P<ts>[^\]]+)\] 收到消息，.*?msgId: (?P<msg_id>[^,]+)')

sys.path.insert(0, str(LEGACY_DIR))
from auto_import_latest_log import (  # noqa: E402
    latest_log_file,
    load_policy,
    load_state as load_import_state,
    parse_rows_from_log,
    parse_start_time,
    row_fingerprint,
    save_state as save_import_state,
    merge_same_work_rows,
)
from log_to_feishu import rows_to_csv  # noqa: E402
from feishu_bitable_import import get_tenant_access_token, parse_csv_text, upload_records  # noqa: E402


def load_dotenv(path: Path):
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return dict(default)


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def latest_log_or_none(log_dir: Path):
    try:
        return latest_log_file(log_dir)
    except SystemExit:
        return None


def read_new_bytes(path: Path, state: dict):
    key = str(path)
    pos = int(state.get('log_offsets', {}).get(key, 0))
    size = path.stat().st_size
    if size < pos:
        pos = 0
    with path.open('r', encoding='utf-8', errors='replace') as f:
        f.seek(pos)
        data = f.read()
        new_pos = f.tell()
    state.setdefault('log_offsets', {})[key] = new_pos
    return data


def find_summary_trigger(text: str) -> tuple[str | None, str | None]:
    current_msg_id = None
    for line in text.splitlines():
        received = RECEIVED_RE.match(line)
        if received:
            msg_id = received.group('msg_id').strip()
            current_msg_id = msg_id if msg_id and msg_id.lower() != 'undefined' else None
            continue
        m = LOG_MSG_RE.match(line)
        if m:
            content = m.group('content').strip()
            if SUMMARY_RE.search(content):
                return current_msg_id, content
    return None, None


def find_summary_trigger_message_id(text: str) -> str | None:
    msg_id, _ = find_summary_trigger(text)
    return msg_id


def contains_summary_trigger(text: str) -> bool:
    _, content = find_summary_trigger(text)
    return content is not None


def import_new_rows(log_file: Path, import_state_file: Path, policy_file: Path, dry_run=False):
    import_start = time.monotonic()
    import_state = load_import_state(import_state_file)
    policy = load_policy(policy_file)
    start_time = parse_start_time(import_state.get('startTime'))
    rows = parse_rows_from_log(log_file, start_time=start_time, policy=policy)
    imported = set(import_state.get('imported', []))
    new_rows = [row for row in rows if row_fingerprint(row) not in imported]
    print(f'Feishu import parsed rows={len(rows)} new_rows={len(new_rows)} elapsed={time.monotonic() - import_start:.2f}s', flush=True)
    if not new_rows:
        return 0, rows, []
    if not dry_run:
        csv_text = rows_to_csv(new_rows)
        records = parse_csv_text(csv_text)
        token_start = time.monotonic()
        print('Feishu token start', flush=True)
        token = get_tenant_access_token()
        print(f'Feishu token done elapsed={time.monotonic() - token_start:.2f}s', flush=True)
        upload_start = time.monotonic()
        created = upload_records(records, token)
        print(f'Feishu upload_records done created={created} elapsed={time.monotonic() - upload_start:.2f}s', flush=True)
        imported.update(row_fingerprint(r) for r in new_rows)
        import_state['imported'] = sorted(imported)
        import_state['lastLog'] = log_file.name
        save_import_state(import_state_file, import_state)
        print(f'Feishu import done total_elapsed={time.monotonic() - import_start:.2f}s', flush=True)
        return created, rows, new_rows
    return len(new_rows), rows, new_rows


def floor_sort_key(floor: str):
    s = str(floor or '未標明樓層')
    nums = re.findall(r'\d+', s)
    return (int(nums[0]) if nums else 9999, s)


def display_record_date(value: str | None) -> str:
    raw = str(value or '').strip()
    if not raw or raw.lower() == 'null':
        return '未標明日期'
    m = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', raw)
    if m:
        d, mo, y = m.groups()
        return f'{int(d):02d}/{int(mo):02d}/{y}'
    return raw


def date_sort_key(value: str):
    m = re.fullmatch(r'(\d{2})/(\d{2})/(\d{4})', value)
    if m:
        d, mo, y = m.groups()
        return (int(y), int(mo), int(d), value)
    return (9999, 99, 99, value)


def parse_requested_summary_date(text: str | None) -> str | None:
    text = str(text or '')
    patterns = [
        r'(20\d{2})[/-](\d{1,2})[/-](\d{1,2})',
        r'(\d{1,2})[/-](\d{1,2})[/-](20\d{2})',
        r'(\d{1,2})/(\d{1,2})(?!/\d)',
        r'(\d{1,2})月(\d{1,2})日',
    ]
    m = re.search(patterns[0], text)
    if m:
        y, mo, d = m.groups()
        return f'{int(d):02d}/{int(mo):02d}/{y}'
    m = re.search(patterns[1], text)
    if m:
        d, mo, y = m.groups()
        return f'{int(d):02d}/{int(mo):02d}/{y}'
    m = re.search(patterns[2], text)
    if m:
        d, mo = m.groups()
        return f'{int(d):02d}/{int(mo):02d}/2026'
    m = re.search(patterns[3], text)
    if m:
        mo, d = m.groups()
        return f'{int(d):02d}/{int(mo):02d}/2026'
    return None


def build_summary(rows: list[dict], requested_date: str | None = None) -> str:
    if not rows:
        return '今日無工地記錄。'

    by_date = {}
    for r in rows:
        date_label = display_record_date(r.get('日期'))
        if requested_date and date_label != requested_date:
            continue
        building = (r.get('樓棟') or '未標明樓棟').strip() or '未標明樓棟'
        floor = (r.get('樓層') or '未標明樓層').strip() or '未標明樓層'
        by_date.setdefault(date_label, {}).setdefault(building, {}).setdefault(floor, []).append(r)

    if requested_date and not by_date:
        return f'{requested_date}\n今日無工地記錄。'

    blocks = []
    for date_label in sorted(by_date, key=date_sort_key):
        blocks.append(date_label)
        grouped = by_date[date_label]
        for building in sorted(grouped):
            blocks.append(f'*✅{building}*')
            floors = grouped[building]
            for floor in sorted(floors, key=floor_sort_key):
                blocks.append(floor)
                for r in floors[floor]:
                    contractor = (r.get('分判') or '未標明分判').strip()
                    count = (r.get('人數') or 'null').strip()
                    task = (r.get('工序') or '').strip()
                    zone = (r.get('分區') or '').strip()
                    task_text = f'{zone} {task}'.strip() if zone and zone.lower() != 'null' else task
                    count_text = f'{count}人' if count.isdigit() else ''
                    blocks.append(f'{contractor}：{count_text} {task_text}'.rstrip())
                blocks.append('')
        blocks.append('')
    return '\n'.join(blocks).strip()


def send_reaction(message_id: str | None, emoji: str, react_url: str, dry_run=False):
    if not message_id:
        return
    payload = {'messageId': message_id, 'emoji': emoji}
    if dry_run:
        print('REACTION DRY RUN:')
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    import urllib.request
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(react_url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    secret = os.environ.get('SEND_API_SECRET')
    if secret:
        req.add_header('x-api-secret', secret)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode('utf-8')
    try:
        result = json.loads(body)
    except Exception:
        result = {'raw': body}
    if not result.get('ok'):
        raise RuntimeError(f'Reaction failed: {result}')


def send_whatsapp(to: str, message: str, send_url: str, dry_run=False):
    payload = {'to': to, 'message': message}
    if dry_run:
        print('SEND WHATSAPP DRY RUN:')
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    import urllib.request
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(send_url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    secret = os.environ.get('SEND_API_SECRET')
    if secret:
        req.add_header('x-api-secret', secret)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode('utf-8')
    try:
        result = json.loads(body)
    except Exception:
        result = {'raw': body}
    if not result.get('ok'):
        raise RuntimeError(f'WhatsApp send failed: {result}')


def parse_rows_for_summary(log_file: Path, import_state_file: Path, policy_file: Path) -> list[dict]:
    import_state = load_import_state(import_state_file)
    policy = load_policy(policy_file)
    start_time = parse_start_time(import_state.get('startTime'))
    return parse_rows_from_log(log_file, start_time=start_time, policy=policy)


DAILY_TABLE_STATE = {
    'date': None,
    'table_id': None,
}


DAILY_TABLE_FIELDS = [
    {'field_name': '發布用戶', 'type': 1, 'is_primary': True},
    {'field_name': '發送時間', 'type': 1},
    {'field_name': '日期', 'type': 1},
    {'field_name': '分區', 'type': 1},
    {'field_name': '樓棟', 'type': 3, 'property': {'options': [
        {'name': 'A座'}, {'name': 'B座'}, {'name': 'C座'}, {'name': 'null'},
    ]}},
    {'field_name': '樓層', 'type': 1},
    {'field_name': '分判', 'type': 3, 'property': {'options': [
        {'name': '偉健'}, {'name': '利安'}, {'name': '駿慶'}, {'name': '遠東德鴻'}, {'name': '萬利'},
        {'name': '新豪'}, {'name': '鉅城'}, {'name': '永興'}, {'name': '美時'}, {'name': '健力'},
        {'name': '日麗雅'}, {'name': '陳橋'}, {'name': '順利'}, {'name': '康和'}, {'name': '萬通'},
        {'name': '建安'}, {'name': '仙壁'}, {'name': '恆昇'}, {'name': '藝薪'}, {'name': '浩洲'},
        {'name': '捷信'}, {'name': '億雄'}, {'name': '創豐'}, {'name': '秦深記'}, {'name': '好標準'},
        {'name': 'null'},
    ]}},
    {'field_name': '工序', 'type': 1},
    {'field_name': '人數', 'type': 1},
    {'field_name': '原始消息', 'type': 1},
]


def request_feishu_json(url: str, token: str, payload: dict | None = None, method: str | None = None):
    import urllib.request
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method or ('POST' if payload is not None else 'GET'))
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def list_feishu_tables(app_token: str, token: str) -> list[dict]:
    tables = []
    page_token = None
    base = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables'
    while True:
        url = base + '?page_size=100'
        if page_token:
            url += '&page_token=' + page_token
        result = request_feishu_json(url, token)
        if result.get('code') != 0:
            raise RuntimeError(f'list tables failed: {result}')
        data = result.get('data', {})
        tables.extend(data.get('items', []))
        if not data.get('has_more'):
            break
        page_token = data.get('page_token')
    return tables


def ensure_daily_table() -> str:
    """Ensure today's table exists and point FEISHU_BITABLE_TABLE_ID to it."""
    today = date.today().isoformat()
    if DAILY_TABLE_STATE.get('date') == today and DAILY_TABLE_STATE.get('table_id'):
        os.environ['FEISHU_BITABLE_TABLE_ID'] = DAILY_TABLE_STATE['table_id']
        return DAILY_TABLE_STATE['table_id']

    app_token = os.environ['FEISHU_BITABLE_APP_TOKEN']
    token = get_tenant_access_token()
    table_name = today
    tables = list_feishu_tables(app_token, token)
    for table in tables:
        name = table.get('name') or table.get('table_name')
        if name == table_name:
            table_id = table.get('table_id')
            os.environ['FEISHU_BITABLE_TABLE_ID'] = table_id
            DAILY_TABLE_STATE.update({'date': today, 'table_id': table_id})
            print(f'Using existing Feishu daily table: {table_name} {table_id}', flush=True)
            return table_id

    base = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables'
    created = request_feishu_json(base, token, {'table': {'name': table_name}})
    if created.get('code') != 0:
        raise RuntimeError(f'create daily table failed: {created}')
    table_id = created.get('data', {}).get('table_id')
    if not table_id:
        raise RuntimeError(f'create daily table returned no table_id: {created}')

    fields_url = f'{base}/{table_id}/fields'
    existing = request_feishu_json(fields_url + '?page_size=100', token)
    existing_names = {item.get('field_name') for item in existing.get('data', {}).get('items', [])} if existing.get('code') == 0 else set()
    for field in DAILY_TABLE_FIELDS:
        if field['field_name'] in existing_names:
            continue
        payload = {'field_name': field['field_name'], 'type': field['type']}
        if field.get('is_primary'):
            payload['is_primary'] = True
        if field.get('property'):
            payload['property'] = field['property']
        result = request_feishu_json(fields_url, token, payload)
        if result.get('code') != 0:
            print(f'WARN: create field failed {field["field_name"]}: {result}', flush=True)

    os.environ['FEISHU_BITABLE_TABLE_ID'] = table_id
    DAILY_TABLE_STATE.update({'date': today, 'table_id': table_id})
    print(f'Created Feishu daily table: {table_name} {table_id}', flush=True)
    return table_id


def list_feishu_records(app_token: str, table_id: str, token: str) -> list[dict]:
    records = []
    page_token = None
    base = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records'
    while True:
        url = base + '?page_size=500'
        if page_token:
            url += '&page_token=' + page_token
        result = request_feishu_json(url, token)
        if result.get('code') != 0:
            raise RuntimeError(f'list records failed: {result}')
        data = result.get('data', {})
        records.extend(data.get('items', []))
        if not data.get('has_more'):
            break
        page_token = data.get('page_token')
    return records


def parse_rows_for_summary_from_feishu(requested_date: str | None = None) -> list[dict]:
    table_id = ensure_daily_table()
    app_token = os.environ['FEISHU_BITABLE_APP_TOKEN']
    token = get_tenant_access_token()
    items = list_feishu_records(app_token, table_id, token)
    rows = []
    for item in items:
        fields = item.get('fields') or {}
        row = {k: str(fields.get(k, 'null') if fields.get(k, '') != '' else 'null') for k in [
            '發布用戶', '發送時間', '日期', '分區', '樓棟', '樓層', '分判', '工序', '人數', '原始消息'
        ]}
        if requested_date and display_record_date(row.get('日期')) != requested_date:
            continue
        rows.append(row)
    return merge_same_work_rows(rows)


def feishu_import_worker(log_file: Path, args):
    try:
        ensure_daily_table()
        print(f'Feishu import worker start for {log_file.name} table={os.environ.get("FEISHU_BITABLE_TABLE_ID")}', flush=True)
        created, rows, new_rows = import_new_rows(log_file, Path(args.import_state_file), Path(args.policy_file), dry_run=args.dry_run)
        if created:
            print(f'Imported {created} new rows from {log_file.name}', flush=True)
        print(f'Feishu import worker done for {log_file.name}', flush=True)
    except Exception as e:
        print(f'ERROR: Feishu import worker failed for {log_file.name}: {e}', file=sys.stderr, flush=True)


def run_once(args, service_state: dict):
    log_file = latest_log_or_none(Path(args.log_dir))
    if not log_file:
        print(f'No log files found in {args.log_dir}', flush=True)
        return service_state

    new_text = read_new_bytes(log_file, service_state)
    service_state['last_log'] = str(log_file)

    # Summary trigger must stay responsive even if Feishu import is slow/stuck.
    summary_triggered = False
    if new_text and contains_summary_trigger(new_text):
        summary_triggered = True
        trigger_msg_id, trigger_text = find_summary_trigger(new_text)
        requested_date = parse_requested_summary_date(trigger_text)
        print(f'Summary trigger detected in {log_file.name}: {trigger_msg_id or "no-msg-id"} requested_date={requested_date or "all"}', flush=True)
        send_reaction(trigger_msg_id, '👀', args.react_url, dry_run=args.dry_run)
        try:
            rows = parse_rows_for_summary_from_feishu(requested_date=requested_date)
            print(f'Summary loaded {len(rows)} rows from Feishu table', flush=True)
        except Exception as e:
            print(f'WARN: summary read from Feishu failed, fallback to log parse: {e}', flush=True)
            rows = parse_rows_for_summary(log_file, Path(args.import_state_file), Path(args.policy_file))
        summary = build_summary(rows, requested_date=requested_date)
        send_whatsapp(args.target_group, summary, args.send_url, dry_run=args.dry_run)
        send_reaction(trigger_msg_id, '✅', args.react_url, dry_run=args.dry_run)
        print(f'Sent WhatsApp summary to {args.target_group}', flush=True)

    # A pure summary trigger should not start Feishu import; otherwise the importer
    # reparses old log content around the trigger and can duplicate historical rows.
    if new_text and summary_triggered:
        print('Skipping Feishu import for summary trigger batch', flush=True)
    elif new_text and not service_state.get('feishu_import_running'):
        print(f'Scheduling Feishu import for {log_file.name}', flush=True)
        service_state['feishu_import_running'] = True
        service_state['feishu_import_log'] = str(log_file)
        worker_args = args
        def _run_worker():
            try:
                feishu_import_worker(log_file, worker_args)
            finally:
                service_state['feishu_import_running'] = False
                service_state.pop('feishu_import_log', None)
        threading.Thread(target=_run_worker, name='feishu-import-worker', daemon=True).start()
    elif new_text:
        print(f'Feishu import already running for {service_state.get("feishu_import_log")}', flush=True)
    return service_state


def env_value(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f'Missing required environment variable: {name}')
    return value


def main():
    load_dotenv(DEFAULT_ENV_FILE)
    p = argparse.ArgumentParser(description='Engineer info structured long-running hook: import Feishu and send WhatsApp summary on 總結')
    p.add_argument('--log-dir', default=env_value('ENGINEER_LOG_DIR', DEFAULT_LOG_DIR, required=True))
    p.add_argument('--env-file', default=str(DEFAULT_ENV_FILE))
    p.add_argument('--state-file', default=env_value('ENGINEER_STATE_FILE', str(DEFAULT_STATE_FILE)))
    p.add_argument('--import-state-file', default=env_value('ENGINEER_IMPORT_STATE_FILE', str(DEFAULT_IMPORT_STATE_FILE)))
    p.add_argument('--policy-file', default=env_value('ENGINEER_POLICY_FILE', str(DEFAULT_POLICY_FILE)))
    p.add_argument('--target-group', default=env_value('ENGINEER_TARGET_GROUP', DEFAULT_TARGET_GROUP, required=True))
    p.add_argument('--send-url', default=env_value('ENGINEER_SEND_URL', DEFAULT_SEND_URL, required=True))
    p.add_argument('--react-url', default=env_value('ENGINEER_REACT_URL', DEFAULT_REACT_URL, required=True))
    p.add_argument('--interval', type=float, default=5.0)
    p.add_argument('--once', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    if Path(args.env_file) != DEFAULT_ENV_FILE:
        load_dotenv(Path(args.env_file))
    state_path = Path(args.state_file)
    state = load_json(state_path, {'log_offsets': {}})
    ensure_daily_table()

    while True:
        try:
            state = run_once(args, state)
            persisted_state = dict(state)
            # Runtime thread flags are process-local; do not persist them across restarts.
            persisted_state.pop('feishu_import_running', None)
            persisted_state.pop('feishu_import_log', None)
            save_json(state_path, persisted_state)
        except Exception as e:
            print(f'ERROR: {e}', file=sys.stderr, flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
