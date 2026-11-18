import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask

from database import connect_database


class RequestScopedDatabaseTests(unittest.TestCase):
    def test_connections_remain_independent_and_are_closed_with_app_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-scoped.db"
            app = Flask(__name__)
            with app.app_context():
                first = connect_database(path, isolation_level=None)
                second = connect_database(path, isolation_level=None)
                self.assertIsNot(first, second)
                first.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
                first.execute("INSERT INTO probe DEFAULT VALUES")
            for connection in (first, second):
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_connections_outside_app_context_remain_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "independent.db"
            first = connect_database(path, isolation_level=None)
            second = connect_database(path, isolation_level=None)
            try:
                self.assertIsNot(first, second)
            finally:
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()
