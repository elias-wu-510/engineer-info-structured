#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
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


def find_summary_trigger_message_id(text: str) -> str | None:
    current_msg_id = None
    for line in text.splitlines():
        received = RECEIVED_RE.match(line)
        if received:
            msg_id = received.group('msg_id').strip()
            current_msg_id = msg_id if msg_id and msg_id.lower() != 'undefined' else None
            continue
        m = LOG_MSG_RE.match(line)
        if m and SUMMARY_RE.search(m.group('content').strip()):
            return current_msg_id
    return None


def contains_summary_trigger(text: str) -> bool:
    return find_summary_trigger_message_id(text) is not None or any(
        (m := LOG_MSG_RE.match(line)) and SUMMARY_RE.search(m.group('content').strip())
        for line in text.splitlines()
    )


def import_new_rows(log_file: Path, import_state_file: Path, policy_file: Path, dry_run=False):
    import_state = load_import_state(import_state_file)
    policy = load_policy(policy_file)
    start_time = parse_start_time(import_state.get('startTime'))
    rows = parse_rows_from_log(log_file, start_time=start_time, policy=policy)
    imported = set(import_state.get('imported', []))
    new_rows = [row for row in rows if row_fingerprint(row) not in imported]
    if not new_rows:
        return 0, rows, []
    if not dry_run:
        csv_text = rows_to_csv(new_rows)
        token = get_tenant_access_token()
        records = parse_csv_text(csv_text)
        created = upload_records(records, token)
        imported.update(row_fingerprint(r) for r in new_rows)
        import_state['imported'] = sorted(imported)
        import_state['lastLog'] = log_file.name
        save_import_state(import_state_file, import_state)
        return created, rows, new_rows
    return len(new_rows), rows, new_rows


def floor_sort_key(floor: str):
    s = str(floor or '未標明樓層')
    nums = re.findall(r'\d+', s)
    return (int(nums[0]) if nums else 9999, s)


def build_summary(rows: list[dict]) -> str:
    if not rows:
        return '今日無工地記錄。'
    grouped = {}
    for r in rows:
        building = (r.get('樓棟') or '未標明樓棟').strip() or '未標明樓棟'
        floor = (r.get('樓層') or '未標明樓層').strip() or '未標明樓層'
        grouped.setdefault(building, {}).setdefault(floor, []).append(r)
    blocks = []
    for building in sorted(grouped):
        blocks.append(building)
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


def run_once(args, service_state: dict):
    log_file = latest_log_or_none(Path(args.log_dir))
    if not log_file:
        print(f'No log files found in {args.log_dir}', flush=True)
        return service_state

    new_text = read_new_bytes(log_file, service_state)
    service_state['last_log'] = str(log_file)

    # Summary trigger must stay responsive even if Feishu import is slow/stuck.
    if new_text and contains_summary_trigger(new_text):
        trigger_msg_id = find_summary_trigger_message_id(new_text)
        print(f'Summary trigger detected in {log_file.name}: {trigger_msg_id or "no-msg-id"}', flush=True)
        send_reaction(trigger_msg_id, '👀', args.react_url, dry_run=args.dry_run)
        rows = parse_rows_for_summary(log_file, Path(args.import_state_file), Path(args.policy_file))
        summary = build_summary(rows)
        send_whatsapp(args.target_group, summary, args.send_url, dry_run=args.dry_run)
        send_reaction(trigger_msg_id, '✅', args.react_url, dry_run=args.dry_run)
        print(f'Sent WhatsApp summary to {args.target_group}', flush=True)

    print(f'Checking Feishu import for {log_file.name}', flush=True)
    created, rows, new_rows = import_new_rows(log_file, Path(args.import_state_file), Path(args.policy_file), dry_run=args.dry_run)
    if created:
        print(f'Imported {created} new rows from {log_file.name}', flush=True)
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

    while True:
        try:
            state = run_once(args, state)
            save_json(state_path, state)
        except Exception as e:
            print(f'ERROR: {e}', file=sys.stderr, flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
