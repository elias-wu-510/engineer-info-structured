#!/usr/bin/env python3
import argparse
import csv
import io
import re
from pathlib import Path

from extract_engineering_messages import extract_messages, looks_like_engineering_message
from feishu_bitable_import import CSV_COLUMNS, get_tenant_access_token, upload_records, parse_csv_text

DATE_RE = re.compile(r"(\d{4}[/-]\d{1,2}[/-]\d{1,2}(?:\s*星期[一二三四五六日天])?|(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}/\d{1,2})(?:[（(][^）)]*[）)])?|\d{1,2}月\d{1,2}日)")
BUILDING_RE = re.compile(r"(Block\s*[A-Za-z]+|Blk\s*[A-Za-z]+|[A-Za-z]座|[A-Za-z]棟)", re.I)
FLOOR_RE = re.compile(r"(\d+樓及\d+樓|\d+[-‑–—](?:\d+|[A-Za-z]+)樓|(?:\d+~\d+/[Ff]|\d+(?:,\d+)+/[Ff]|\d+[Ff])|(?:(?:\d+|[A-Za-z]+|MR|UP)/[Ff])(?:至(?:\d+|[A-Za-z]+|MR|UP)/[Ff])?(?:及(?:\d+|[A-Za-z]+|MR|UP)/[Ff])?|\d+樓|[A-Za-z]摟|[A-Za-z]樓|M/[Ff]|m/[Ff]|MR/[Ff]|UP/[Ff])")
ZONE_INLINE_RE = re.compile(r"([Zz]one\s*\d+(?:[-‑–—]\d+)?[A-Za-z]?(?:\s*(?:&|＆|/|、|,|，)\s*(?:[Zz]one\s*)?\d+(?:[-‑–—]\d+)?[A-Za-z]?)*|[東西南北]{1,2}面\s+[A-Z]{2}\d+[-‑–—]\d+|[A-Z]\d{1,2}~\d{1,2}/[A-Z]{1,2}|[東西南北]{1,2}面\s+[A-Z]\d{1,2}|西面及北面|東面及北面|西面及南面|東面及南面|東北面|西北面|東南面|西南面|[東西南北]{1,2}面|近[A-Z0-9]+至[A-Z0-9]+向(?:siteB|B座)|[A-Z][0-9]+至[A-Z][0-9]+向siteB|[A-Z]{2}\d+[-‑–—]\d+|[A-Z]\d{1,2}~\d{1,2}/[A-Z]{1,2}(?:\s*~\s*[A-Z]{1,2})?|[A-Z]\d{1,2}[-‑–—]\d{2,3}[A-Za-z]?|[A-Za-z]牛房|[A-Z]區|全場|lift機房)")
HEADCOUNT_RE = re.compile(r"[（(]?(\d+)人[）)]?")
SEGMENT_HEADER_RE = re.compile(r"^\[(?P<ts>\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}:\d{2})\]\s*(?P<user>.*?):\s*(?P<body>.*)$")
SEGMENT_START_RE = re.compile(r"^\[\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}:\d{2}\]\s*.*?:")
NON_WORK_PREFIXES = ("收到消息", "[DEBUG", "[LOG]")
IGNORE_TASK_HINTS = ("執狗臂架",)
CONTRACTOR_HEADING_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9·•\-~  ]{1,20}[:：]?$")
KNOWN_CONTRACTORS = [
    "陳橋", "藝薪", "藝新", "日麗雅", "明泰", "順利", "萬通", "偉健", "利安", "秦深记", "美時",
    "中機電", "遠東德鴻", "德鴻", "德鸿", "遠東億雄", "怡和", "怡和（JEC)", "怡和（JEC)", "駿興", "福明", "東洋", "鴻溢", "中富", "捷信", "駿慶", "萬利", "力成", "仙壁", "康和", "恆昇", "恒記",
    "浩洲", "創豐", "新豪", "鉅城", "永興", "榮豐", "好標準", "中建", "長樂", "京臻", "永輝", "安全外勞", "安全外",
]

INVALID_CONTRACTOR_VALUES = {
    "東", "西", "南", "北", "東面", "西面", "南面", "北面",
    "東北面", "西北面", "東南面", "西南面", "Ab橋頭", "AB橋頭", "樓內", "執", "落", "搭棚", "打", "外租場",
}

