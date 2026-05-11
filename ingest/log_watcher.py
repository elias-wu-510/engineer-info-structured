import time

class LogWatcher:
    def __init__(self, path: str, sleep: float = 1.0):
        self.path = path
        self.sleep = sleep

    def watch(self):
        with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0,2)
            while True:
                line = f.readline()
                if line:
                    yield line.strip()
                else:
                    time.sleep(self.sleep)
