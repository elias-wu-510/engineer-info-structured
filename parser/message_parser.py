import re
from schema.engineer_record import EngineerRecord

WORKER_PATTERN = re.compile(r"(\\d+)\\s*人")


def parse_message(text: str) -> EngineerRecord:
    workers = None
    m = WORKER_PATTERN.search(text)
    if m:
        workers = int(m.group(1))

    return EngineerRecord(
        date=None,
        building=None,
        floor=None,
        zone=None,
        contractor=None,
        task=None,
        workers=workers,
        raw_text=text,
    )