KNOWN_TASKS = [
    "組裝橋料", "裝燈喉", "打爆版石矢", "安裝Drywall", "BS Opening吊板", "BS opening 吊板", "鑽窿", "鏟地台+清理", "鏟地台", "剷地台", "炮尾", "石矢", "墨斗", "跟炮尾清泥頭", "清場", "磚牆",
    "公眾位出泥柱", "公眾位包角", "產地台", "跟炮尾", "磚牆釘網", "磚牆包角",
    "出泥柱", "砌磚", "批幼料", "砌磚牆", "牆身釘網", "大機房噴油漆",
    "裝線槽", "PD裝喉", "線坑批蘯", "mark位 裝燈喉", "天花過面", "HR種鐵", "地台出餅仔",
    "洗地", "扶手電梯位砌磚", "地台轉吼", "泵水", "開料", "裝喉", "燈喉",
    "釘板", "燒焊", "上拆", "搭架", "清垃圾", "信号员", "裝套筒", "裝馬仔",
    "紮陣鐵", "紮柱鐵", "執石矢defect", "石矢defect", "打地台碼石矢", "打石矢", "開線", "開墨", "點焊", "外牆作石矢Cut鐵", "外牆作石矢", "Cut鐵", "撞膠筒，撩膠杯", "全層撞膠筒，撩膠杯", "全層撞膠筒", "運身橋做保護", "清石矢頭", "外牆打拆石矢", "打拆石矢", "点焊及回焊", "點焊及回焊", "釘躉", "扎躉", "打八字角", "打碼", "PD打碼", "做重欄", "清埸", "放線", "裝碼", "較碼", "较码", "全層測量", "測量", "樓窿開線", "點焊", "用蜘蛛車裝碼仔", "執九劈架位", "樓邊打地台碼石矢", "外棚清垃圾", "執石矢defect", "cut鐵&種鐵", "封板&頂底槽", "天花裝風喉", "噴漿", "种鐵", "種鐵", "cut鐵", "封板", "頂底槽", "開墨", "开墨", "包冷水喉", "裝消防水喉", "冷水喉燒焊", "冷水喉烧焊", "冷水喉", "冷氣", "消防", "電燈",
]


def is_valid_contractor(value: str | None) -> bool:
    if not value:
        return False
    value = value.strip().rstrip(":：")
    if value in INVALID_CONTRACTOR_VALUES:
        return False
    if re.match(r"^\d", value):
        return False
    if re.fullmatch(r"[A-Z]座", value):
        return False
    # Location fragments must not become contractors, e.g. 北 / Ab橋頭 / C座 近CA... / B8~9 打.
    if re.search(r"(?:^|\s)[A-Z]座", value) or re.search(r"近[A-Z0-9]|向(?:SiteB|siteB|megabox|體育園)", value):
        return False
    if re.search(r"[A-Z]\d\s*[~至-]", value) and len(value) <= 12:
        return False
    # 分判必须是中文词组；纯数字/英文/编号（如 ST01）只能作为区域/备注，不能作为分判。
    return bool(re.search(r"[\u4e00-\u9fff]", value))


ROLE_SUFFIX_RE = re.compile(r"(?:墨斗工|墨斗|焊工|炮手|男工|女工|工人|師傅)$")
WORKER_TYPE_RE = re.compile(r"(搭棚工|棚工|男工|女工|外勞|焊工|墨斗)")


def split_worker_type(value: str | None) -> tuple[str, str | None]:
    raw = clean_task(value or "")
    m = WORKER_TYPE_RE.search(raw)
    if not m:
        return raw, None
    if raw == m.group(1):
        return raw, None
    worker_type = m.group(1)
    if worker_type == "搭棚工":
        worker_type = "棚工"
    contractor = clean_task((raw[:m.start()] + raw[m.end():]).strip())
    return contractor, worker_type


def normalize_contractor_name(value: str | None) -> str:
    value = clean_task(value or "")
    # Parentheses may be aliases or trade notes, not separate contractor names.
    # 長樂（長盛） = 長樂 / 長樂長盛.
    if value in {"德鸿", "德鴻"}:
        return "德鴻"
    if value.startswith("長樂"):
        return "長樂"
    # 美時有時會寫「美時（平水）」/「美時（泥水）」等，統一為美時。
    if value.startswith("美時"):
        return "美時"
    value = re.sub(r"[（(](?:長盛|平水|泥水|水喉)[）)]", "", value).strip()
    value = ROLE_SUFFIX_RE.sub("", value).strip()
    value = re.sub(r"人$", "", value).strip()
    return value


def split_segments(text: str, group_sender: str = "null", group_sent_time: str = "null", group_sender_number: str = "null"):
    lines = text.splitlines()
    current = []
    blocks = []
    for line in lines:
        if SEGMENT_START_RE.match(line) and current:
            blocks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())

    for raw in blocks:
        first, *rest = raw.splitlines()
        m = SEGMENT_HEADER_RE.match(first)
        if m:
            body_lines = [m.group("body")] + rest
            yield {
                "發布用戶": group_sender or "null",
                "發送人號碼": group_sender_number or "null",
                "發送時間": group_sent_time or "null",
                "body": "\n".join(body_lines).strip(),
            }
        else:
            yield {"發布用戶": group_sender or "null", "發送人號碼": group_sender_number or "null", "發送時間": group_sent_time or "null", "body": raw.strip()}


def normalize_building(value: str | None):
    if not value:
        return None
    text = re.sub(r"\s+", "", value.strip())
    m = re.fullmatch(r"(?:Block|Blk)([A-Za-z]+)", text, re.I)
    if m:
        return f"{m.group(1).upper()}座"
    m = re.fullmatch(r"([A-Za-z]+)[座棟]", text, re.I)
    if m:
        return f"{m.group(1).upper()}座"
    return value.strip()


def normalize_zone(z: str | None):
    if not z:
        return None
    z = z.strip().replace("，", ",")
    return re.sub(r"\s+", " ", z)


