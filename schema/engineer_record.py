from dataclasses import dataclass
from typing import Optional

@dataclass
class EngineerRecord:
    date: Optional[str]
    building: Optional[str]
    floor: Optional[str]
    zone: Optional[str]
    contractor: Optional[str]
    task: Optional[str]
    workers: Optional[int]
    raw_text: str
