#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from log_to_feishu import split_segments, parse_segment, rows_to_csv
from extract_engineering_messages import extract_messages, looks_like_engineering_message
from feishu_bitable_import import get_tenant_access_token, parse_csv_text, upload_records, CSV_COLUMNS

DEFAULT_LOG_DIR = Path('/home/claw/workspace/insp-bot/logs/120363425741086960@g.us')
DEFAULT_STATE = Path('/home/claw/.openclaw/workspace-engineer-info-structured/.openclaw/feishu-import-state.json')
DEFAULT_POLICY = Path('/home/claw/.openclaw/workspace-engineer-info-structured/.openclaw/import-policy.json')


def row_fingerprint(row: dict) -> str:
    base = '||'.join(str(row.get(k, '')) for k in CSV_COLUMNS)
    return hashlib.sha256(base.encode('utf-8')).hexdigest()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"imported": [], "lastLog": None}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {"imported": [], "lastLog": None}


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def latest_log_file(log_dir: Path) -> Path:
    files = sorted(log_dir.glob('*.log'))
    if not files:
        raise SystemExit(f'No log files found in {log_dir}')
    return files[-1]


def load_policy(path: Path) -> dict:
    if not path.exists():
        return {"mode": "all_engineering", "trustedSenderMatchers": [], "fallbackRawImport": True}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise SystemExit(f'Invalid import policy {path}: {exc}')


def trusted_sender_match(msg: dict, policy: dict) -> bool:
    matchers = [str(x).lower() for x in policy.get('trustedSenderMatchers', []) if str(x).strip()]
    if not matchers:
        return True
    haystack = ' '.join([
        str(msg.get('sender', '')),
        str(msg.get('sender_number', '')),
        str(msg.get('text', '')[:300]),
    ]).lower()
    return any(m in haystack for m in matchers)


def normalize_structured_text(text: str) -> str:
    text = text.replace('—', ' ')
    text = text.replace('–', '-')
    text = text.replace('：', ' ')
    text = re.sub(r'[（）()]', ' ', text)
    return text


def parse_start_time(value: str | None):
    if not value:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise SystemExit(f'Invalid startTime format: {value}')


def parse_rows_from_log(log_file: Path, start_time=None, policy=None) -> list[dict]:
    policy = policy or {"mode": "all_engineering", "fallbackRawImport": True}
    trusted_only = policy.get('mode') == 'trusted_structured_sender_only'
    messages = extract_messages(log_file)
    rows = []
    seen = set()
    skipped_candidates = 0
    for msg in messages:
        if start_time:
            try:
                msg_time = datetime.strptime(msg.get('log_ts', ''), '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
            if msg_time < start_time:
                continue
        if not looks_like_engineering_message(msg['text']):
            continue
        if trusted_only and not trusted_sender_match(msg, policy):
            skipped_candidates += 1
            continue
        parse_text = normalize_structured_text(msg['text']) if trusted_only else msg['text']
        sender_label = policy.get('trustedSenderLabel') if trusted_only else msg.get('sender', 'null')
        for seg in split_segments(parse_text, sender_label or msg.get('sender', 'null'), msg.get('log_ts', 'null')):
            for row in parse_segment(seg):
                fp = row_fingerprint(row)
                if fp in seen:
                    continue
                seen.add(fp)
                rows.append(row)
    if skipped_candidates:
        state_note_path = DEFAULT_STATE.parent / 'auto-import-skipped.log'
        with state_note_path.open('a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%F %T')}] skipped {skipped_candidates} non-trusted engineering candidate messages from {log_file.name}\n")
    return rows


def main():
    parser = argparse.ArgumentParser(description='Auto import new engineering rows from latest insp-bot log into Feishu')
    parser.add_argument('--log-dir', default=str(DEFAULT_LOG_DIR))
    parser.add_argument('--state-file', default=str(DEFAULT_STATE))
    parser.add_argument('--policy-file', default=str(DEFAULT_POLICY))
    parser.add_argument('--csv-out', help='Optional CSV snapshot output path')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    state_path = Path(args.state_file)
    log_file = latest_log_file(log_dir)

    state = load_state(state_path)
    policy = load_policy(Path(args.policy_file))
    start_time = parse_start_time(state.get('startTime'))
    rows = parse_rows_from_log(log_file, start_time=start_time, policy=policy)
    imported = set(state.get('imported', []))
    new_rows = [row for row in rows if row_fingerprint(row) not in imported]

    if args.csv_out:
        csv_text = rows_to_csv(new_rows if new_rows else []) if new_rows else ','.join(CSV_COLUMNS) + '\n'
        Path(args.csv_out).write_text(csv_text, encoding='utf-8')

    if args.dry_run:
        print(json.dumps({
            'log_file': str(log_file),
            'parsed_rows': len(rows),
            'new_rows': len(new_rows)
        }, ensure_ascii=False, indent=2))
        return

    if not new_rows:
        print(f'No new engineering rows to import from {log_file.name}')
        return

    csv_text = rows_to_csv(new_rows)
    token = get_tenant_access_token()
    records = parse_csv_text(csv_text)
    created = upload_records(records, token)

    imported.update(row_fingerprint(r) for r in new_rows)
    state['imported'] = sorted(imported)
    state['lastLog'] = log_file.name
    save_state(state_path, state)
    print(f'Imported {created} new rows from {log_file.name}')


if __name__ == '__main__':
    main()
