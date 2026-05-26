#!/usr/bin/env python3
"""Ensure today's Feishu daily table exists for engineer info structured flow."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engineer_hook_service as svc  # noqa: E402

svc.load_dotenv(ROOT / '.env')
table_id = svc.ensure_daily_table()
print(f'ensured daily table: {table_id}')
