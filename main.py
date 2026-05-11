from ingest.log_watcher import LogWatcher
from pipeline.processor import Processor

LOG_PATH = "./engineer_messages.log"


def main():
    watcher = LogWatcher(LOG_PATH)
    processor = Processor()

    print("engineer_info_structured watcher started")

    for line in watcher.watch():
        record = processor.process_line(line)
        print("parsed:", record)


if __name__ == "__main__":
    main()
