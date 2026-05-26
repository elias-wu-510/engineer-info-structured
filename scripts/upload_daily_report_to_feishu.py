#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, mimetypes, os
import requests
from pathlib import Path
from dotenv import load_dotenv

APP_TOKEN='ArDYb6sZtaqu2jsML3IcMe6qncf'
TABLE_ID='tbl1dH9GihZQ8HoC'
FIELDS=[
    {'field_name':'日期','type':1},
    {'field_name':'報表日期','type':1},
    {'field_name':'出入闸总人数','type':2},
    {'field_name':'已匹配人数','type':2},
    {'field_name':'未匹配人数','type':2},
    {'field_name':'匹配记录数','type':2},
    {'field_name':'未匹配记录数','type':2},
    {'field_name':'状态','type':1},
    {'field_name':'Daily Report Excel','type':17},
    {'field_name':'出入閘源文件','type':17},
    {'field_name':'Labour寫入人數','type':1},
    {'field_name':'BS Labour寫入人數','type':1},
    {'field_name':'Staff寫入人數','type':1},
    {'field_name':'未匹配清单','type':17},
    {'field_name':'新增工种人数','type':2},
    {'field_name':'新增工种数','type':2},
    {'field_name':'最终写入人数','type':2},
]

def req_json(url, token=None, payload=None, method=None):
    headers={'Content-Type':'application/json; charset=utf-8'}
    if token: headers['Authorization']='Bearer '+token
    if payload is None and (method or 'GET') == 'GET':
        r=requests.get(url, headers=headers, timeout=60)
    else:
        m=(method or 'POST').upper()
        r=requests.request(m, url, headers=headers, json=payload, timeout=60)
    try:
        return r.json()
    except Exception:
        r.raise_for_status()
        raise

def get_token():
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parents[1]/'.env', override=True)
    return req_json('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal', payload={'app_id':os.environ['FEISHU_APP_ID'],'app_secret':os.environ['FEISHU_APP_SECRET']})['tenant_access_token']

def ensure_fields(token):
    base=f'https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/fields'
    cur=req_json(base+'?page_size=100',token)
    names={i.get('field_name') for i in cur.get('data',{}).get('items',[])}
    for f in FIELDS:
        if f['field_name'] in names: continue
        res=req_json(base,token,{'field_name':f['field_name'],'type':f['type']})
        if res.get('code')!=0:
            raise RuntimeError(f'create field {f["field_name"]} failed: {res}')

def upload_file(token, path):
    path=Path(path)
    data={
        'file_name': path.name,
        'parent_type':'bitable_file',
        'parent_node': APP_TOKEN,
        'size': str(path.stat().st_size),
    }
    ctype=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    with path.open('rb') as f:
        files={'file': (path.name, f, ctype)}
        r=requests.post('https://open.feishu.cn/open-apis/drive/v1/medias/upload_all',
                        headers={'Authorization':'Bearer '+token}, data=data, files=files, timeout=120)
    res=r.json()
    if res.get('code')!=0:
        raise RuntimeError(f'upload file failed {path}: {res}')
    return res['data']['file_token']

def create_record(token, summary, report_file, unmatched_file):
    rf=upload_file(token, report_file)
    uf=upload_file(token, unmatched_file) if unmatched_file else None
    fields={
        '日期': str(summary.get('report_date_header') or ''),
        '報表日期': str(summary.get('report_date') or summary.get('report_date_header') or ''),
        '出入闸总人数': int(summary.get('access_total') or 0),
        '已匹配人数': int(summary.get('matched_total') or 0),
        '未匹配人数': int(summary.get('final_unmatched_total', summary.get('unmatched_total') or 0) or 0),
        '匹配记录数': int(summary.get('matched_record_count') or 0),
        '未匹配记录数': int(summary.get('final_unmatched_record_count', summary.get('unmatched_record_count') or 0) or 0),
        '状态': str(summary.get('status') or ('有未匹配' if int(summary.get('unmatched_total') or 0) else '完成')),
        '新增工种人数': int(summary.get('new_trade_total') or 0),
        '新增工种数': int(summary.get('new_trade_record_count') or 0),
        '最终写入人数': int(summary.get('final_written_total', summary.get('matched_total') or 0) or 0),
        'Daily Report Excel': [{'file_token': rf}],
    }
    if uf: fields['未匹配清单']=[{'file_token':uf}]
    url=f'https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records'
    res=req_json(url,token,{'fields':fields})
    if res.get('code')!=0:
        raise RuntimeError(f'create record failed: {res}')
    return res

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--summary',required=True)
    ap.add_argument('--report-file',required=True)
    ap.add_argument('--unmatched-file')
    args=ap.parse_args()
    token=get_token(); ensure_fields(token)
    summary=json.loads(Path(args.summary).read_text(encoding='utf-8'))
    res=create_record(token,summary,args.report_file,args.unmatched_file)
    print(json.dumps(res,ensure_ascii=False,indent=2))
