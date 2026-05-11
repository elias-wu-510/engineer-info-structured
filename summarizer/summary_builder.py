from typing import List
from schema.engineer_record import EngineerRecord


def build_summary(records: List[EngineerRecord]) -> str:
    total_workers = sum(r.workers or 0 for r in records)
    lines = ["工程信息汇总", ""]

    for r in records:
        lines.append(f"- {r.raw_text}")

    lines.append("")
    lines.append(f"总人数: {total_workers}")

    return "\n".join(lines)
