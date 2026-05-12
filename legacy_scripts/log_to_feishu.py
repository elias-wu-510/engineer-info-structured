#!/usr/bin/env python3
import argparse
import csv
import io
import re
from pathlib import Path

from extract_engineering_messages import extract_messages, looks_like_engineering_message
from feishu_bitable_import import CSV_COLUMNS, get_tenant_access_token, upload_records, parse_csv_text

DATE_RE = re.compile(r"(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/\d{1,2}/\d{2}(?:\([^)]*\))?|\d{1,2}-\d{1,2}-\d{4}|\d{1,2}月\d{1,2}日)")
BUILDING_RE = re.compile(r"(Block\s*[A-Za-z]+|Blk\s*[A-Za-z]+|[A-Za-z]座|[A-Za-z]棟)", re.I)
FLOOR_RE = re.compile(r"((?:(?:\d+|[A-Za-z]+|MR|UP)/[Ff])(?:至(?:\d+|[A-Za-z]+|MR|UP)/[Ff])?(?:及(?:\d+|[A-Za-z]+|MR|UP)/[Ff])?|\d+樓|[A-Za-z]摟|[A-Za-z]樓|B\d+|M/[Ff]|m/[Ff]|MR/[Ff]|UP/[Ff])")
ZONE_INLINE_RE = re.compile(r"([Zz]one\s*\d+[A-Za-z]?(?:\s*(?:&|＆|/|、|,|，)\s*(?:[Zz]one\s*)?\d+[A-Za-z]?)*|[A-Z]\d{1,2}[-‑–—]\d{2,3}[A-Za-z]?|[A-Z]區|全場|lift機房)")
HEADCOUNT_RE = re.compile(r"[（(]?(\d+)人[）)]?")
SEGMENT_HEADER_RE = re.compile(r"^\[(?P<ts>\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}:\d{2})\]\s*(?P<user>.*?):\s*(?P<body>.*)$")
SEGMENT_START_RE = re.compile(r"^\[\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}:\d{2}\]\s*.*?:")
NON_WORK_PREFIXES = ("收到消息", "[DEBUG", "[LOG]")
CONTRACTOR_HEADING_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9·•\-~  ]{1,20}[:：]?$")
KNOWN_CONTRACTORS = [
    "陳橋", "藝薪", "藝新", "日麗雅", "明泰", "順利", "萬通", "偉健", "利安", "秦深记", "美時",
    "中機電", "遠東德鴻", "捷信", "駿慶", "萬利", "力成", "仙壁", "康和", "恆昇", "恒記",
    "浩洲", "安全外勞", "安全外",
]

KNOWN_TASKS = [
    "安裝Drywall", "BS Opening吊板", "BS opening 吊板", "鑽窿", "鏟地台+清理", "鏟地台", "跟炮尾清泥頭", "清場", "磚牆",
    "公眾位出泥柱", "公眾位包角", "產地台", "跟炮尾", "磚牆釘網", "磚牆包角",
    "出泥柱", "砌磚", "批幼料", "砌磚牆", "牆身釘網", "大機房噴油漆",
    "PD裝喉", "線坑批蘯", "mark位 裝燈喉", "天花過面", "HR種鐵", "地台出餅仔",
    "洗地", "扶手電梯位砌磚", "地台轉吼", "泵水", "開料", "裝喉", "燈喉",
    "釘板", "燒焊", "上拆", "搭架", "清垃圾", "信号员", "裝套筒", "裝馬仔",
    "紮陣鐵", "紮柱鐵", "外牆作石矢Cut鐵", "外牆作石矢", "全層撞膠筒，撩膠杯", "全層撞膠筒", "運身橋做保護", "清石矢頭", "外牆打拆石矢", "点焊及回焊", "較碼", "较码", "全層測量", "測量", "樓窿開線", "點焊", "用蜘蛛車裝碼仔", "執九劈架位", "樓邊打地台碼石矢", "外棚清垃圾", "執石矢defect", "cut鐵&種鐵", "封板&頂底槽", "天花裝風喉", "噴漿", "种鐵", "種鐵", "cut鐵", "封板", "頂底槽", "開墨", "开墨", "包冷水喉", "裝消防水喉", "冷水喉燒焊", "冷水喉烧焊", "冷水喉", "冷氣", "消防", "電燈",
]


