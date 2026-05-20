#!/usr/bin/env python3
import argparse
import re
from pathlib import Path

LOG_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] \[LOG\] 文本消息内容: (?P<content>.*)$")
DEBUG_SENDER_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] \[DEBUG 发送人的number, name, pushname分别是\]\s*(?P<number>\S+)?\s*(?P<name>\S+)?\s*(?P<pushname>.*)$")
INSP_BOT_METADATA_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] (?:重新强制获取消息|收到消息，|\[DEBUG )")

ENGINEERING_HINTS = [
    "Block", "Blk", "座", "棟", "楼", "樓", "/F", "M/F", "zone", "Zone", "扎鐵", "撘架",
    "人", "砌磚", "釘基仔", "噴幼料", "噴粗料", "做油漆", "磚牆", "清場", "搭架",
    "開線", "开线", "開綫", "开墨", "裝套筒", "消防", "冷氣", "電燈", "風喉", "做喉",
    "打花", "拆板", "泵水", "執defect", "信号员", "清垃圾", "紮", "燒焊", "钉板", "釘網",
]

TIMESTAMPED_CHAT_RE = re.compile(r"\[\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}:\d{2}\]\s*.*?:")
DATE_RE = re.compile(r"(?:\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}月\d{1,2}日)")
HEADCOUNT_RE = re.compile(r"\d+人")
FLOOR_RE = re.compile(r"(?:\d+|[A-Za-z]+)/(?:F|f)|\d+樓|B\d+")
UNIT_RE = re.compile(r"\b[A-Za-z]\d{1,2}-\d{2,3}[A-Za-z]?\b")


def looks_like_engineering_message(text: str) -> bool:
    text = text.strip()
    if not text:
        return False

    lowered = text.lower()
    if lowered in {"hi", "hello", "/status", "1", "11"}:
        return False
    if text.startswith("# ") and "工地信息结构化提取专家" in text:
        return False

    hint_hits = sum(1 for h in ENGINEERING_HINTS if h in text)
    structural_hits = sum([
        1 if TIMESTAMPED_CHAT_RE.search(text) else 0,
        1 if DATE_RE.search(text) else 0,
        1 if HEADCOUNT_RE.search(text) else 0,
        1 if FLOOR_RE.search(text) else 0,
        1 if UNIT_RE.search(text) else 0,
    ])

    if DATE_RE.search(text) and HEADCOUNT_RE.search(text) and UNIT_RE.search(text):
        return True
    if TIMESTAMPED_CHAT_RE.search(text) and DATE_RE.search(text) and HEADCOUNT_RE.search(text):
        return True
    if hint_hits >= 2 and (HEADCOUNT_RE.search(text) or DATE_RE.search(text)) and (FLOOR_RE.search(text) or "座" in text or "Block" in text or "Blk" in text):
        return True
    if hint_hits >= 4 and structural_hits >= 2:
        return True
    return False


def extract_messages(log_path: Path):
    current = None
    messages = []
    pending_sender = None
    pending_sender_number = None

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            dbg = DEBUG_SENDER_RE.match(line)
            if dbg:
                pushname = (dbg.group("pushname") or "").strip()
                name = (dbg.group("name") or "").strip()
                number = (dbg.group("number") or "").strip()
                sender = pushname or (None if name == "undefined" else name) or number or "null"
                pending_sender = sender
                pending_sender_number = number
                continue

            m = LOG_LINE_RE.match(line)
            if m:
                if current is not None:
                    messages.append(current)
                current = {
                    "log_ts": m.group("ts"),
                    "text": m.group("content"),
                    "sender": pending_sender or "null",
                    "sender_number": pending_sender_number or "null",
                }
                pending_sender = None
                pending_sender_number = None
            elif current is not None:
                # Multi-line WhatsApp message content continues as plain lines after
                # the [LOG] line. insp-bot metadata for the next message also appears
                # before the next [LOG] line; do not swallow those wrapper lines into
                # the previous message, or the next message can be parsed twice.
                if INSP_BOT_METADATA_RE.match(line):
                    messages.append(current)
                    current = None
                    continue
                current["text"] += "\n" + line

    if current is not None:
        messages.append(current)
    return messages


def main():
    parser = argparse.ArgumentParser(description="Extract engineering-related messages from insp-bot log")
    parser.add_argument("log_file", help="Path to .log file")
    parser.add_argument("--output", "-o", help="Write extracted message blocks to file")
    parser.add_argument("--all", action="store_true", help="Output all messages with separators for debugging")
    args = parser.parse_args()

    log_path = Path(args.log_file)
    if not log_path.exists():
        raise SystemExit(f"Log file not found: {log_path}")

    messages = extract_messages(log_path)
    selected = messages if args.all else [m for m in messages if looks_like_engineering_message(m["text"])]

    blocks = []
    for m in selected:
        sender = m.get('sender', 'null')
        blocks.append(f"[GROUP_SENDER:{sender}]\n" + m["text"].strip())

    output = "\n\n".join(blocks)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
