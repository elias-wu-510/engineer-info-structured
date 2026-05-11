from parser.message_parser import parse_message
from summarizer.summary_builder import build_summary

class Processor:
    def __init__(self):
        self.records = []

    def process_line(self, line: str):
        record = parse_message(line)
        self.records.append(record)
        return record

    def summary(self):
        return build_summary(self.records)
