from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    # Large ledger tables may already contain millions of rows. Build indexes
    # concurrently so deployment does not hold a write-blocking table lock.
    atomic = False

    dependencies = [
        ("accounting", "0010_alter_journalentry_source_type"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                        "je_posted_branch_date_idx "
                        "ON accounting_journalentry (company_id, branch_id, entry_date) "
                        "INCLUDE (source_type) "
                        "WHERE status IN ('posted', 'reversed')"
                    ),
                    reverse_sql=(
                        "DROP INDEX CONCURRENTLY IF EXISTS je_posted_branch_date_idx"
                    ),
                ),
                migrations.RunSQL(
                    sql=(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                        "jl_entry_acct_cover_idx "
                        "ON accounting_journalline (entry_id, account_id) "
                        "INCLUDE (debit, credit)"
                    ),
                    reverse_sql=(
                        "DROP INDEX CONCURRENTLY IF EXISTS jl_entry_acct_cover_idx"
                    ),
                ),
                migrations.RunSQL(
                    sql=(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                        "jl_acct_entry_cover_idx "
                        "ON accounting_journalline (account_id, entry_id) "
                        "INCLUDE (debit, credit)"
                    ),
                    reverse_sql=(
                        "DROP INDEX CONCURRENTLY IF EXISTS jl_acct_entry_cover_idx"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="journalentry",
                    index=models.Index(
                        fields=["company", "branch", "entry_date"],
                        include=["source_type"],
                        condition=Q(status__in=("posted", "reversed")),
                        name="je_posted_branch_date_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="journalline",
                    index=models.Index(
                        fields=["entry", "account"],
                        include=["debit", "credit"],
                        name="jl_entry_acct_cover_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="journalline",
                    index=models.Index(
                        fields=["account", "entry"],
                        include=["debit", "credit"],
                        name="jl_acct_entry_cover_idx",
                    ),
                ),
            ],
        ),
    ]
