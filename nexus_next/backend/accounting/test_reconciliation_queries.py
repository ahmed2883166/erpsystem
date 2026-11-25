from django.test import SimpleTestCase

from .models import JournalEntry
from .reconciliation import _collect_ids, _with_reversals


class ReconciliationQueryScalingTests(SimpleTestCase):
    def test_source_journal_ids_remain_database_subqueries(self):
        source = JournalEntry.objects.filter(pk__gt=0)

        linked_ids = _collect_ids([source], [("pk",)])

        self.assertTrue(hasattr(linked_ids, "query"))
        self.assertIn("SELECT", str(linked_ids.query).upper())

    def test_reversal_ids_remain_database_subqueries(self):
        source = JournalEntry.objects.filter(pk__gt=0).values("pk")
        linked_ids = _with_reversals(source)

        self.assertTrue(hasattr(linked_ids, "query"))
        sql = str(linked_ids.query).upper()
        self.assertIn("SELECT", sql)
        self.assertIn("REVERSAL_OF_ID", sql)
