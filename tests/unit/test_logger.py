import logging
import os

import allure
import pytest

# Imported as a module: a class named Test* imported directly would be collected as a test class
from src.utils import logger as logger_module


@pytest.mark.bugfix
def test_logger_writes_every_level_once(tmp_path, monkeypatch):
    """Regression: the logger never attached a handler, so nothing was written. Re-creating it doesn't duplicate."""
    monkeypatch.chdir(tmp_path)
    with allure.step("Log DEBUG, INFO, WARNING (with 'Ω') and ERROR lines to logger 'demo'"):
        logger = logger_module.TestLogger("demo")
        logger.debug("debug line")
        logger.info("info line")
        logger.warning("warning with Ω")
        logger.error("error line")
    with allure.step("The file is under results/logs and holds all four levels, including 'Ω' (UTF-8)"):
        assert os.path.normpath(os.path.dirname(logger.log_file)) == os.path.join("results", "logs")
        content = open(logger.log_file, encoding="utf-8").read()
        for line in ("[DEBUG]", "debug line", "[INFO]", "[WARNING]", "warning with Ω", "[ERROR]"):
            assert line in content
    with allure.step("Creating a logger with the same name keeps 2 handlers (not 4); a line is written once"):
        logger_module.TestLogger("same")
        second = logger_module.TestLogger("same")
        second.info("only once")
        assert len(second.logger.handlers) == 2
        assert open(second.log_file, encoding="utf-8").read().count("only once") == 1
    with allure.step("close() releases the file (it can be deleted at once) and unregisters the logger"):
        second.close()
        os.remove(second.log_file)
        assert second.logger.handlers == []
        assert "test_same" not in logging.Logger.manager.loggerDict