def normalize_date(text: str | None):
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"\s*星期[一二三四五六日天]$", "", text)
    text = re.sub(r"[（(][^）)]*[）)]$", "", text)
    text = text.strip()

    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        y, mo, d = m.groups()
        return f"{int(d):02d}/{int(mo):02d}/{int(y):04d}"

    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
    if m:
        d, mo, y = m.groups()
        y = int(y)
        if y < 100:
            y += 2000
        return f"{int(d):02d}/{int(mo):02d}/{y:04d}"

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)
    if m:
        d, mo = m.groups()
        return f"{int(d):02d}/{int(mo):02d}/2026"

    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        mo, d = m.groups()
        return f"{int(d):02d}/{int(mo):02d}/2026"

    return text


def clean_task(text: str) -> str:
    text = re.sub(r"(\d+)\s+人", r"\1人", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -—,:：.。")


TASK_NORMALIZATION = [
    ("外牆作石矢Cut鐵", "Hacking Off Concrete,Cutting & bending steel reinforcement bar"),
    ("外牆作石矢", "Hacking Off Concrete"),
    ("Cut鐵", "Cutting & bending steel reinforcement bar"),
    ("撞膠筒，撩膠杯", "Site cleanliness, dust control, tidy up"),
    ("全層撞膠筒，撩膠杯", "Site cleanliness, dust control, tidy up"),
    ("清石矢頭", "Site cleanliness, dust control, tidy up"),
    ("外牆打拆石矢", "Hacking Off Concrete"),
    ("打拆石矢", "Hacking Off Concrete"),
    ("较码", "Installation of steel bracket"),
    ("較碼", "Installation of steel bracket"),
    ("測量", "Setting out"),
    ("打地台碼石矢", "打石矢"),
    ("開線", "開墨"),
    ("清垃圾", "site cleanliness"),
    ("執石矢defect", "石矢defect"),
]


def normalize_task_name(task: str | None) -> str:
    task = clean_task(task or "")
    task = re.sub(r"\bN牌\s*x?\s*\d+\b", "", task, flags=re.I)
    task = re.sub(r"\bN牌x\d+\b", "", task, flags=re.I)
    for src, dst in TASK_NORMALIZATION:
        task = task.replace(src, dst)
    return clean_task(task)


def final_clean_task(task: str | None, building: str | None = None) -> str:
    task = normalize_task_name(task)
    task = re.sub(r"^[\s/、，,]+", "", task)
    task = re.sub(r"^人\s*", "", task)
    if building:
        task = task.replace(building, "")
    task = re.sub(r"^[A-Z]座", "", task)
    task = re.sub(r"\b全層", "", task)
    task = re.sub(r"^[，,、\s之]+", "", task)
    task = re.sub(r"\s+", " ", task)
    return clean_task(task)


def strip_list_marker(text: str) -> str:
    return re.sub(r"^\s*\d+[)）.]\s*", "", text).strip()


def normalize_floor_value(value: str) -> str:
    if re.fullmatch(r'[Mm][摟樓]', value or ''):
        return 'M/F'
    if value:
        value = re.sub(r'[‑–—]', '-', value)
    return value


def extract_floors(text: str) -> tuple[str | None, str]:
    matches = list(FLOOR_RE.finditer(text))
    if not matches:
        return None, text
    floors = []
    for m in matches:
        floor = normalize_floor_value(m.group(1))
        if floor not in floors:
            floors.append(floor)
    remaining_parts = []
    last = 0
    for m in matches:
        remaining_parts.append(text[last:m.start()])
        last = m.end()
    remaining_parts.append(text[last:])
    remaining = clean_task(" ".join(remaining_parts))
    return "，".join(floors), remaining


def extract_zone(text: str):
    return extract_zones(text)


def extract_zones(text: str):
    zones = []
    def repl(m):
        z = normalize_zone(m.group(1))
        if z and z not in zones:
            zones.append(z)
        return " "
    remaining = ZONE_INLINE_RE.sub(repl, text)
    remaining = re.sub(r"[（(]\s*[）)]", "", remaining)
    remaining = clean_task(remaining)
    return (" / ".join(zones) if zones else None), remaining


def split_known_contractor(base: str):
    for name in sorted(KNOWN_CONTRACTORS, key=len, reverse=True):
        if base.startswith(name) and len(base) > len(name):
            return name, clean_task(base[len(name):])
    return None, None


def split_by_known_task(base: str):
    compact = re.sub(r"\s+", "", base)
    for task in sorted(KNOWN_TASKS, key=len, reverse=True):
        task_compact = re.sub(r"\s+", "", task)
        idx = compact.find(task_compact)
        if idx <= 0:
            continue
        # Map compact index back approximately by scanning the original string.
        consumed = 0
        original_idx = None
        for i, ch in enumerate(base):
            if ch.isspace():
                continue
            if consumed == idx:
                original_idx = i
                break
            consumed += 1
        if original_idx is None:
            continue
        contractor = clean_task(base[:original_idx])
        task_text = clean_task(base[original_idx:])
        if is_valid_contractor(contractor) and task_text:
            return contractor, task_text
    return None, None


def contains_known_task(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return any(re.sub(r"\s+", "", task) in compact for task in KNOWN_TASKS)


def looks_like_contractor_heading(line: str) -> bool:
    value = line.rstrip(":：").strip()
    if not is_valid_contractor(value):
        return False
    if re.match(r"^\d", value):
        return False
    if re.fullmatch(r"[A-Z]座", value):
        return False
    if ZONE_INLINE_RE.search(value) or contains_known_task(value):
        return False
    if len(value) > 12:
        return False
    return True


def parse_compact_no_space(before: str, current_contractor: str | None):
    zone, base = extract_zone(before)
    contractor = current_contractor
    task = None

    if current_contractor and is_valid_contractor(current_contractor) and base.startswith(current_contractor):
        contractor = current_contractor
        task = clean_task(base[len(current_contractor):])
    elif current_contractor and is_valid_contractor(current_contractor):
        contractor = current_contractor
        task = clean_task(base)
    else:
        known_contractor, known_task = split_known_contractor(base)
        if known_contractor and known_task:
            contractor = known_contractor
            task = known_task
        else:
            task_contractor, task_text = split_by_known_task(base)
            if task_contractor and task_text:
                contractor = task_contractor
                task = task_text
            else:
                m = re.match(r"^(?P<contractor>[\u4e00-\u9fff]{2,6})(?P<task>.+)$", base)
                if m:
                    contractor = m.group("contractor")
                    task = clean_task(m.group("task"))

    if contractor and is_valid_contractor(contractor) and task:
        return contractor, task, zone
    return None, None, zone


def split_task_items(text: str) -> list[str]:
    text = clean_task(text)
    if not text:
        return []
    parts = []
    current = []
    depth = 0
    for ch in text:
        if ch in "（(":
            depth += 1
        elif ch in "）)" and depth:
            depth -= 1
        if depth == 0 and ch in "、,，":
            part = clean_task("".join(current))
            if part:
                parts.append(part)
            current = []
        else:
            current.append(ch)
    part = clean_task("".join(current))
    if part:
        parts.append(part)
    return parts


def parse_counted_task_items(text: str) -> list[dict]:
    items = []
    pattern = re.compile(r"(?P<count>\d+)人(?P<task>.*?)(?=(?:[、,，]\s*\d+人)|$)")
    for m in pattern.finditer(text):
        count = m.group("count")
        task_text = clean_task(m.group("task"))
        if not task_text:
            continue
        zone, task = extract_zone(task_text)
        task = clean_task(task)
        if task:
            items.append({"工序": task, "人數": count, "分區": zone})
    return items


def parse_colon_form(line: str):
    colon_parts = re.split(r"[:：]", line, maxsplit=1)
    if len(colon_parts) != 2:
        return None
    left = clean_task(colon_parts[0])
    rest = colon_parts[1]
    if not left:
        return None

    left_count = HEADCOUNT_RE.search(left)
    rest_count = HEADCOUNT_RE.search(rest)
    if left_count:
        contractor_raw, worker_type = split_worker_type(left[:left_count.start()])
        contractor = normalize_contractor_name(contractor_raw)
        if not is_valid_contractor(contractor):
            return None

        # Form: 順利3人：1人鏟地台、2人清場（Zone4、5）
        counted_items = parse_counted_task_items(rest)
        if counted_items:
            return [{"分判": contractor, "工種": worker_type or "", **item} for item in counted_items]

        # Form: 順利6人：BS Opening吊板、鑽窿（Zone4、5）、鏟地台（Zone4）
        count = left_count.group(1)
        rows = []
        for task_text in split_task_items(rest):
            zone, task = extract_zone(task_text)
            task = clean_task(task)
            if task:
                rows.append({"分判": contractor, "工種": worker_type or "", "工序": task, "人數": count, "分區": zone})
        return rows or None

    contractor_raw, worker_type = split_worker_type(left)
    contractor = normalize_contractor_name(contractor_raw)
    m = rest_count
    if not is_valid_contractor(contractor) or not m:
        return None

    # Form: 陳橋：Zone1，3人噴粗料，1人清場
    # Keep leading zone and split each counted task into a separate row.
    if len(list(HEADCOUNT_RE.finditer(rest))) >= 2:
        leading_zone, _ = extract_zone(rest[:m.start()])
        counted_items = parse_counted_task_items(rest[m.start():])
        if counted_items:
            rows = []
            for item in counted_items:
                rows.append({"分判": contractor, "工種": worker_type or "", **item, "分區": item.get("分區") or leading_zone})
            return rows

    count = m.group(1)
    before = clean_task(rest[:m.start()])
    after = clean_task(rest[m.end():])
    combined = clean_task((before + " " + after).strip())
    zone, task = extract_zone(combined)
    task = clean_task(task)
    if not task:
        return None
    return {"分判": contractor, "工種": worker_type or "", "工序": task, "人數": count, "分區": zone}


def parse_no_headcount_record(line: str, current_contractor: str | None):
    # Allow records without explicit headcount, e.g. 陳橋 zone 5 噴漿.
    zone, base = extract_zone(line)
    if not zone:
        return None
    base = clean_task(base)
    if not base:
        return None
    contractor = None
    task = None
    if current_contractor and is_valid_contractor(current_contractor):
        contractor = current_contractor
        task = base
    else:
        return None
        known_contractor, known_task = split_known_contractor(base)
        if known_contractor and known_task:
            contractor = known_contractor
            task = known_task
        else:
            task_contractor, task_text = split_by_known_task(base)
            if task_contractor and task_text:
                contractor = task_contractor
                task = task_text
            else:
                m = re.match(r"^(?P<contractor>[\u4e00-\u9fff]{2,6})\s*(?P<task>.+)$", base)
                if m:
                    contractor = m.group("contractor")
                    task = clean_task(m.group("task"))
    if contractor and is_valid_contractor(contractor) and task:
        # Only emit no-headcount records when the line starts with a contractor, e.g. 陳橋 zone 5 噴漿.
        # Location/task pending lines like 15樓南面... should wait for the following 分判：人數 line.
        if line.strip().startswith(contractor):
            return {"分判": contractor, "工序": task, "人數": "null", "分區": zone}
    return None


def parse_multi_headcount_inline(line: str, current_contractor: str | None):
    matches = list(HEADCOUNT_RE.finditer(line))
    if len(matches) < 2:
        return None

    first = matches[0]
    prefix = clean_task(line[:first.start()])
    if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", prefix) and len(matches) >= 2:
        return None
    zone, prefix_no_zone = extract_zone(prefix)
    contractor = None
    first_task = None

    known_contractor, known_task = split_known_contractor(prefix_no_zone)
    if known_contractor:
        contractor = known_contractor
        first_task = known_task
    else:
        contractor_candidate, task_candidate = split_by_known_task(prefix_no_zone)
        if contractor_candidate:
            contractor = contractor_candidate
            first_task = task_candidate
        elif current_contractor and is_valid_contractor(current_contractor):
            contractor = current_contractor
            first_task = prefix_no_zone

    if not contractor or not is_valid_contractor(contractor):
        return None

    rows = []
    task = normalize_task_name(first_task)
    if task:
        rows.append({"分判": contractor, "工序": task, "人數": first.group(1), "分區": zone})

    for idx in range(1, len(matches)):
        m = matches[idx]
        prev = matches[idx - 1]
        between = clean_task(line[prev.end():m.start()])
        item_zone, item_task = extract_zone(between)
        item_task = normalize_task_name(item_task)
        if item_task:
            rows.append({"分判": contractor, "工序": item_task, "人數": m.group(1), "分區": item_zone or zone})

    return rows or None


def maybe_extract_inline_record(line: str, current_contractor: str | None):
    colon_form = parse_colon_form(line)
    if colon_form:
        return colon_form

    multi_form = parse_multi_headcount_inline(line, current_contractor)
    if multi_form:
        return multi_form

    m = HEADCOUNT_RE.search(line)
    if not m:
        return None

    count = m.group(1)
    before = final_clean_task(line[:m.start()])
    after = clean_task(line[m.end():])

    # Continuation under a contractor heading: Zone1-5，2人清理廢鐵料
    if current_contractor and is_valid_contractor(current_contractor):
        z_cont, before_no_zone_cont = extract_zone(before)
        before_no_zone_cont = clean_task(before_no_zone_cont).strip('，,、')
        if z_cont and not before_no_zone_cont and after:
            return {"分判": current_contractor, "工序": final_clean_task(after), "人數": count, "分區": z_cont}

    total_then_detail = re.match(r'^(?P<contractor>[\u4e00-\u9fff]{2,8})$', before)
    detail = re.match(r'^(?P<count>\d+)人(?P<rest>.+)$', after)
    if total_then_detail and detail:
        contractor = normalize_contractor_name(total_then_detail.group('contractor'))
        if is_valid_contractor(contractor):
            zone_detail, task_detail = extract_zone(detail.group('rest'))
            task_detail = final_clean_task(task_detail)
            if task_detail:
                return {"分判": contractor, "工序": task_detail, "人數": detail.group('count'), "分區": zone_detail}

    if not current_contractor and before:
        zone_before_early, before_no_zone_early = extract_zone(before)
        task_contractor_early, task_text_early = split_by_known_task(before_no_zone_early)
        if task_contractor_early and task_text_early:
            zone_after, after_no_zone = extract_zone(after)
            task = normalize_task_name((task_text_early + " " + after_no_zone).strip())
            if task:
                return {"分判": task_contractor_early, "工序": task, "人數": count, "分區": zone_before_early or zone_after}
        if is_valid_contractor(before):
            zone_after, after_no_zone = extract_zone(after)
            task = normalize_task_name(after_no_zone)
            if task:
                return {"分判": before, "工序": task, "人數": count, "分區": zone_after}
        known_contractor, prefix_task = split_known_contractor(before)
        if known_contractor:
            zone_after, after_no_zone = extract_zone(after)
            zone_before, prefix_no_zone = extract_zone(prefix_task or "")
            task = clean_task((prefix_no_zone + " " + after_no_zone).strip())
            if task:
                return {"分判": known_contractor, "工序": task, "人數": count, "分區": zone_after or zone_before}

    known_contractor, known_task = split_known_contractor(before)
    if known_contractor and (known_task or after):
        zone_before, before_no_zone = extract_zone(known_task or "")
        zone_after, after_no_zone = extract_zone(after)
        task = clean_task((before_no_zone + " " + after_no_zone).strip())
        if task:
            return {"分判": known_contractor, "工序": task, "人數": count, "分區": zone_before or zone_after}

    # Lines like C座12/F... may have no explicit contractor; do not infer C座 as 分判.
    if re.match(r"^[A-Z]座", before):
        zone_b, task_b = extract_zone(before)
        task_b = final_clean_task(task_b)
        if current_contractor and is_valid_contractor(current_contractor) and task_b:
            return {"分判": current_contractor, "工序": task_b, "人數": count, "分區": zone_b}

    if current_contractor and is_valid_contractor(current_contractor) and not before:
        zone, task = extract_zone(after)
        task = clean_task(task)
        if task:
            return {"分判": current_contractor, "工序": task, "人數": count, "分區": zone}

    floor_prefix, before_without_floor = extract_floors(before)
    if floor_prefix and before_without_floor and before_without_floor != before:
        before_without_floor = final_clean_task(before_without_floor)
        zone_floor, before_floor_no_zone = extract_zone(before_without_floor)
        before_floor_no_zone = final_clean_task(before_floor_no_zone)
        known_floor_contractor, known_floor_task = split_known_contractor(before_floor_no_zone)
        if known_floor_contractor and (known_floor_task or after):
            task = final_clean_task(((known_floor_task or "") + " " + after).strip())
            if task:
                return {"分判": known_floor_contractor, "工序": task, "人數": count, "分區": zone_floor}

    if current_contractor and is_valid_contractor(current_contractor) and before:
        zone_new, before_no_zone = extract_zone(before)
        # Inline rows can switch contractor even under a previous contractor heading:
        #   順利 1人 zone 4 地台打花
        #   仙壁 8人 zone4 &zone 3 封板&頂底槽&塞棉
        # Do not inherit current_contractor when the text before N人 is itself a contractor.
        if is_valid_contractor(before_no_zone):
            zone_after, after_no_zone = extract_zone(after)
            task = final_clean_task(after_no_zone)
            if task:
                return {"分判": normalize_contractor_name(before_no_zone), "工序": task, "人數": count, "分區": zone_new or zone_after}
        known_contractor2, known_task2 = split_known_contractor(before_no_zone)
        if known_contractor2 and known_task2:
            task = final_clean_task((known_task2 + " " + after).strip())
            if task:
                return {"分判": known_contractor2, "工序": task, "人數": count, "分區": zone_new}
        new_contractor, new_task = split_by_known_task(before_no_zone)
        if new_contractor and new_task:
            task = final_clean_task((new_task + " " + after).strip())
            if task:
                return {"分判": new_contractor, "工序": task, "人數": count, "分區": zone_new}
        zone, task = extract_zone(before)
        task = clean_task(task)
        after_task = clean_task(after)
        if zone and not task and after_task:
            return {"分判": current_contractor, "工序": after_task, "人數": count, "分區": zone}
        if task:
            return {"分判": current_contractor, "工序": task, "人數": count, "分區": zone}

    zone_before, before_no_zone = extract_zone(before)
    task_contractor, task_text = split_by_known_task(before_no_zone)
    if task_contractor and task_text:
        after_task = normalize_task_name(after)
        task = normalize_task_name((task_text + " " + after_task).strip())
        if task:
            return {"分判": task_contractor, "工序": task, "人數": count, "分區": zone_before}

    contractor, task, zone = parse_compact_no_space(before, current_contractor)
    if contractor and task:
        return {"分判": contractor, "工序": task, "人數": count, "分區": zone}

    zone, remaining = extract_zone(before)
    after_task = clean_task(after)
    if zone and after_task:
        return {"分判": current_contractor if is_valid_contractor(current_contractor) else "null", "工序": after_task, "人數": count, "分區": zone}

    return None


def split_floor_task_line(line: str):
    text = clean_task(line)
    zone, text_no_zone = extract_zone(text)
    text_no_zone = clean_task(text_no_zone)
    range_floor = re.search(r"(?P<floor>\d+\s*[-至]\s*(?:G|\d+)樓?)", text_no_zone, re.I)
    if range_floor:
        floor = range_floor.group("floor").replace(" ", "")
        task = clean_task((text_no_zone[:range_floor.start()] + " " + text_no_zone[range_floor.end():]).strip())
        task = re.sub(r"^至\s*", "", task)
    else:
        floor, task = extract_floors(text_no_zone)
    if not floor:
        m2 = re.match(r"^(?P<floor>(?:\d+以上樓|\d+樓[^\s]*|G/[Ff][^\s]*|M/[Ff][^\s]*|B\d+[^\s]*))(?P<task>.+)$", text_no_zone)
        if m2:
            floor = m2.group("floor")
            task = clean_task(m2.group("task"))
        else:
            task = text_no_zone
    return floor, zone, task


def parse_colon_headcount_with_pending(line: str, pending_task_line: str | None):
    if not pending_task_line:
        return None
    m = re.match(r"^(?P<contractor>.+?)[:：]\s*(?P<count>\d+)人\s*$", line)
    if not m:
        return None
    contractor_raw, worker_type = split_worker_type(m.group("contractor"))
    contractor = normalize_contractor_name(contractor_raw)
    if not is_valid_contractor(contractor):
        return None
    floor, zone, task = split_floor_task_line(pending_task_line)
    if not task:
        return None
    return {"分判": contractor, "工種": worker_type or "", "工序": task, "人數": m.group("count"), "分區": zone, "樓層": floor}



def split_line_on_contractor_switches(line: str) -> list[str]:
    """Split compact lines when a new known contractor starts after punctuation/space."""
    parts = []
    start = 0
    pattern = r'(?<=[。；;，,\s])(' + '|'.join(re.escape(x) for x in sorted(KNOWN_CONTRACTORS, key=len, reverse=True)) + r')(?=\s|[\u4e00-\u9fff]|\d)'
    for m in re.finditer(pattern, line):
        if m.start() <= start:
            continue
        prev = line[start:m.start()].strip(' 。；;，,')
        if prev:
            parts.append(prev)
        start = m.start()
    tail = line[start:].strip(' 。；;，,')
    if tail:
        parts.append(tail)
    return parts or [line]

def parse_segment(seg: dict):
    body = seg["body"]
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith(NON_WORK_PREFIXES)]
    context = {"日期": None, "樓棟": None, "樓層": None, "分區": None}
    rows = []
    current_contractor = None
    pending_floor = None
    pending_task_line = None

    def make_row(inline_row: dict, row_floor: str | None) -> dict:
        contractor, worker_type = split_worker_type(inline_row.get("分判"))
        task_raw, task_worker_type = split_worker_type(inline_row.get("工序"))
        contractor = normalize_contractor_name(contractor)
        return {
            "發布用戶": seg["發布用戶"],
            "發送人號碼": seg.get("發送人號碼", "null"),
            "發送時間": seg["發送時間"],
            "日期": context["日期"] or "null",
            "分區": inline_row.get("分區") or context["分區"] or "null",
            "樓棟": context["樓棟"] or "null",
            "樓層": row_floor or "null",
            "分判": contractor or "null",
            "工種": (None if inline_row.get("工種") == "null" else inline_row.get("工種")) or worker_type or task_worker_type or "",
            "工序": final_clean_task(task_raw, context.get("樓棟")),
            "人數": inline_row["人數"],
            "原始消息": body,
        }

    for line in lines:
        line = strip_list_marker(line)
        if any(hint in line for hint in IGNORE_TASK_HINTS):
            pending_task_line = None
            continue
        # 小計/總數行不是施工記錄，例如 德鴻... / 合共19人。
        if re.match(r"^(合共|共計|总计|總計)\s*\d+人", line):
            pending_task_line = None
            continue
        pending_colon = parse_colon_headcount_with_pending(line, pending_task_line)
        if pending_colon:
            rows.append(make_row(pending_colon, pending_colon.get("樓層") or context["樓層"]))
            pending_task_line = None
            continue

        dm = DATE_RE.search(line)
        if dm:
            context["日期"] = normalize_date(dm.group(1))
            current_contractor = None

        bm = BUILDING_RE.search(line)
        if bm:
            context["樓棟"] = normalize_building(bm.group(1))
            if "外牆" in line:
                context["分區"] = "外牆"
            if not HEADCOUNT_RE.search(line) and not FLOOR_RE.search(line) and line.strip() in {bm.group(1), f'{bm.group(1)}外牆'}:
                current_contractor = None

        floor_only = FLOOR_RE.fullmatch(line)
        if floor_only:
            context["樓層"] = normalize_floor_value(floor_only.group(1))
            pending_floor = context["樓層"]
            # Zone headings like ST01 should not leak to subsequent explicit floors.
            if context.get("分區") and re.fullmatch(r"[A-Za-z]+\d+", context["分區"]):
                context["分區"] = None
            current_contractor = None
            continue

        building_floor = re.match(r"^(?P<building>[A-Za-z]座|Block\s*[A-Za-z]+|Blk\s*[A-Za-z]+)\s+(?P<floor>(?:\d+|[A-Za-z]+)/[Ff])$", line, re.I)
        if building_floor:
            context["樓棟"] = normalize_building(building_floor.group("building"))
            context["樓層"] = normalize_floor_value(building_floor.group("floor"))
            current_contractor = None
            continue

        if ZONE_INLINE_RE.fullmatch(line) and not HEADCOUNT_RE.search(line):
            context["分區"] = normalize_zone(line)
            current_contractor = None
            continue

        compact_heading = re.fullmatch(r"(?P<date>\d{1,2}-\d{1,2}-\d{4})\s+(?P<building>[A-Za-z]座|[A-Za-z]棟)\s*(?P<floor>[A-Za-z]摟|[A-Za-z]樓|(?:\d+|[A-Za-z]+)/[Ff])", line, re.I)
        if compact_heading:
            context["日期"] = normalize_date(compact_heading.group("date"))
            context["樓棟"] = normalize_building(compact_heading.group("building"))
            context["樓層"] = normalize_floor_value(compact_heading.group("floor"))
            current_contractor = None
            continue

        total_heading = re.fullmatch(r"(?P<contractor>[\u4e00-\u9fff]{2,8})人?(?P<count>\d+)人", line)
        if total_heading:
            contractor = normalize_contractor_name(total_heading.group("contractor"))
            # Only treat as a contractor total heading when it is really a plain contractor name.
            # Lines like 順利清場1人 / 順利做重欄2人 are actual work rows and must not be swallowed.
            if is_valid_contractor(contractor) and contractor in KNOWN_CONTRACTORS:
                current_contractor = contractor
                continue

        if not HEADCOUNT_RE.search(line):
            pending_match = FLOOR_RE.search(line)
            if pending_match:
                pending_floor = pending_match.group(1)

        if CONTRACTOR_HEADING_RE.match(line) and not is_valid_contractor(line) and not HEADCOUNT_RE.search(line) and not FLOOR_RE.search(line) and not DATE_RE.search(line) and not BUILDING_RE.search(line):
            context["分區"] = normalize_zone(line)
            current_contractor = None
            continue

        if CONTRACTOR_HEADING_RE.match(line) and looks_like_contractor_heading(line) and not HEADCOUNT_RE.search(line) and not FLOOR_RE.search(line) and not DATE_RE.search(line) and not BUILDING_RE.search(line):
            current_contractor = line.rstrip(":：").strip()
            continue

        line_for_record = line
        row_floor = context["樓層"]
        embedded_floors, line_without_floors = extract_floors(line)
        if embedded_floors and HEADCOUNT_RE.search(line):
            row_floor = embedded_floors
            pending_floor = row_floor
            line_for_record = line_without_floors
        elif HEADCOUNT_RE.search(line) and pending_floor:
            row_floor = pending_floor

        floor_removed_from_record = bool(embedded_floors and HEADCOUNT_RE.search(line))
        # If a contractor heading appears on the previous line, e.g.
        # 偉健 / 20/F 上拆埋碼8人, keep that contractor after removing the floor.
        inline_current_contractor = current_contractor
        record_parts = split_line_on_contractor_switches(line_for_record) if HEADCOUNT_RE.search(line_for_record) else [line_for_record]
        inline = []
        local_contractor = inline_current_contractor
        for record_part in record_parts:
            parsed_part = maybe_extract_inline_record(record_part, local_contractor)
            if parsed_part:
                part_rows = parsed_part if isinstance(parsed_part, list) else [parsed_part]
                inline.extend(part_rows)
                if part_rows and is_valid_contractor(part_rows[-1].get("分判")):
                    local_contractor = part_rows[-1].get("分判")
        if not inline:
            inline = None
        if not inline and not HEADCOUNT_RE.search(line_for_record):
            inline = parse_no_headcount_record(line_for_record, current_contractor)
        if inline:
            inline_rows = inline if isinstance(inline, list) else [inline]
            for inline_row in inline_rows:
                if is_valid_contractor(inline_row.get("分判")):
                    current_contractor = inline_row.get("分判")
                rows.append(make_row(inline_row, row_floor))
            pending_task_line = None
        elif (
            not HEADCOUNT_RE.search(line)
            and not DATE_RE.search(line)
            and not BUILDING_RE.fullmatch(line)
            and not (BUILDING_RE.search(line) and not contains_known_task(line) and not FLOOR_RE.search(line))
            and (contains_known_task(line) or FLOOR_RE.search(line) or re.match(r"^\d+以上樓", line) or re.match(r"^\d+樓", line) or re.match(r"^G/[Ff]", line))
        ):
            pending_task_line = line

    return rows


def rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "null") for k in CSV_COLUMNS})
    return buf.getvalue()


