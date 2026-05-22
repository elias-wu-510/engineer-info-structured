import csv
import io
import json
import os
import urllib.request

from feishu_bitable_import import CSV_COLUMNS

SYSTEM_PROMPT = """你是工程施工報工信息結構化助手。
你只輸出 CSV，不要解釋。
CSV 表頭必須嚴格為：發布用戶,發送人號碼,發送時間,日期,分區,樓棟,樓層,分判,工種,工序,人數,原始消息
如果沒有有效工程記錄，輸出 exactly: 无有效记录
規則：
- 日期統一 DD/MM/YYYY；如果只有日/月，默認年份 2026。
- 樓棟統一 A座/B座/C座。
- 樓層保留原工程樓層，如 1F、1/F、G/F、M/F；多樓層可用中文逗號連接。
- 分區包括 zone、ST、CP、方向、外牆、grid/range 等，例如 CP7-8 必須放入分區，不要放入分判。
- 分判必須是中文公司/隊伍名，例如 新豪、鉅城、駿慶、永興、偉健、順利、康和等；不要把 zone、樓層、ST01、CP7-8、B5 等當分判。
- 如果分判/隊伍描述中出現男工或女工，將男工/女工填入工種，分判中移除男工/女工；否則工種填 null。
- 工序是實際工作內容，例如 打石矢、core 窿、維修臨時lift膽門、燒焊。
- 人數只輸出數字；缺失用 null。
- 缺失字段用 null。
- 發送人號碼填入用戶提供的發送人號碼；如果未提供，用 null。
- 原始消息填入完整原文。
- 一條消息中多個樓層/分判/工序/人數要拆成多行。
- 如果提供了規則解析結果，只把它當參考；必須以原始消息為準修正錯亂字段。
"""


def _api_keys() -> list[str]:
    raw = os.getenv("LLM_API_KEYS") or os.getenv("LLM_API_KEY") or ""
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


def enabled() -> bool:
    return bool(os.getenv("LLM_API_BASE_URL") and _api_keys() and os.getenv("LLM_MODEL"))


def mode() -> str:
    # fallback: only when rules return no rows; review: always review/correct rule rows.
    return (os.getenv("LLM_MODE") or os.getenv("LLM_PARSE_MODE") or "fallback").strip().lower()


def _request_llm(prompt: str) -> str:
    payload = {
        "model": os.environ["LLM_MODEL"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = None
    keys = _api_keys()
    for idx, api_key in enumerate(keys, start=1):
        req = urllib.request.Request(
            os.environ["LLM_API_BASE_URL"],
            data=body,
            method="POST",
        )
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            return msg.get("content") or ""
        except Exception as exc:
            last_error = exc
            print(f"WARN: LLM key #{idx} failed, trying next key" if idx < len(keys) else f"WARN: LLM key #{idx} failed", flush=True)
    if last_error:
        raise last_error
    return ""


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_csv_output(text: str) -> list[dict]:
    text = _strip_fence(text)
    if not text or text.strip() == "无有效记录":
        return []
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != CSV_COLUMNS:
        raise ValueError(f"LLM CSV header mismatch: {reader.fieldnames}")
    rows = []
    for row in reader:
        item = {k: (row.get(k, "") or "").strip() for k in CSV_COLUMNS}
        if not any(item.values()):
            continue
        if not item["人數"]:
            item["人數"] = "null"
        rows.append(item)
    return rows


def rows_to_csv_for_prompt(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for row in rows or []:
        writer.writerow({k: row.get(k, "null") for k in CSV_COLUMNS})
    return buf.getvalue().strip()


def parse_message(text: str, sender: str, sent_time: str, rule_rows: list[dict] | None = None, sender_number: str = "null") -> list[dict]:
    if not enabled():
        return []
    rule_csv = rows_to_csv_for_prompt(rule_rows or []) if rule_rows else "无"
    prompt = f"""請將以下 WhatsApp 工程報工消息結構化為 CSV。
發布用戶：{sender}
發送人號碼：{sender_number}
發送時間：{sent_time}

原始消息：
{text}

規則解析結果（可能有錯，只作參考）：
{rule_csv}

請輸出修正後的最終 CSV。"""
    output = _request_llm(prompt)
    return parse_csv_output(output)
