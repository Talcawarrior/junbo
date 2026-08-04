"""Logging configuration for Junbo."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


class _SafeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            super().emit(record)
        except UnicodeEncodeError:
            try:
                msg = self.format(record)
                stream = self.stream
                enc = getattr(stream, "encoding", None) or "utf-8"
                stream.write(
                    msg.encode(enc, errors="replace").decode(enc, errors="replace")
                    + self.terminator
                )
                self.flush()
            except Exception:
                pass


def setup_logging():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "bot.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if any(
        isinstance(h, (logging.StreamHandler, RotatingFileHandler))
        for h in root_logger.handlers
    ):
        return

    console_handler = _SafeStreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(console_formatter)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(file_formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