def dedupe_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        key = tuple(row.get(k, "") for k in CSV_COLUMNS)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    # Headers like "B座 B1" / "C座 B2" are block + basement zone markers;
    # put B1/B2 into 分區, not 樓層.
    m_bzone = re.search(r'([ABC]座)\s*(B\d+)\b', raw, re.I)
    if m_bzone and str(out.get('樓棟') or '').strip() == m_bzone.group(1):
        zone_now = str(out.get('分區') or '').strip().lower()
        floor_now = str(out.get('樓層') or '').strip()
        if zone_now in {'', 'null'} or floor_now == m_bzone.group(2):
            out['分區'] = m_bzone.group(2)
            if floor_now == m_bzone.group(2):
                out['樓層'] = 'null'

    return out


def main():
    parser = argparse.ArgumentParser(description="End-to-end pipeline: log -> extract engineering -> structure -> import to Feishu")
    parser.add_argument("log_file")
    parser.add_argument("--csv-out", help="Write parsed CSV to file")
    parser.add_argument("--dry-run", action="store_true", help="Only print CSV, do not upload")
    args = parser.parse_args()

    messages = extract_messages(Path(args.log_file))
    selected = [m for m in messages if looks_like_engineering_message(m["text"])]

    rows = []
    for msg in selected:
        for seg in split_segments(msg["text"], msg.get("sender", "null"), msg.get("log_ts", "null")):
            rows.extend(parse_segment(seg))

    rows = dedupe_rows(rows)
    if not rows:
        raise SystemExit("No engineering records parsed from log")

    csv_text = rows_to_csv(rows)
    if args.csv_out:
        Path(args.csv_out).write_text(csv_text, encoding="utf-8")

    if args.dry_run:
        print(csv_text)
        return

    token = get_tenant_access_token()
    records = parse_csv_text(csv_text)
    created = upload_records(records, token)
    print(f"Parsed {len(rows)} rows and imported {created} records to Feishu Bitable")


if __name__ == "__main__":
    main()