def is_valid_contractor(value: str | None) -> bool:
    if not value:
        return False
    value = value.strip().rstrip(":：")
    # 分判必须是中文词组；纯数字/英文/编号（如 ST01）只能作为区域/备注，不能作为分判。
    return bool(re.search(r"[\u4e00-\u9fff]", value))


ROLE_SUFFIX_RE = re.compile(r"(?:墨斗工|焊工|炮手|男工|女工|工人|師傅)$")


def normalize_contractor_name(value: str | None) -> str:
    value = clean_task(value or "")
    value = ROLE_SUFFIX_RE.sub("", value).strip()
    return value


def split_segments(text: str, group_sender: str = "null", group_sent_time: str = "null"):
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
                "發送時間": group_sent_time or "null",
                "body": "\n".join(body_lines).strip(),
            }
        else:
            yield {"發布用戶": group_sender or "null", "發送時間": group_sent_time or "null", "body": raw.strip()}


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
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if m:
        y, mo, d = m.groups()
        return f"{int(d)}/{int(mo)}/{y}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        d, mo, y = m.groups()
        return f"{int(d)}/{int(mo)}/{y}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2})(?:\([^)]*\))?", text)
    if m:
        d, mo, y = m.groups()
        return f"{int(d)}/{int(mo)}/20{int(y):02d}"
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", text)
    if m:
        d, mo, y = m.groups()
        return f"{int(d)}/{int(mo)}/{y}"
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        mo, d = m.groups()
        return f"{int(d)}/{int(mo)}/2026"
    return text


