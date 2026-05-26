#!/usr/bin/env python3
"""Send today's summary/reports to the configured WhatsApp target group."""
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engineer_hook_service as svc  # noqa: E402


def main():
    svc.load_dotenv(ROOT / '.env')
    report_date = date.today().strftime('%d/%m/%Y')
    target = os.environ['ENGINEER_TARGET_GROUP']
    send_url = os.environ['ENGINEER_SEND_URL']
    report_dir = os.environ.get('ENGINEER_REPORT_DIR')
    keyword_xlsx = os.environ.get('ENGINEER_PROCESS_KEYWORD_XLSX')

    rows = svc.parse_rows_for_summary_from_feishu(requested_date=report_date, filter_record_date=False)
    print(f'Daily summary {report_date}: rows={len(rows)} target={target}', flush=True)

    # 1) A/B/C/Null text summaries.
    for summary in svc.build_summary_messages(rows, requested_date=None):
        svc.send_whatsapp(target, summary, send_url)
        time.sleep(0.2)

    # 2) Floor detail Feishu table + PDF file.
    table_name, table_id, detail_count = svc.update_floor_detail_table(rows, report_date)
    floor_png = svc.render_floor_detail_report(rows, report_date, report_dir)
    floor_pdf = svc.png_to_pdf(floor_png)
    svc.send_whatsapp_file(target, floor_pdf, send_url, filename=floor_pdf.name, caption=f'樓層明細表 {report_date}')
    print(f'Updated/sent {table_name} {table_id} rows={detail_count}', flush=True)

    # 3) Process headcount Feishu table + text summary + PDF file.
    process_rows = svc.aggregate_process_headcount(rows, report_date, keyword_xlsx, filter_record_date=False)
    process_table_name = '工序人數表-' + (svc.table_name_from_display_date(report_date) or date.today().isoformat())
    process_table_id = svc.ensure_named_feishu_table(process_table_name, svc.PROCESS_TABLE_FIELDS)
    svc.replace_feishu_records(process_table_id, [{**r, '日期': report_date} for r in process_rows], numeric_fields=set())
    _, process_png = svc.render_process_report(process_rows, report_date, report_dir)
    process_pdf = svc.png_to_pdf(process_png)
    svc.send_whatsapp(target, svc.build_process_text_summary(process_rows, report_date), send_url)
    svc.send_whatsapp_file(target, process_pdf, send_url, filename=process_pdf.name, caption=f'工序人數表 {report_date}')
    print(f'Updated/sent {process_table_name} {process_table_id} rows={len(process_rows)}', flush=True)


if __name__ == '__main__':
    main()
