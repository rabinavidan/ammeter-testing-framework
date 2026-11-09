import logging
import os
from datetime import datetime


class TestLogger:
    def __init__(self, test_name: str, log_dir: str = "results/logs"):
        self._test_name = test_name
        self._log_dir = os.path.normpath(log_dir)
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        """
        Set up the logger: DEBUG and above to a per-run file, INFO and above to the console
        """
        # Create the log folder
        os.makedirs(self._log_dir, exist_ok=True)

        # File name: timestamp and test ID
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(self._log_dir, f"{timestamp}_{self._test_name}.log")

        # One logger per test run
        logger = logging.getLogger(f"test_{self._test_name}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        # Drop handlers from a previous TestLogger with the same name, so lines aren't duplicated
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

        file_handler = logging.FileHandler(self.log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        return logger

    def info(self, message: str):
        self.logger.info(message)

    def error(self, message: str):
        self.logger.error(message)

    def debug(self, message: str):
        self.logger.debug(message)

    def warning(self, message: str):
        self.logger.warning(message)

    def close(self):
        """Close and detach the handlers, so the log file is released and the logger holds nothing."""
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)
        logging.Logger.manager.loggerDict.pop(self.logger.name, None)