def clean_task(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -—,:：.。")


def strip_list_marker(text: str) -> str:
    return re.sub(r"^\s*\d+[)）.]\s*", "", text).strip()


def extract_floors(text: str) -> tuple[str | None, str]:
    matches = list(FLOOR_RE.finditer(text))
    if not matches:
        return None, text
    floors = []
    for m in matches:
        floor = m.group(1)
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
    m = ZONE_INLINE_RE.search(text)
    if not m:
        return None, text
    zone = normalize_zone(m.group(1))
    remaining = clean_task((text[:m.start()] + " " + text[m.end():]).strip())
    remaining = re.sub(r"[（(]\s*[）)]", "", remaining)
    remaining = clean_task(remaining)
    return zone, remaining


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
        contractor = clean_task(left[:left_count.start()])
        if not is_valid_contractor(contractor):
            return None

        # Form: 順利3人：1人鏟地台、2人清場（Zone4、5）
        counted_items = parse_counted_task_items(rest)
        if counted_items:
            return [{"分判": contractor, **item} for item in counted_items]

        # Form: 順利6人：BS Opening吊板、鑽窿（Zone4、5）、鏟地台（Zone4）
        count = left_count.group(1)
        rows = []
        for task_text in split_task_items(rest):
            zone, task = extract_zone(task_text)
            task = clean_task(task)
            if task:
                rows.append({"分判": contractor, "工序": task, "人數": count, "分區": zone})
        return rows or None

    contractor = left
    m = rest_count
    if not is_valid_contractor(contractor) or not m:
        return None
    count = m.group(1)
    before = clean_task(rest[:m.start()])
    after = clean_task(rest[m.end():])
    combined = clean_task((before + " " + after).strip())
    zone, task = extract_zone(combined)
    task = clean_task(task)
    if not task:
        return None
    return {"分判": contractor, "工序": task, "人數": count, "分區": zone}


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
        return {"分判": contractor, "工序": task, "人數": "null", "分區": zone}
    return None


def maybe_extract_inline_record(line: str, current_contractor: str | None):
    colon_form = parse_colon_form(line)
    if colon_form:
        return colon_form

    m = HEADCOUNT_RE.search(line)
    if not m:
        return None

    count = m.group(1)
    before = clean_task(line[:m.start()])
    after = clean_task(line[m.end():])

    if not current_contractor and before:
        if is_valid_contractor(before):
            zone_after, after_no_zone = extract_zone(after)
            task = clean_task(after_no_zone)
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

    if current_contractor and is_valid_contractor(current_contractor) and not before:
        zone, task = extract_zone(after)
        task = clean_task(task)
        if task:
            return {"分判": current_contractor, "工序": task, "人數": count, "分區": zone}

    if current_contractor and is_valid_contractor(current_contractor) and before:
        zone, task = extract_zone(before)
        task = clean_task(task)
        after_task = clean_task(after)
        if zone and not task and after_task:
            return {"分判": current_contractor, "工序": after_task, "人數": count, "分區": zone}
        if task:
            return {"分判": current_contractor, "工序": task, "人數": count, "分區": zone}

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
    m = re.match(r"^(?P<contractor>[\u4e00-\u9fffA-Za-z0-9·•\-~  ]{2,20})[:：]\s*(?P<count>\d+)人\s*$", line)
    if not m:
        return None
    contractor = normalize_contractor_name(m.group("contractor"))
    if not is_valid_contractor(contractor):
        return None
    floor, zone, task = split_floor_task_line(pending_task_line)
    if not task:
        return None
    return {"分判": contractor, "工序": task, "人數": m.group("count"), "分區": zone, "樓層": floor}


def parse_segment(seg: dict):
    body = seg["body"]
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith(NON_WORK_PREFIXES)]
    context = {"日期": None, "樓棟": None, "樓層": None, "分區": None}
    rows = []
    current_contractor = None
    pending_floor = None
    pending_task_line = None

    for line in lines:
        line = strip_list_marker(line)
        pending_colon = parse_colon_headcount_with_pending(line, pending_task_line)
        if pending_colon:
            rows.append({
                "發布用戶": seg["發布用戶"],
                "發送時間": seg["發送時間"],
                "日期": context["日期"] or "null",
                "分區": pending_colon.get("分區") or context["分區"] or "null",
                "樓棟": context["樓棟"] or "null",
                "樓層": pending_colon.get("樓層") or context["樓層"] or "null",
                "分判": pending_colon["分判"],
                "工序": pending_colon["工序"],
                "人數": pending_colon["人數"],
            })
            pending_task_line = None
            continue

        dm = DATE_RE.search(line)
        if dm:
            context["日期"] = normalize_date(dm.group(1))

        bm = BUILDING_RE.search(line)
        if bm:
            context["樓棟"] = normalize_building(bm.group(1))
            if "外牆" in line:
                context["分區"] = "外牆"

        floor_only = FLOOR_RE.fullmatch(line)
        if floor_only:
            context["樓層"] = floor_only.group(1)
            pending_floor = context["樓層"]
            continue

        building_floor = re.match(r"^(?P<building>[A-Za-z]座|Block\s*[A-Za-z]+|Blk\s*[A-Za-z]+)\s+(?P<floor>(?:\d+|[A-Za-z]+)/[Ff])$", line, re.I)
        if building_floor:
            context["樓棟"] = normalize_building(building_floor.group("building"))
            context["樓層"] = building_floor.group("floor")
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
            context["樓層"] = compact_heading.group("floor")
            current_contractor = None
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

        inline = maybe_extract_inline_record(line_for_record, current_contractor)
        if not inline and not HEADCOUNT_RE.search(line_for_record):
            inline = parse_no_headcount_record(line_for_record, current_contractor)
        if inline:
            inline_rows = inline if isinstance(inline, list) else [inline]
            for inline_row in inline_rows:
                rows.append({
                    "發布用戶": seg["發布用戶"],
                    "發送時間": seg["發送時間"],
                    "日期": context["日期"] or "null",
                    "分區": inline_row.get("分區") or context["分區"] or "null",
                    "樓棟": context["樓棟"] or "null",
                    "樓層": row_floor or "null",
                    "分判": inline_row["分判"],
                    "工序": inline_row["工序"],
                    "人數": inline_row["人數"],
                })
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

