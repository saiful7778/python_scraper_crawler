import logging
import sys


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    handler = logging.StreamHandler(sys.stdout)
    # handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    return logger
