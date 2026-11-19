from app_paths import DB_PATH
from date_formatting import format_export_dates
from database import connect_database, fetch_page, register_database_connection_teardown
from db_compat import (begin_write_transaction, insert_and_get_id, table_exists, table_columns, table_names, execute_script, database_dialect, DB_ERROR, DB_INTEGRITY_ERROR)
from db_sql import execute_compat
"""Employee master, payroll preparation and employee custody workflows."""
import io
import os
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for, g, send_file, send_from_directory, current_app
from werkzeug.utils import secure_filename
from werkzeug.datastructures import FileStorage
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError

# Exception tuples combine the active database driver's errors with workflow errors.
_DB_VALUE_KEY_INTEGRITY_ERRORS = (ValueError, KeyError) + DB_INTEGRITY_ERROR
_DB_OS_VALUE_RUNTIME_ERRORS = (OSError, ValueError, RuntimeError) + DB_ERROR
_DB_VALUE_KEY_ERRORS = (ValueError, KeyError) + DB_ERROR
_DB_VALUE_TYPE_ERRORS = (ValueError, TypeError) + DB_ERROR
_DB_VALUE_ERRORS = (ValueError,) + DB_ERROR
_DB_VALUE_INVALID_KEY_ERRORS = (ValueError, InvalidOperation, KeyError) + DB_ERROR

def _group_concat_expression(connection, expression):
    """Return a dialect-specific aggregate for a fixed internal SQL expression."""
    if database_dialect(connection) == "sqlite":
        return "GROUP_CONCAT(" + expression + ", ', ')"
    return "STRING_AGG(CAST(" + expression + " AS TEXT), ', ')"


def _friday_attendance_predicate(connection):
    """Return the SQL predicate matching Friday for the active DB dialect."""
    if database_dialect(connection) == "sqlite":
        return "strftime('%w',a.attendance_date)='5'"
    return "EXTRACT(DOW FROM CAST(a.attendance_date AS DATE))=5"
from accounting_core import post_journal, safe_error_message, decimal_sum_expression
from code_sequences import next_code
from cash_commitments import create_cash_commitment

workforce_bp = Blueprint("workforce", __name__)


@workforce_bp.record
def _register_database_teardown(state):
    register_database_connection_teardown(state.app)

CENT = Decimal("0.01")


def D(value):
    try:
        result = Decimal(str(value or "0"))
        if not result.is_finite():
            raise InvalidOperation()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("يوجد مبلغ أو عدد ساعات غير صحيح.")


def M(value):
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _db():
    return connect_database(DB_PATH, isolation_level=None)


def initialize_workforce_schema():
    c = _db()
    execute_script(c,"""
    CREATE TABLE IF NOT EXISTS employees(
      id INTEGER PRIMARY KEY, employee_code TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL,
      job_title TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '',
      hire_date TEXT NOT NULL, basic_salary TEXT NOT NULL, standard_hours TEXT NOT NULL DEFAULT '208',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      address TEXT NOT NULL DEFAULT '', national_id TEXT NOT NULL DEFAULT '',
      variable_salary TEXT NOT NULL DEFAULT '0',
      work_status TEXT NOT NULL DEFAULT 'permanent',
      raise_type TEXT NOT NULL DEFAULT 'fixed', raise_value TEXT NOT NULL DEFAULT '0',
      raise_mode TEXT NOT NULL DEFAULT 'basic', raise_effective_date TEXT NOT NULL DEFAULT '',
      overtime_rate_percent TEXT NOT NULL DEFAULT '150',
      branch_id INTEGER, cost_center_id INTEGER, project_id INTEGER, photo_path TEXT NOT NULL DEFAULT '',
      termination_date TEXT NOT NULL DEFAULT '',
      friday_work_eligible INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS employee_pay_items(
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
      item_name TEXT NOT NULL, item_type TEXT NOT NULL CHECK(item_type IN ('allowance','deduction')),
      amount TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS employee_pay_item_catalog(
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, item_type TEXT NOT NULL
        CHECK(item_type IN ('allowance','deduction')),
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(name,item_type)
    );
    CREATE TABLE IF NOT EXISTS salary_change_batches(
      id INTEGER PRIMARY KEY, decision_no TEXT NOT NULL UNIQUE, decision_date TEXT NOT NULL,
      effective_date TEXT NOT NULL, title TEXT NOT NULL, employee_scope TEXT NOT NULL,
      calculation_type TEXT NOT NULL CHECK(calculation_type IN ('fixed','percentage')),
      calculation_base TEXT NOT NULL CHECK(calculation_base IN ('basic','insured_wage')),
      calculation_value TEXT NOT NULL, treatment TEXT NOT NULL CHECK(treatment IN ('basic','allowance')),
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_salary_history(
      id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES salary_change_batches(id),
      employee_id INTEGER NOT NULL REFERENCES employees(id), effective_date TEXT NOT NULL,
      old_basic_salary TEXT NOT NULL, increase_amount TEXT NOT NULL, new_basic_salary TEXT NOT NULL,
      calculation_base_amount TEXT NOT NULL, treatment TEXT NOT NULL,
      allowance_item_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(batch_id,employee_id)
    );
    CREATE INDEX IF NOT EXISTS idx_salary_history_employee_date
      ON employee_salary_history(employee_id,effective_date,id);
    CREATE TABLE IF NOT EXISTS workforce_migrations(
      migration_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_employment_history(
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
      from_status TEXT NOT NULL DEFAULT '', to_status TEXT NOT NULL,
      effective_date TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_end_of_service_settlements(
      id INTEGER PRIMARY KEY, settlement_no TEXT NOT NULL UNIQUE,
      employee_id INTEGER NOT NULL REFERENCES employees(id),
      termination_date TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
      service_start_date TEXT NOT NULL, service_years TEXT NOT NULL DEFAULT '0',
      gross_entitlement TEXT NOT NULL DEFAULT '0', deductions TEXT NOT NULL DEFAULT '0',
      net_payable TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved','paid','cancelled')),
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      approved_by INTEGER, approved_at TEXT, paid_at TEXT,
      UNIQUE(employee_id,termination_date)
    );
    CREATE INDEX IF NOT EXISTS idx_eos_employee_date
      ON employee_end_of_service_settlements(employee_id,termination_date);
    CREATE INDEX IF NOT EXISTS idx_employee_employment_history
      ON employee_employment_history(employee_id,effective_date,id);
    CREATE TABLE IF NOT EXISTS employee_final_payrolls(
      id INTEGER PRIMARY KEY, final_payroll_no TEXT NOT NULL UNIQUE,
      employee_id INTEGER NOT NULL REFERENCES employees(id), period TEXT NOT NULL,
      termination_date TEXT NOT NULL, days_worked INTEGER NOT NULL,
      basic_salary_snapshot TEXT NOT NULL, allowances_snapshot TEXT NOT NULL DEFAULT '0',
      gross_pay TEXT NOT NULL, tax_deduction TEXT NOT NULL DEFAULT '0',
      employee_insurance TEXT NOT NULL DEFAULT '0', employer_insurance TEXT NOT NULL DEFAULT '0',
      stamp_duty TEXT NOT NULL DEFAULT '0', other_deductions TEXT NOT NULL DEFAULT '0',
      net_pay TEXT NOT NULL, journal_entry_id INTEGER UNIQUE, status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','approved','paid','cancelled')),
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(employee_id,termination_date)
    );
    CREATE INDEX IF NOT EXISTS idx_employee_final_payroll_period
      ON employee_final_payrolls(period,employee_id);
    CREATE TABLE IF NOT EXISTS payroll_runs(
      id INTEGER PRIMARY KEY, period TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'draft',
      journal_entry_id INTEGER UNIQUE, created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS payroll_lines(
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES payroll_runs(id) ON DELETE CASCADE,
      employee_id INTEGER NOT NULL REFERENCES employees(id), basic_salary TEXT NOT NULL,
      allowances TEXT NOT NULL, overtime_hours TEXT NOT NULL, overtime_amount TEXT NOT NULL,
      absent_hours TEXT NOT NULL, absence_deduction TEXT NOT NULL, other_deductions TEXT NOT NULL,
      taxable_income_tax TEXT NOT NULL, employee_insurance TEXT NOT NULL,
      employer_insurance TEXT NOT NULL, gross_pay TEXT NOT NULL, net_pay TEXT NOT NULL,
      stamp_duty TEXT NOT NULL DEFAULT '0',
      incentives TEXT NOT NULL DEFAULT '0', advance_deduction TEXT NOT NULL DEFAULT '0',
      penalty_deduction TEXT NOT NULL DEFAULT '0',
      total_entitlements TEXT NOT NULL DEFAULT '0', total_deductions TEXT NOT NULL DEFAULT '0',
      salary_raise TEXT NOT NULL DEFAULT '0', overtime_rate_percent TEXT NOT NULL DEFAULT '150',
      incentive_rule_id INTEGER, incentive_name TEXT NOT NULL DEFAULT '',
      incentive_days TEXT NOT NULL DEFAULT '0', incentive_method TEXT NOT NULL DEFAULT '',
      UNIQUE(run_id,employee_id)
    );
    CREATE TABLE IF NOT EXISTS payroll_line_items(
      id INTEGER PRIMARY KEY,
      payroll_line_id INTEGER NOT NULL REFERENCES payroll_lines(id) ON DELETE CASCADE,
      section TEXT NOT NULL CHECK(section IN ('earning','deduction','information','employer_cost')),
      item_name TEXT NOT NULL, amount TEXT NOT NULL,
      source_type TEXT NOT NULL DEFAULT 'payroll_input', source_id INTEGER,
      sort_order INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_payroll_line_items_line_order
      ON payroll_line_items(payroll_line_id,sort_order,id);
    CREATE TABLE IF NOT EXISTS payroll_payments(
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES payroll_runs(id),
      payment_date TEXT NOT NULL, amount TEXT NOT NULL,
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER NOT NULL UNIQUE, reference TEXT NOT NULL UNIQUE,
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_payroll_payments_run ON payroll_payments(run_id,payment_date,id);
    CREATE TABLE IF NOT EXISTS payroll_reversals(
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL UNIQUE REFERENCES payroll_runs(id),
      reversal_date TEXT NOT NULL, reason TEXT NOT NULL,
      reversal_journal_entry_id INTEGER NOT NULL UNIQUE, created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_loans (
      id INTEGER PRIMARY KEY, loan_no TEXT NOT NULL UNIQUE, employee_id INTEGER NOT NULL REFERENCES employees(id),
      request_date TEXT NOT NULL, requested_amount TEXT NOT NULL, approved_amount TEXT NOT NULL DEFAULT '0',
      installment_count INTEGER NOT NULL DEFAULT 1, first_due_period TEXT NOT NULL,
      remaining_balance TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'requested' CHECK(status IN ('requested','approved','active','closed','rejected')),
      loan_account_id INTEGER REFERENCES accounts(id), disbursement_account_id INTEGER REFERENCES accounts(id),
      disbursement_date TEXT, journal_entry_id INTEGER UNIQUE, notes TEXT NOT NULL DEFAULT '',
      created_by INTEGER, approved_by INTEGER, approved_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_loan_installments (
      id INTEGER PRIMARY KEY, loan_id INTEGER NOT NULL REFERENCES employee_loans(id),
      due_period TEXT NOT NULL, due_amount TEXT NOT NULL, paid_amount TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid')), UNIQUE(loan_id,due_period)
    );
    CREATE TABLE IF NOT EXISTS payroll_loan_allocations (
      id INTEGER PRIMARY KEY, payroll_line_id INTEGER NOT NULL REFERENCES payroll_lines(id),
      installment_id INTEGER NOT NULL REFERENCES employee_loan_installments(id),
      amount TEXT NOT NULL, UNIQUE(payroll_line_id,installment_id)
    );
    CREATE TABLE IF NOT EXISTS payroll_incentive_rules(
      id INTEGER PRIMARY KEY, name TEXT NOT NULL,
      calculation_method TEXT NOT NULL CHECK(calculation_method IN ('fixed','percentage_basic','per_day')),
      calculation_value TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_custodies(
      id INTEGER PRIMARY KEY, custody_no TEXT NOT NULL UNIQUE,
      employee_id INTEGER NOT NULL REFERENCES employees(id), issue_date TEXT NOT NULL,
      amount TEXT NOT NULL, remaining_amount TEXT NOT NULL, description TEXT NOT NULL,
      custody_account_id INTEGER NOT NULL REFERENCES accounts(id),
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER UNIQUE, status TEXT NOT NULL DEFAULT 'open',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS custody_expenses(
      id INTEGER PRIMARY KEY, custody_id INTEGER NOT NULL REFERENCES employee_custodies(id),
      expense_date TEXT NOT NULL, expense_account_id INTEGER NOT NULL REFERENCES accounts(id),
      amount TEXT NOT NULL, description TEXT NOT NULL, receipt_reference TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS attendance_records(
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
      attendance_date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('present','absent','permission','holiday','annual_leave','sick_leave','unpaid_leave')),
      check_in TEXT NOT NULL DEFAULT '', check_out TEXT NOT NULL DEFAULT '',
      absence_hours TEXT NOT NULL DEFAULT '0', approved_overtime_hours TEXT NOT NULL DEFAULT '0',
      notes TEXT NOT NULL DEFAULT '', recorded_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(employee_id,attendance_date)
    );
    CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance_records(attendance_date,employee_id);
    CREATE TABLE IF NOT EXISTS friday_allowance_batches(
      id INTEGER PRIMARY KEY, period TEXT NOT NULL UNIQUE,
      status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','posted')),
      expense_account_id INTEGER NOT NULL REFERENCES accounts(id),
      payable_account_id INTEGER NOT NULL REFERENCES accounts(id),
      total_amount TEXT NOT NULL DEFAULT '0', journal_entry_id INTEGER UNIQUE,
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS friday_allowance_lines(
      id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES friday_allowance_batches(id) ON DELETE CASCADE,
      employee_id INTEGER NOT NULL REFERENCES employees(id),
      employee_code_snapshot TEXT NOT NULL, employee_name_snapshot TEXT NOT NULL,
      basic_salary_snapshot TEXT NOT NULL, attendance_days INTEGER NOT NULL,
      amount TEXT NOT NULL, branch_id INTEGER, cost_center_id INTEGER, project_id INTEGER,
      UNIQUE(batch_id,employee_id)
    );
    CREATE INDEX IF NOT EXISTS idx_friday_allowance_batch ON friday_allowance_lines(batch_id,employee_code_snapshot);
    CREATE TABLE IF NOT EXISTS employee_grant_types(
      id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_grants(
      id INTEGER PRIMARY KEY, grant_type_id INTEGER NOT NULL REFERENCES employee_grant_types(id),
      grant_name_snapshot TEXT NOT NULL,
      calculation_method TEXT NOT NULL CHECK(calculation_method IN ('fixed','percentage_basic','per_day')),
      calculation_value TEXT NOT NULL, days_count TEXT NOT NULL DEFAULT '0',
      expense_account_id INTEGER NOT NULL REFERENCES accounts(id),
      payable_account_id INTEGER NOT NULL REFERENCES accounts(id),
      grant_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','approved','partially_paid','paid')),
      total_amount TEXT NOT NULL, journal_entry_id INTEGER UNIQUE,
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_grant_lines(
      id INTEGER PRIMARY KEY, grant_id INTEGER NOT NULL REFERENCES employee_grants(id) ON DELETE CASCADE,
      employee_id INTEGER NOT NULL REFERENCES employees(id), employee_code_snapshot TEXT NOT NULL,
      employee_name_snapshot TEXT NOT NULL, basic_salary_snapshot TEXT NOT NULL,
      amount TEXT NOT NULL, paid_amount TEXT NOT NULL DEFAULT '0',
      UNIQUE(grant_id,employee_id)
    );
    CREATE TABLE IF NOT EXISTS employee_grant_payments(
      id INTEGER PRIMARY KEY, grant_id INTEGER NOT NULL REFERENCES employee_grants(id),
      payment_date TEXT NOT NULL, amount TEXT NOT NULL,
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER NOT NULL UNIQUE, reference TEXT NOT NULL UNIQUE,
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_employee_grant_lines_grant ON employee_grant_lines(grant_id,id);
    CREATE INDEX IF NOT EXISTS idx_employee_grant_payments_grant ON employee_grant_payments(grant_id,payment_date,id);
    CREATE TABLE IF NOT EXISTS leave_types(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      is_paid INTEGER NOT NULL DEFAULT 1, requires_balance INTEGER NOT NULL DEFAULT 0,
      active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS employee_leave_balances(
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
      leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
      balance_days TEXT NOT NULL DEFAULT '0', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(employee_id,leave_type_id)
    );
    CREATE TABLE IF NOT EXISTS employee_leave_requests(
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
      leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
      start_date TEXT NOT NULL, end_date TEXT NOT NULL, days TEXT NOT NULL,
      reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'requested'
        CHECK(status IN ('requested','approved','rejected')),
      requested_by INTEGER, decided_by INTEGER, decided_at TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_leave_requests_dates ON employee_leave_requests(start_date,end_date,status);
    CREATE INDEX IF NOT EXISTS idx_payroll_period ON payroll_runs(period);
    CREATE INDEX IF NOT EXISTS idx_custody_employee ON employee_custodies(employee_id,status);
    """)
    final_payroll_columns = table_columns(c, "employee_final_payrolls")
    if "journal_entry_id" not in final_payroll_columns:
        execute_compat(c,"ALTER TABLE employee_final_payrolls ADD COLUMN journal_entry_id INTEGER")
    execute_script(c,"""
    CREATE TABLE IF NOT EXISTS employee_final_payroll_payments(
      id INTEGER PRIMARY KEY,
      final_payroll_id INTEGER NOT NULL REFERENCES employee_final_payrolls(id),
      payment_date TEXT NOT NULL, amount TEXT NOT NULL,
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER NOT NULL UNIQUE, reference TEXT NOT NULL UNIQUE,
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_final_payroll_payments
      ON employee_final_payroll_payments(final_payroll_id,payment_date,id);
    """)
    # Backward-compatible additions for the company-specific end-of-service policy.
    eos_columns = table_columns(c, "employee_end_of_service_settlements")
    for column, definition in (
        ("basic_salary_snapshot", "TEXT NOT NULL DEFAULT '0'"),
        ("service_months", "INTEGER NOT NULL DEFAULT 0"),
        ("service_days", "INTEGER NOT NULL DEFAULT 0"),
        ("retirement_age", "INTEGER"),
        ("stamp_duty", "TEXT NOT NULL DEFAULT '0'"),
    ):
        if column not in eos_columns:
            execute_compat(c,f"ALTER TABLE employee_end_of_service_settlements ADD COLUMN {column} {definition}")
    if "journal_entry_id" not in eos_columns:
        execute_compat(c,"ALTER TABLE employee_end_of_service_settlements ADD COLUMN journal_entry_id INTEGER")
    execute_compat(c,"""CREATE UNIQUE INDEX IF NOT EXISTS idx_eos_journal_entry
        ON employee_end_of_service_settlements(journal_entry_id) WHERE journal_entry_id IS NOT NULL""")
    # Additive approval metadata for maker-checker workflows; preserve existing rows.
    for table, additions in {
        "employee_final_payrolls": {
            "approved_by": "INTEGER", "approved_at": "TEXT",
            "approval_status": "TEXT NOT NULL DEFAULT 'pending'",
            "rejected_by": "INTEGER", "rejected_at": "TEXT",
            "rejection_reason": "TEXT NOT NULL DEFAULT ''",
        },
        "employee_grants": {
            "approved_by": "INTEGER", "approved_at": "TEXT",
            "approval_status": "TEXT NOT NULL DEFAULT 'pending'",
            "rejected_by": "INTEGER", "rejected_at": "TEXT",
            "rejection_reason": "TEXT NOT NULL DEFAULT ''",
        },
        "employee_end_of_service_settlements": {
            "approval_status": "TEXT NOT NULL DEFAULT 'pending'",
            "rejected_by": "INTEGER", "rejected_at": "TEXT",
            "rejection_reason": "TEXT NOT NULL DEFAULT ''",
        },
        "employee_loans": {
            "rejection_reason": "TEXT NOT NULL DEFAULT ''",
        },
        "friday_allowance_batches": {
            "approved_by": "INTEGER", "approved_at": "TEXT",
            "approval_reason": "TEXT NOT NULL DEFAULT ''",
            "approval_status": "TEXT NOT NULL DEFAULT 'pending'",
            "rejected_by": "INTEGER", "rejected_at": "TEXT",
            "rejection_reason": "TEXT NOT NULL DEFAULT ''",
        },
    }.items():
        existing_columns = table_columns(c, table)
        for column, declaration in additions.items():
            if column not in existing_columns:
                execute_compat(c,f'ALTER TABLE "{table}" ADD COLUMN "{column}" {declaration}')
    execute_script(c,"""
    CREATE TABLE IF NOT EXISTS employee_end_of_service_payments(
      id INTEGER PRIMARY KEY,
      settlement_id INTEGER NOT NULL REFERENCES employee_end_of_service_settlements(id),
      payment_date TEXT NOT NULL, amount TEXT NOT NULL,
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER NOT NULL UNIQUE, reference TEXT NOT NULL UNIQUE,
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_eos_payments_settlement
      ON employee_end_of_service_payments(settlement_id,payment_date,id);
    """)
    execute_compat(c,"""INSERT INTO employee_pay_item_catalog(name,item_type)
        SELECT DISTINCT item_name,item_type FROM employee_pay_items WHERE trim(item_name)<>''
        ON CONFLICT DO NOTHING""")
    execute_compat(c,"INSERT INTO leave_types(code,name,is_paid,requires_balance) VALUES('annual','إجازة سنوية',1,1) ON CONFLICT DO NOTHING")
    execute_compat(c,"INSERT INTO leave_types(code,name,is_paid,requires_balance) VALUES('unpaid','إجازة بدون أجر',0,0) ON CONFLICT DO NOTHING")
    execute_compat(c,"INSERT INTO leave_types(code,name,is_paid,requires_balance) VALUES('sick','إجازة مرضية',1,0) ON CONFLICT DO NOTHING")
    if database_dialect(c) == "sqlite":
        attendance_sql = execute_compat(c,
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='attendance_records'"
        ).fetchone()
        if attendance_sql and "'annual_leave'" not in (attendance_sql["sql"] or ""):
            execute_compat(c,"PRAGMA foreign_keys=OFF")
            try:
                begin_write_transaction(c)
                execute_compat(c,"""CREATE TABLE attendance_records_rebuilt(
                    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
                    attendance_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('present','absent','permission','holiday','annual_leave','sick_leave','unpaid_leave')),
                    check_in TEXT NOT NULL DEFAULT '', check_out TEXT NOT NULL DEFAULT '',
                    absence_hours TEXT NOT NULL DEFAULT '0', approved_overtime_hours TEXT NOT NULL DEFAULT '0',
                    notes TEXT NOT NULL DEFAULT '', recorded_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(employee_id,attendance_date))""")
                execute_compat(c,"""INSERT INTO attendance_records_rebuilt
                    SELECT id,employee_id,attendance_date,status,check_in,check_out,absence_hours,
                           approved_overtime_hours,notes,recorded_by,created_at FROM attendance_records""")
                execute_compat(c,"DROP TABLE attendance_records")
                execute_compat(c,"ALTER TABLE attendance_records_rebuilt RENAME TO attendance_records")
                execute_compat(c,"CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance_records(attendance_date,employee_id)")
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                execute_compat(c,"PRAGMA foreign_keys=ON")
    elif table_exists(c, "attendance_records"):
        # PostgreSQL stores CHECK constraints separately; replace only an old
        # status constraint and preserve every attendance row and other constraint.
        checks = execute_compat(c,"""
            SELECT conname, pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conrelid='attendance_records'::regclass AND contype='c'
        """).fetchall()
        for check in checks:
            definition = str(check["definition"])
            if (
                "status" in definition.lower()
                and "present" in definition.lower()
                and "absent" in definition.lower()
                and "annual_leave" not in definition
            ):
                constraint = str(check["conname"]).replace('"', '""')
                execute_compat(c,f'ALTER TABLE attendance_records DROP CONSTRAINT "{constraint}"')
        remaining_checks = execute_compat(c,"""
            SELECT pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conrelid='attendance_records'::regclass AND contype='c'
        """).fetchall()
        if not any("status" in str(row["definition"]).lower()
                   and "annual_leave" in str(row["definition"])
                   for row in remaining_checks):
            execute_compat(c,"""ALTER TABLE attendance_records
                ADD CONSTRAINT attendance_records_status_check
                CHECK (status IN ('present','absent','permission','holiday',
                                  'annual_leave','sick_leave','unpaid_leave'))""")
    # Allow a reversed payroll period to be prepared again while keeping only one
    # non-reversed payroll run per period. Preserve run IDs and all linked history.
    if database_dialect(c) == "sqlite":
        payroll_runs_sql = execute_compat(c,
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='payroll_runs'"
        ).fetchone()
        if payroll_runs_sql and "period TEXT NOT NULL UNIQUE" in (payroll_runs_sql["sql"] or ""):
            execute_compat(c,"PRAGMA foreign_keys=OFF")
            try:
                begin_write_transaction(c)
                execute_compat(c,"""CREATE TABLE payroll_runs_rebuilt(
                    id INTEGER PRIMARY KEY, period TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
                    journal_entry_id INTEGER UNIQUE, created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""")
                execute_compat(c,"""INSERT INTO payroll_runs_rebuilt(id,period,status,journal_entry_id,created_by,created_at)
                    SELECT id,period,status,journal_entry_id,created_by,created_at FROM payroll_runs""")
                execute_compat(c,"DROP TABLE payroll_runs")
                execute_compat(c,"ALTER TABLE payroll_runs_rebuilt RENAME TO payroll_runs")
                execute_compat(c,"CREATE UNIQUE INDEX uq_payroll_runs_non_reversed_period ON payroll_runs(period) WHERE status!='reversed'")
                execute_compat(c,"CREATE INDEX IF NOT EXISTS idx_payroll_period ON payroll_runs(period)")
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                execute_compat(c,"PRAGMA foreign_keys=ON")
    elif table_exists(c, "payroll_runs"):
        # Remove the legacy global UNIQUE(period), then enforce uniqueness only
        # for active runs. Duplicate active periods cause a safe migration failure.
        unique_constraints = execute_compat(c,"""
            SELECT conname, pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conrelid='payroll_runs'::regclass AND contype='u'
        """).fetchall()
        for constraint_row in unique_constraints:
            if str(constraint_row["definition"]).replace(" ", "").lower() == "unique(period)":
                constraint = str(constraint_row["conname"]).replace('"', '""')
                execute_compat(c,f'ALTER TABLE payroll_runs DROP CONSTRAINT "{constraint}"')
        execute_compat(c,"""CREATE UNIQUE INDEX IF NOT EXISTS uq_payroll_runs_non_reversed_period
            ON payroll_runs(period) WHERE status <> 'reversed'""")
        execute_compat(c,"CREATE INDEX IF NOT EXISTS idx_payroll_period ON payroll_runs(period)")
    payroll_run_cols = table_columns(c, "payroll_runs")
    for column, declaration in {
        "posted_at": "TEXT",
        "approved_by": "INTEGER",
        "approved_at": "TEXT",
        "approval_status": "TEXT NOT NULL DEFAULT 'pending'",
        "rejected_by": "INTEGER",
        "rejected_at": "TEXT",
        "rejection_reason": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in payroll_run_cols:
            execute_compat(c,f"ALTER TABLE payroll_runs ADD COLUMN {column} {declaration}")
    employee_cols = table_columns(c, "employees")
    for name in ("branch_id", "cost_center_id", "project_id"):
        if name not in employee_cols:
            execute_compat(c,"ALTER TABLE employees ADD COLUMN " + name + " INTEGER")

    for name, definition in {
        "address": "TEXT NOT NULL DEFAULT ''",
        "national_id": "TEXT NOT NULL DEFAULT ''",
        "legal_category": "TEXT NOT NULL DEFAULT 'local'",
        "insurance_number": "TEXT NOT NULL DEFAULT ''",
        "insured_wage": "TEXT NOT NULL DEFAULT '0'",
        "insurance_enabled": "INTEGER NOT NULL DEFAULT 1",
        "variable_salary": "TEXT NOT NULL DEFAULT '0'",
        "work_status": "TEXT NOT NULL DEFAULT 'permanent'",
        "raise_type": "TEXT NOT NULL DEFAULT 'fixed'",
        "raise_value": "TEXT NOT NULL DEFAULT '0'",
        "raise_mode": "TEXT NOT NULL DEFAULT 'basic'",
        "raise_effective_date": "TEXT NOT NULL DEFAULT ''",
        "overtime_rate_percent": "TEXT NOT NULL DEFAULT '150'",
        "photo_path": "TEXT NOT NULL DEFAULT ''",
        "termination_date": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in employee_cols:
            execute_compat(c,"ALTER TABLE employees ADD COLUMN " + name + " " + definition)
    if "friday_work_eligible" not in employee_cols:
        execute_compat(c,"ALTER TABLE employees ADD COLUMN friday_work_eligible INTEGER NOT NULL DEFAULT 0")
    payroll_cols = table_columns(c, "payroll_lines")
    for name, definition in {
        "loan_deduction": "TEXT NOT NULL DEFAULT '0'",
        "stamp_duty": "TEXT NOT NULL DEFAULT '0'",
        "incentives": "TEXT NOT NULL DEFAULT '0'",
        "advance_deduction": "TEXT NOT NULL DEFAULT '0'",
        "penalty_deduction": "TEXT NOT NULL DEFAULT '0'",
        "total_entitlements": "TEXT NOT NULL DEFAULT '0'",
        "total_deductions": "TEXT NOT NULL DEFAULT '0'",
        "salary_raise": "TEXT NOT NULL DEFAULT '0'",
        "overtime_rate_percent": "TEXT NOT NULL DEFAULT '150'",
        "incentive_rule_id": "INTEGER",
        "incentive_name": "TEXT NOT NULL DEFAULT ''",
        "incentive_days": "TEXT NOT NULL DEFAULT '0'",
        "incentive_method": "TEXT NOT NULL DEFAULT ''",
        "overtime_calculated_amount": "TEXT NOT NULL DEFAULT '0'",
        "overtime_manual_override": "INTEGER NOT NULL DEFAULT 0",
        "overtime_override_reason": "TEXT NOT NULL DEFAULT ''",
        "basic_salary_reference": "TEXT NOT NULL DEFAULT '0'",
        "basic_salary_variance_percent": "TEXT NOT NULL DEFAULT '0'",
        "basic_salary_variance_limit_percent": "TEXT NOT NULL DEFAULT '10'",
        "basic_salary_change_reason": "TEXT NOT NULL DEFAULT ''",
        "basic_salary_approval_required": "INTEGER NOT NULL DEFAULT 0",
        "basic_salary_approved_by": "INTEGER",
        "basic_salary_approved_at": "TEXT",
    }.items():
        if name not in payroll_cols:
            execute_compat(c,"ALTER TABLE payroll_lines ADD COLUMN " + name + " " + definition)
    # Backfill legacy payroll runs without changing their original net amounts.
    migration_key = "backfill_payroll_totals_v1"
    already_migrated = execute_compat(c,
        "SELECT 1 FROM workforce_migrations WHERE migration_key=?", (migration_key,)
    ).fetchone()
    if not already_migrated:
        legacy_rows = execute_compat(c,"""SELECT id,gross_pay,employer_insurance,employee_insurance,
            taxable_income_tax,advance_deduction,other_deductions,absence_deduction
            FROM payroll_lines WHERE total_entitlements='0' AND total_deductions='0'""").fetchall()
        for row in legacy_rows:
            employer = D(row["employer_insurance"])
            entitlements = M(D(row["gross_pay"]) + employer)
            deductions = M(employer + D(row["employee_insurance"]) + D(row["taxable_income_tax"])
                           + D(row["advance_deduction"]) + D(row["other_deductions"])
                           + D(row["absence_deduction"]))
            execute_compat(c,"UPDATE payroll_lines SET total_entitlements=?,total_deductions=? WHERE id=?",
                      (str(entitlements),str(deductions),row["id"]))
        execute_compat(c,"INSERT INTO workforce_migrations(migration_key) VALUES(?)", (migration_key,))
    # Add a starting point for employees that existed before the history screen.
    # This is an additive audit snapshot; it does not modify employee or payroll data.
    execute_compat(c,"""INSERT INTO employee_employment_history
        (employee_id,from_status,to_status,effective_date,reason)
        SELECT e.id,'',e.work_status,COALESCE(NULLIF(e.hire_date,''),?),
               'تسجيل الحالة الحالية للملفات السابقة عند تفعيل السجل الوظيفي'
        FROM employees e
        WHERE NOT EXISTS (SELECT 1 FROM employee_employment_history h WHERE h.employee_id=e.id)""",
        (date.today().isoformat(),))
    c.commit()
    c.close()


def _role(*roles, permission=None):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            required = permission if request.method == "POST" and permission else (
                "workforce.manage" if request.method == "POST" else "workforce.view"
            )
            if "permissions" in session:
                allowed = required in set(session["permissions"])
            else:
                allowed = session.get("role") in roles
            if not allowed:
                flash("لا تملك صلاحية تنفيذ هذه العملية.", "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapped
    return deco


def _audit(c, action, details):
    if table_exists(c, "audit_log"):
        execute_compat(c, "INSERT INTO audit_log(user_id,action,details,created_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                  (session.get("user_id"), action, details))


def _accounts(c):
    return execute_compat(c, "SELECT id,code,name_ar,account_type FROM accounts WHERE active=1 AND is_group=0 ORDER BY code").fetchall()


def _employee_advance_account(c, account_id):
    account = execute_compat(c, """SELECT id,account_type,is_group,active FROM accounts
        WHERE id=?""", (account_id,)).fetchone()
    if not account or not account["active"] or account["is_group"] or account["account_type"] != "asset":
        raise ValueError("اختر حساب سلف موظفين تفصيليًا ونشطًا من الأصول.")
    if not table_exists(c, "cash_accounts"):
        raise ValueError("سجل الخزائن والبنوك غير مهيأ؛ لا يمكن التحقق من حساب السلف.")
    if execute_compat(c, "SELECT 1 FROM cash_accounts WHERE account_id=?", (account_id,)).fetchone():
        raise ValueError("حساب سلف الموظفين لا يجوز أن يكون حساب بنك أو صندوق.")
    return account["id"]


def _cash_or_bank_account(c, account_id):
    account = execute_compat(c, """SELECT id,account_type,is_group,active FROM accounts
        WHERE id=?""", (account_id,)).fetchone()
    if not account or not account["active"] or account["is_group"] or account["account_type"] != "asset":
        raise ValueError("اختر حساب بنك أو صندوق تفصيليًا ونشطًا.")
    if not table_exists(c, "cash_accounts"):
        raise ValueError("سجل الخزائن والبنوك غير مهيأ؛ لا يمكن التحقق من حساب الصرف.")
    if not execute_compat(c, "SELECT 1 FROM cash_accounts WHERE account_id=? AND active=1", (account_id,)).fetchone():
        raise ValueError("الحساب المختار غير مسجل كخزينة أو بنك نشط.")
    return account["id"]


def _employee_salary_basis(c, employee_id, as_of_date=None):
    """Use the latest posted payroll snapshot, unless a newer approved salary decision supersedes it."""
    employee = execute_compat(c, "SELECT basic_salary,insured_wage FROM employees WHERE id=?",
                          (employee_id,)).fetchone()
    if not employee:
        raise ValueError("الموظف غير موجود عند تحديد أساس الأجر.")
    effective_date = as_of_date or date.today().isoformat()
    payroll = execute_compat(c, """
        SELECT l.basic_salary,l.insured_wage,COALESCE(r.posted_at,r.created_at,'') AS source_timestamp
        FROM payroll_lines l JOIN payroll_runs r ON r.id=l.run_id
        WHERE l.employee_id=? AND r.status='posted' AND r.period<=?
        ORDER BY r.period DESC,COALESCE(r.posted_at,r.created_at) DESC,r.id DESC LIMIT 1
    """,(employee_id,effective_date[:7])).fetchone()
    history = execute_compat(c, """
        SELECT h.new_basic_salary,b.created_at
        FROM employee_salary_history h JOIN salary_change_batches b ON b.id=h.batch_id
        WHERE h.employee_id=? AND h.effective_date<=?
        ORDER BY h.effective_date DESC,b.created_at DESC,h.id DESC LIMIT 1
    """,(employee_id,effective_date)).fetchone()
    if payroll:
        basic = D(payroll["basic_salary"])
        if history and (history["created_at"] or "") > (payroll["source_timestamp"] or ""):
            basic = D(history["new_basic_salary"])
        insured = D(payroll["insured_wage"] or employee["insured_wage"] or "0")
    elif history:
        basic, insured = D(history["new_basic_salary"]), D(employee["insured_wage"] or "0")
    else:
        basic, insured = D(employee["basic_salary"] or "0"), D(employee["insured_wage"] or "0")
    return M(basic), M(insured)


def _dimension_options(c):
    tables = table_names(c)
    return {
        key: execute_compat(c, f"SELECT id,code,name_ar FROM {table} WHERE active=1 ORDER BY code").fetchall()
        if table in tables else []
        for key, table in (("branches","branches"),("cost_centers","cost_centers"),("projects","projects"))
    }


def _validate_employee_dimensions(c, values):
    # The final five values are branch, cost center, project, termination date, and Friday eligibility.
    # Validate only the three dimension IDs; the termination date and flag are not project IDs.
    for value, table, label in zip(values[-5:-2], ("branches","cost_centers","projects"),
                                   ("الفرع","مركز التكلفة","المشروع")):
        if value is None:
            continue
        if not table_exists(c, table):
            raise ValueError("جدول " + label + " غير مهيأ.")
        if not execute_compat(c, f"SELECT 1 FROM {table} WHERE id=? AND active=1", (value,)).fetchone():
            raise ValueError("اختر " + label + " نشطًا وصحيحًا.")


def _employee_form_values(form):
    # The employee master captures the initial basic and insured wages.
    # Monthly payroll lines keep their own snapshots, so later profile changes
    # cannot rewrite already prepared or posted payroll/accounting history.
    salary = M(D(form.get("basic_salary", "0")))
    variable_salary = Decimal("0")
    hours = D(form.get("standard_hours", "208"))
    insured_wage = M(D(form.get("insured_wage", "0")))
    legal_category = form.get("legal_category", "local").strip().lower()
    work_status = form.get("work_status", "permanent").strip().lower()
    raise_type = "fixed"
    raise_value = Decimal("0")
    raise_mode = "basic"
    overtime_rate = D("150")
    effective_date = ""
    termination_date = form.get("termination_date", "").strip()
    if effective_date:
        date.fromisoformat(effective_date)
    if termination_date:
        date.fromisoformat(termination_date)
    if work_status in {"resigned", "retired"} and not termination_date:
        raise ValueError("يجب تحديد تاريخ سريان الاستقالة أو بلوغ المعاش.")
    if work_status not in {"resigned", "retired"}:
        termination_date = ""
    if (salary < 0 or insured_wage < 0):
        raise ValueError("الأجر الأساسي والأجر التأميني لا يجوز أن يكونا سالبين.")
    if (hours <= 0 or not legal_category or
        work_status not in {"comprehensive_bonus", "permanent", "temporary", "unpaid_leave", "resigned", "retired"}):
        raise ValueError("راجع الأجر الأساسي والأجر التأميني وساعات العمل والحالة الوظيفية.")
    return (
        form.get("employee_code", "").strip(), form["full_name"].strip(),
        form.get("job_title", "").strip(), form.get("department", "").strip(),
        form.get("hire_date") or date.today().isoformat(), str(salary), str(hours), str(variable_salary),
        legal_category, form.get("insurance_number", "").strip(), str(insured_wage),
        int(form.get("insurance_enabled", "1") == "1"),
        form.get("address", "").strip(), form.get("national_id", "").strip(),
        work_status, raise_type, str(raise_value), raise_mode, effective_date,
        str(overtime_rate),
        form.get("branch_id") or None, form.get("cost_center_id") or None,
        form.get("project_id") or None, termination_date,
        int(form.get("friday_work_eligible", "0") == "1"),
    )




def _employee_photo_directory():
    directory = Path(current_app.instance_path) / "uploads" / "employees"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _save_employee_photo(upload):
    if not upload or not upload.filename:
        return None
    extension = os.path.splitext(secure_filename(upload.filename))[1].lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValueError("صورة الموظف يجب أن تكون JPG أو PNG أو WEBP.")
    content = upload.stream.read(5 * 1024 * 1024 + 1)
    if not content or len(content) > 5 * 1024 * 1024:
        raise ValueError("حجم صورة الموظف يجب ألا يتجاوز 5 ميجابايت.")
    try:
        from io import BytesIO
        with Image.open(BytesIO(content)) as probe:
            if probe.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("محتوى الملف ليس صورة JPG أو PNG أو WEBP سليمة.")
            probe.verify()
        with Image.open(BytesIO(content)) as source:
            if source.width * source.height > 20_000_000:
                raise ValueError("أبعاد صورة الموظف أكبر من الحد المسموح.")
            normalized = ImageOps.exif_transpose(source).convert("RGB")
            normalized.thumbnail((2400, 2400))
            filename = uuid.uuid4().hex + ".jpg"
            normalized.save(_employee_photo_directory() / filename, format="JPEG", quality=88, optimize=True)
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("تعذر قراءة الصورة؛ اختر ملف صورة حقيقيًا وسليمًا.") from exc
    return "employees/" + filename


def _delete_employee_photo(relative_path):
    if not relative_path:
        return
    normalized = str(relative_path).replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) == 2 and parts[0] == "employees" and parts[1].endswith(".jpg"):
        directory = _employee_photo_directory().resolve()
        target = (directory / parts[1]).resolve()
    elif (len(parts) == 3 and parts[0] == "uploads" and parts[1] == "employees"
          and parts[2] == Path(parts[2]).name):
        directory = (Path(current_app.static_folder) / "uploads" / "employees").resolve()
        target = (directory / parts[2]).resolve()
    else:
        return
    if target.parent == directory:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            current_app.logger.warning("Could not remove replaced employee photo: %s", target.name)

def _salary_change_preview(c, form, allow_future=False):
    decision_no = form.get("decision_no", "").strip()
    decision_date = form.get("decision_date", "").strip()
    effective_date = form.get("effective_date", "").strip()
    title = form.get("title", "").strip()
    scope_values = form.getlist("employee_scopes") if hasattr(form, "getlist") else form.get("employee_scopes", [])
    if isinstance(scope_values, str):
        scope_values = [scope_values]
    selected_scopes = sorted(set(x.strip() for x in scope_values if str(x).strip()))
    if not selected_scopes:
        legacy_scope = form.get("employee_scope", "all_active").strip()
        selected_scopes = [legacy_scope]
    allowed_scopes = {"permanent","temporary","comprehensive_bonus","unpaid_leave"}
    if "all_active" in selected_scopes:
        selected_scopes = ["all_active"]
    if any(x not in allowed_scopes | {"all_active"} for x in selected_scopes):
        raise ValueError("حدد فئة واحدة على الأقل من الفئات المتاحة.")
    scope = ",".join(selected_scopes)
    calc_type = form.get("calculation_type", "fixed").strip()
    calc_base = form.get("calculation_base", "basic").strip()
    treatment = form.get("treatment", "basic").strip()
    value = M(form.get("calculation_value", "0"))
    date.fromisoformat(decision_date)
    date.fromisoformat(effective_date)
    if date.fromisoformat(effective_date) > date.today() and not allow_future:
        raise ValueError("لا يمكن اعتماد القرار قبل تاريخ سريانه. يمكنك معاينته الآن ثم اعتماده في يوم السريان أو بعده حتى لا تُصرف الزيادة مبكرًا.")
    if (not decision_no or not title or value <= 0
        or not selected_scopes
        or calc_type not in {"fixed","percentage"} or calc_base not in {"basic","insured_wage"}
        or treatment not in {"basic","allowance"}):
        raise ValueError("أكمل رقم القرار واسمه وتواريخه وحدد نطاق الموظفين وقاعدة الحساب والمعالجة وقيمة موجبة.")
    where = "active=1 AND work_status NOT IN ('resigned','retired')"
    params = []
    if "all_active" not in selected_scopes:
        marks = ",".join("?" for _ in selected_scopes)
        where += " AND work_status IN (" + marks + ")"
        params.extend(selected_scopes)
    employees = execute_compat(c, "SELECT id,employee_code,full_name,basic_salary,insured_wage,work_status FROM employees WHERE "
                          + where + " ORDER BY employee_code", params).fetchall()
    if not employees:
        raise ValueError("لا يوجد موظفون نشطون ضمن الفئة المحددة.")
    preview = []
    for emp in employees:
        current_basic, current_insured = _employee_salary_basis(c, emp["id"], effective_date)
        old_basic = current_basic
        basis = old_basic if calc_base == "basic" else current_insured
        amount = M(basis * value / Decimal("100")) if calc_type == "percentage" else value
        new_basic = M(old_basic + amount) if treatment == "basic" else old_basic
        if amount <= 0:
            raise ValueError("نتجت زيادة صفرية للموظف " + emp["full_name"] + ". راجع أجر أساس الحساب.")
        preview.append({"id":emp["id"],"code":emp["employee_code"],"name":emp["full_name"],
                        "old":old_basic,"basis":basis,"amount":amount,"new":new_basic,
                        "status":emp["work_status"]})
    return {"decision_no":decision_no,"decision_date":decision_date,"effective_date":effective_date,
            "title":title,"scope":scope,"scopes":selected_scopes,"calc_type":calc_type,"calc_base":calc_base,
            "value":value,"treatment":treatment,"notes":form.get("notes","").strip(),
            "employees":preview}


@workforce_bp.route("/hr/salary-changes", methods=["GET","POST"])
@_role("admin","accountant")
def salary_changes():
    c = _db()
    preview = None
    error = ""
    if request.method == "POST":
        try:
            preview = _salary_change_preview(c, request.form, allow_future=request.form.get("confirm") != "1")
            if request.form.get("confirm") == "1":
                begin_write_transaction(c)
                batch_id = insert_and_get_id(c, """INSERT INTO salary_change_batches(decision_no,decision_date,effective_date,title,
                    employee_scope,calculation_type,calculation_base,calculation_value,treatment,notes,created_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (preview["decision_no"],preview["decision_date"],preview["effective_date"],preview["title"],
                     preview["scope"],preview["calc_type"],preview["calc_base"],str(preview["value"]),
                     preview["treatment"],preview["notes"],session.get("user_id")))
                for item in preview["employees"]:
                    allowance_item_id = None
                    if preview["treatment"] == "basic":
                        execute_compat(c, "UPDATE employees SET basic_salary=? WHERE id=?",
                                  (str(item["new"]),item["id"]))
                        # Clear legacy one-off raise settings to prevent re-applying this increase in payroll.
                        execute_compat(c, """UPDATE employees SET raise_type='fixed',raise_value='0',
                            raise_mode='basic',raise_effective_date='' WHERE id=?""",(item["id"],))
                    else:
                        allowance_item_id = insert_and_get_id(c, """INSERT INTO employee_pay_items(employee_id,item_name,item_type,amount,active)
                            VALUES(?,?,'allowance',?,1)""",
                            (item["id"],preview["title"]+" ("+preview["decision_no"]+")",str(item["amount"])))
                    execute_compat(c, """INSERT INTO employee_salary_history(batch_id,employee_id,effective_date,
                        old_basic_salary,increase_amount,new_basic_salary,calculation_base_amount,treatment,allowance_item_id)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (batch_id,item["id"],preview["effective_date"],str(item["old"]),str(item["amount"]),
                         str(item["new"]),str(item["basis"]),preview["treatment"],allowance_item_id))
                _audit(c,"salary_change_batch_approved",
                       preview["decision_no"]+" | "+preview["title"]+" | employees="+str(len(preview["employees"])))
                c.commit()
                flash("تم اعتماد القرار وتحديث "+str(len(preview["employees"]))+" موظفًا، وحفظ سجل تاريخي لكل موظف.","success")
                c.close()
                return redirect(url_for("workforce.salary_changes"))
        except _DB_VALUE_KEY_INTEGRITY_ERRORS as exc:
            c.rollback()
            error = str(exc) or "تعذر حفظ قرار الزيادة. تأكد من عدم تكرار رقم القرار."
            preview = None
    batches = execute_compat(c, """SELECT b.*,COUNT(h.id) employee_count
        FROM salary_change_batches b LEFT JOIN employee_salary_history h ON h.batch_id=b.id
        GROUP BY b.id ORDER BY b.effective_date DESC,b.id DESC LIMIT 100""").fetchall()
    c.close()
    return render_template("salary_changes.html",preview=preview,error=error,batches=batches,today=date.today().isoformat())


@workforce_bp.route("/hr/salary-history", methods=["GET"])
@_role("admin","accountant")
def salary_history():
    c = _db()
    employee_id = request.args.get("employee_id",type=int)
    employees = execute_compat(c, "SELECT id,employee_code,full_name FROM employees ORDER BY employee_code").fetchall()
    where, params = ("WHERE h.employee_id=?","") if employee_id else ("","")
    if employee_id:
        params = (employee_id,)
    rows = execute_compat(c, """SELECT h.*,e.employee_code,e.full_name,b.decision_no,b.title,b.calculation_type,
        b.calculation_base,b.calculation_value,b.decision_date,b.notes
        FROM employee_salary_history h JOIN employees e ON e.id=h.employee_id
        JOIN salary_change_batches b ON b.id=h.batch_id """+where+
        " ORDER BY h.effective_date DESC,e.employee_code,h.id DESC",params).fetchall()
    c.close()
    return render_template("salary_history.html",employees=employees,rows=rows,selected_employee=employee_id)


@workforce_bp.before_app_request
def _block_public_employee_photo_files():
    # Legacy uploads may still exist under static/. They must never be served directly.
    if request.path.startswith("/static/uploads/employees/"):
        return "Not found", 404


@workforce_bp.route("/hr/<int:employee_id>/photo", methods=["GET"])
@_role("admin", "accountant", "viewer")
def employee_photo(employee_id):
    c = _db()
    employee = execute_compat(c, "SELECT photo_path FROM employees WHERE id=?", (employee_id,)).fetchone()
    c.close()
    if not employee or not employee["photo_path"]:
        return "Not found", 404
    relative = str(employee["photo_path"]).replace("\\", "/")
    parts = relative.split("/")
    if (len(parts) == 3 and parts[0] == "uploads" and parts[1] == "employees"
            and parts[2] and parts[2] == Path(parts[2]).name):
        legacy_file = Path(current_app.static_folder) / "uploads" / "employees" / parts[2]
        if legacy_file.is_file():
            migrated_path = None
            try:
                with legacy_file.open("rb") as old_stream:
                    migrated_path = _save_employee_photo(FileStorage(stream=old_stream, filename=parts[2]))
                migration_db = _db()
                try:
                    migration_cursor = migration_db.execute("UPDATE employees SET photo_path=? WHERE id=? AND photo_path=?",
                                         (migrated_path, employee_id, relative))
                    if migration_cursor.rowcount != 1:
                        raise RuntimeError("Employee photo reference changed during migration.")
                    migration_db.commit()
                except Exception:
                    migration_db.rollback()
                    _delete_employee_photo(migrated_path)
                    raise
                finally:
                    migration_db.close()
                try:
                    legacy_file.unlink(missing_ok=True)
                except OSError:
                    current_app.logger.warning("Could not remove migrated public employee photo: %s", legacy_file.name)
                relative = migrated_path
                parts = relative.split("/")
            except _DB_OS_VALUE_RUNTIME_ERRORS:
                return "Not found", 404
    if len(parts) != 2 or parts[0] != "employees" or not parts[1].endswith(".jpg"):
        return "Not found", 404
    response = send_from_directory(_employee_photo_directory(), parts[1], mimetype="image/jpeg", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@workforce_bp.route("/hr", methods=["GET", "POST"])
@_role("admin", "accountant")
def employees():
    c = _db()
    if request.method == "POST":
        photo_path = None
        try:
            values = _employee_form_values(request.form)
            values = (next_code(c, "employee", "EMP", "employees", "employee_code"), *values[1:])
            _validate_employee_dimensions(c, values)
            photo_path = _save_employee_photo(request.files.get("photo"))
            employee_id = insert_and_get_id(c, """INSERT INTO employees(employee_code,full_name,job_title,department,hire_date,basic_salary,
                standard_hours,variable_salary,legal_category,insurance_number,insured_wage,insurance_enabled,address,national_id,
                work_status,raise_type,raise_value,raise_mode,raise_effective_date,overtime_rate_percent,
                branch_id,cost_center_id,project_id,termination_date,friday_work_eligible,photo_path)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*values, photo_path or ""))
            execute_compat(c, """INSERT INTO employee_employment_history
                (employee_id,from_status,to_status,effective_date,reason,created_by)
                VALUES(?,?,?,?,?,?)""",(employee_id,"",values[14],values[23] or values[4],"تسجيل الحالة عند إنشاء ملف الموظف",session.get("user_id")))
            _audit(c, "employee_created", values[0])
            c.commit()
            flash("تم إضافة الموظف وحفظ بياناته الوظيفية والأجرية.", "success")
        except _DB_VALUE_KEY_INTEGRITY_ERRORS as e:
            c.rollback()
            _delete_employee_photo(photo_path)
            flash(str(e) or "كود الموظف أو الرقم القومي مستخدم بالفعل.", "error")
        c.close()
        return redirect(url_for("workforce.employees"))
    employees_page = fetch_page(c, "SELECT * FROM employees ORDER BY employee_code",
        "SELECT COUNT(*) FROM employees",
        page=request.args.get("employees_page",1,type=int),
        per_page=request.args.get("employees_per_page",50,type=int))
    rows = employees_page["rows"]
    employee_rows = []
    for row in rows:
        item = dict(row)
        item["current_basic_salary"], item["current_insured_wage"] = _employee_salary_basis(
            c, item["id"], date.today().isoformat()
        )
        employee_rows.append(item)
    dimensions = _dimension_options(c)
    c.close()
    return render_template("employees.html", employees=employee_rows,
                           employees_page=employees_page, dimensions=dimensions)


@workforce_bp.route("/hr/<int:employee_id>/edit", methods=["GET", "POST"])
@_role("admin", "accountant")
def edit_employee(employee_id):
    c = _db()
    employee = execute_compat(c, "SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not employee:
        c.close()
        flash("الموظف غير موجود.", "error")
        return redirect(url_for("workforce.employees"))
    if request.method == "POST":
        new_photo_path = None
        try:
            values = list(_employee_form_values(request.form))
            # Preserve legacy payroll profile columns during profile edits; they are no longer editable here.
            for index, column in ((5,"basic_salary"),(7,"variable_salary"),
                                  (8,"legal_category"),(10,"insured_wage"),(11,"insurance_enabled"),
                                  (15,"raise_type"),(16,"raise_value"),(17,"raise_mode"),
                                  (18,"raise_effective_date"),(19,"overtime_rate_percent")):
                values[index] = employee[column]
            values = tuple(values)
            _validate_employee_dimensions(c, values)
            old_work_status = employee["work_status"]
            old_termination_date = employee["termination_date"] or ""
            new_photo_path = _save_employee_photo(request.files.get("photo"))
            old_photo_path = employee["photo_path"]
            photo_path = new_photo_path
            if photo_path:
                execute_compat(c, """UPDATE employees SET employee_code=?,full_name=?,job_title=?,department=?,hire_date=?,
                    basic_salary=?,standard_hours=?,variable_salary=?,legal_category=?,insurance_number=?,insured_wage=?,insurance_enabled=?,
                    address=?,national_id=?,work_status=?,raise_type=?,raise_value=?,raise_mode=?,
                    raise_effective_date=?,overtime_rate_percent=?,branch_id=?,cost_center_id=?,project_id=?,termination_date=?,friday_work_eligible=?,photo_path=? WHERE id=?""",
                    (*values, photo_path, employee_id))
            else:
                execute_compat(c, """UPDATE employees SET employee_code=?,full_name=?,job_title=?,department=?,hire_date=?,
                    basic_salary=?,standard_hours=?,variable_salary=?,legal_category=?,insurance_number=?,insured_wage=?,insurance_enabled=?,
                    address=?,national_id=?,work_status=?,raise_type=?,raise_value=?,raise_mode=?,
                    raise_effective_date=?,overtime_rate_percent=?,branch_id=?,cost_center_id=?,project_id=?,termination_date=?,friday_work_eligible=? WHERE id=?""",
                    (*values, employee_id))
            if old_work_status != values[14] or old_termination_date != (values[23] or ""):
                effective_date = values[23] or date.today().isoformat()
                reason = request.form.get("employment_change_reason", "").strip() or "تعديل الحالة من ملف الموظف"
                execute_compat(c, """INSERT INTO employee_employment_history
                    (employee_id,from_status,to_status,effective_date,reason,created_by)
                    VALUES(?,?,?,?,?,?)""",(employee_id,old_work_status,values[14],effective_date,reason,session.get("user_id")))
            _audit(c, "employee_updated", values[0])
            c.commit()
            if new_photo_path:
                _delete_employee_photo(old_photo_path)
            flash("تم تحديث ملف الموظف.", "success")
            c.close()
            return redirect(url_for("workforce.employees"))
        except _DB_VALUE_KEY_INTEGRITY_ERRORS as exc:
            c.rollback()
            _delete_employee_photo(new_photo_path)
            flash(str(exc) or "تعذر تحديث بيانات الموظف.", "error")
    current_basic_salary, current_insured_wage = _employee_salary_basis(
        c, employee_id, date.today().isoformat()
    )
    dimensions = _dimension_options(c)
    c.close()
    return render_template("employee_edit.html", e=employee, dimensions=dimensions,
                           current_basic_salary=current_basic_salary,
                           current_insured_wage=current_insured_wage)


@workforce_bp.route("/hr/<int:employee_id>/employment-history", methods=["GET", "POST"])
@_role("admin", "accountant")
def employee_employment_history(employee_id):
    c = _db()
    employee = execute_compat(c, "SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not employee:
        c.close()
        flash("الموظف غير موجود.", "error")
        return redirect(url_for("workforce.employees"))
    if request.method == "POST":
        try:
            status = request.form.get("work_status", "").strip().lower()
            effective_date = date.fromisoformat(request.form.get("effective_date", "")).isoformat()
            reason = request.form.get("reason", "").strip()
            allowed = {"permanent", "temporary", "comprehensive_bonus", "unpaid_leave", "resigned", "retired"}
            if status not in allowed or not reason:
                raise ValueError("اختر حالة وظيفية صحيحة واكتب سبب التغيير.")
            termination_date = effective_date if status in {"resigned", "retired"} else ""
            begin_write_transaction(c)
            current = execute_compat(c, "SELECT work_status,termination_date,hire_date FROM employees WHERE id=?", (employee_id,)).fetchone()
            if not current:
                raise ValueError("الموظف غير موجود.")
            if effective_date < (current["hire_date"] or effective_date):
                raise ValueError("تاريخ التغيير لا يمكن أن يسبق تاريخ تعيين الموظف.")
            latest = execute_compat(c, """SELECT effective_date FROM employee_employment_history
                WHERE employee_id=? ORDER BY effective_date DESC,id DESC LIMIT 1""", (employee_id,)).fetchone()
            if latest and effective_date < latest["effective_date"]:
                raise ValueError("تاريخ التغيير أقدم من آخر حركة وظيفية مسجلة؛ راجع السجل قبل الإضافة.")
            if current["work_status"] == status and (current["termination_date"] or "") == termination_date:
                raise ValueError("الحالة المختارة مطابقة للحالة الحالية بالفعل.")
            execute_compat(c, "UPDATE employees SET work_status=?,termination_date=? WHERE id=?",
                      (status,termination_date,employee_id))
            execute_compat(c, """INSERT INTO employee_employment_history
                (employee_id,from_status,to_status,effective_date,reason,created_by)
                VALUES(?,?,?,?,?,?)""",(employee_id,current["work_status"],status,effective_date,reason,session.get("user_id")))
            _audit(c,"employee_employment_status_changed",str(employee["employee_code"])+": "+str(current["work_status"])+" -> "+status+"; "+effective_date)
            c.commit()
            flash("تم تسجيل التغيير الوظيفي مع الاحتفاظ بالحالة السابقة وسجل المرتبات.", "success")
            c.close()
            return redirect(url_for("workforce.employee_employment_history", employee_id=employee_id))
        except _DB_VALUE_TYPE_ERRORS as exc:
            c.rollback()
            flash(str(exc) or "تعذر تسجيل التغيير الوظيفي.", "error")
    history = execute_compat(c, """SELECT * FROM employee_employment_history
        WHERE employee_id=? ORDER BY effective_date DESC,id DESC""", (employee_id,)).fetchall()
    c.close()
    return render_template("employee_employment_history.html", employee=employee, history=history,
        today=date.today().isoformat())


@workforce_bp.route("/hr/<int:employee_id>/form-2-salary", methods=["GET"])
@_role("admin", "accountant", "viewer")
def employee_form_2_salary(employee_id):
    """Keep old employee-specific links working by opening the matching group report."""
    c = _db()
    employee = execute_compat(c, "SELECT work_status FROM employees WHERE id=?", (employee_id,)).fetchone()
    c.close()
    if not employee:
        flash("الموظف غير موجود.", "error")
        return redirect(url_for("workforce.employees"))
    scope = employee["work_status"] if employee["work_status"] in {
        "permanent", "temporary", "comprehensive_bonus"
    } else "all"
    return redirect(url_for("workforce.employee_form_2_salary_report", scope=scope))


@workforce_bp.route("/hr/form-2-salary-report", methods=["GET"])
@_role("admin", "accountant", "viewer")
def employee_form_2_salary_report():
    """Printable group report for employee wage/insurance data by employment category."""
    scope = request.args.get("scope", "all")
    scope_statuses = {
        "all": ("permanent", "temporary", "comprehensive_bonus"),
        "permanent": ("permanent",),
        "temporary": ("temporary",),
        "comprehensive_bonus": ("comprehensive_bonus",),
    }
    if scope not in scope_statuses:
        scope = "all"
    statuses = scope_statuses[scope]
    placeholders = ",".join("?" for _ in statuses)
    c = _db()
    employee_rows = execute_compat(c, 
        f"""SELECT id,employee_code,full_name,national_id,insurance_number,hire_date,
                   basic_salary,variable_salary,insured_wage
            FROM employees WHERE work_status IN ({placeholders})
            ORDER BY employee_code,full_name""", statuses
    ).fetchall()
    employees = []
    as_of_date = date.today().isoformat()
    for employee_row in employee_rows:
        item = dict(employee_row)
        basic, insured = _employee_salary_basis(c, item["id"], as_of_date)
        latest_payroll = execute_compat(c, """SELECT l.allowances FROM payroll_lines l
            JOIN payroll_runs r ON r.id=l.run_id
            WHERE l.employee_id=? AND r.status='posted' AND r.period<=?
            ORDER BY r.period DESC,COALESCE(r.posted_at,r.created_at) DESC,r.id DESC LIMIT 1""",
            (item["id"],as_of_date[:7])).fetchone()
        variable = M(latest_payroll["allowances"]) if latest_payroll else M(item["variable_salary"])
        item["basic_salary"] = str(basic)
        item["variable_salary"] = str(variable)
        item["insured_wage"] = str(insured)
        item["comprehensive_wage"] = str(M(basic+variable))
        employees.append(item)
    c.close()
    totals = {
        "count": len(employees),
        "comprehensive_wage": M(sum((D(e["comprehensive_wage"]) for e in employees), Decimal("0"))),
        "insured_wage": M(sum((D(e["insured_wage"]) for e in employees), Decimal("0"))),
    }
    labels = {"all": "كل الفئات", "permanent": "الموظفون المثبتون",
              "temporary": "الموظفون المؤقتون", "comprehensive_bonus": "موظفو المكافأة الشاملة"}
    return render_template("form2_salary.html", employees=employees, totals=totals,
                           scope=scope, scope_label=labels[scope],
                           print_date=date.today().isoformat())


@workforce_bp.route("/hr/import-excel", methods=["POST"])
@_role("admin", "accountant")
def import_employees_excel():
    """Retire the legacy no-preview importer; use the unified mapping/preview wizard."""
    flash("تم إيقاف الاستيراد القديم. استخدم معالج استيراد البيانات لمعاينة الأعمدة ومراجعة الصفوف قبل التأكيد.", "warning")
    return redirect(url_for("import_data_wizard", dataset="employees"))


@workforce_bp.route("/hr/export-excel", methods=["GET"])
@_role("admin", "accountant", "viewer")
def export_employees_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    c = _db()
    rows = execute_compat(c, """SELECT employee_code,full_name,address,national_id,insurance_number,
        basic_salary,insured_wage,work_status,active,termination_date,job_title,department,hire_date
        FROM employees ORDER BY employee_code""").fetchall()
    c.close()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Employees"
    headers = ["كود الموظف","اسم الموظف","العنوان","الرقم القومي","رقم التأمين",
               "الأجر الأساسي","الأجر التأميني","الحالة الوظيفية","نشط","تاريخ انتهاء الخدمة",
               "الوظيفة","الإدارة","تاريخ التعيين"]
    sheet.append(headers)
    status_labels = {"permanent":"مثبت","temporary":"مؤقت","comprehensive_bonus":"مكافأة شاملة",
                     "unpaid_leave":"إجازة بدون مرتب","resigned":"استقالة","retired":"معاش"}
    for row in rows:
        sheet.append([row["employee_code"],row["full_name"],row["address"],row["national_id"],
                      row["insurance_number"],row["basic_salary"],row["insured_wage"],
                      status_labels.get(row["work_status"],row["work_status"]),
                      "نعم" if row["active"] else "لا",format_export_dates(row["termination_date"] or ""),
                      row["job_title"],row["department"],format_export_dates(row["hire_date"])])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="244062")
        cell.alignment = Alignment(horizontal="center")
    for col, width in enumerate((18,30,38,20,20,18,18,22,12,22,24,24,16), start=1):
        sheet.column_dimensions[chr(64+col)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(horizontal="right")
        for col in (1,2,3,4,5,10,11):
            row[col-1].number_format = "@"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="NEXUS_Employees.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@workforce_bp.route("/hr/template-excel", methods=["GET"])
@_role("admin", "accountant", "viewer")
def download_employee_template():
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Employees"
    sheet.append(["كود الموظف","اسم الموظف","العنوان","الرقم القومي","رقم التأمين",
                  "الأجر الأساسي","الأجر التأميني","الحالة الوظيفية","نشط","تاريخ انتهاء الخدمة",
                  "الوظيفة","الإدارة","تاريخ التعيين"])
    sheet.freeze_panes = "A2"
    for col, width in enumerate((18,30,38,20,20,18,18,22,12,22,24,24,16), start=1):
        sheet.column_dimensions[chr(64+col)].width = width
    guide = workbook.create_sheet("تعليمات")
    guide.append(["الحقل","التعليمات"])
    guide.append(["الحالة الوظيفية","مثبت أو مؤقت أو مكافأة شاملة أو إجازة بدون مرتب أو استقالة أو معاش"])
    guide.append(["نشط","نعم أو لا؛ يحدد إذا كان ملف الموظف نشطًا بالنظام"])
    guide.append(["الأجر الأساسي","مبلغ شهري غير سالب؛ مطلوب عند استيراد الموظف"])
    guide.append(["الأجر التأميني","مبلغ غير سالب؛ اختياري ويُترك صفرًا عند عدم توفره"])
    guide.append(["البيانات المتغيرة","البدلات والاستقطاعات والسلف والحوافز تُدار من شاشاتها المستقلة"])
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="NEXUS_Employees_Template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@workforce_bp.route("/hr/incentives", methods=["GET"])
@_role("admin", "accountant", "viewer")
def incentive_rules():
    # Compatibility URL: incentive/grant setup is now managed in one independent-grants screen.
    return redirect(url_for("workforce.employee_grants"))


@workforce_bp.route("/hr/incentive-rules", methods=["POST"])
@_role("admin", "accountant")
def add_incentive_rule():
    c = _db()
    try:
        name = request.form.get("name", "").strip()
        method = request.form.get("calculation_method", "")
        value = M(request.form.get("calculation_value", "0"))
        if not name or method not in {"fixed", "percentage_basic", "per_day"} or value <= 0:
            raise ValueError("أدخل اسم الحافز وطريقة حساب صحيحة وقيمة أكبر من صفر.")
        execute_compat(c, """INSERT INTO payroll_incentive_rules(name,calculation_method,calculation_value)
                     VALUES(?,?,?)""", (name, method, str(value)))
        _audit(c, "payroll_incentive_rule_created", name)
        c.commit()
        flash("تم حفظ نوع الحافز ويمكن اختياره عند إعداد المرتبات.", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر حفظ الحافز.", "error")
    c.close()
    return redirect(url_for("workforce.employee_grants"))


@workforce_bp.route("/hr/pay-items/catalog", methods=["POST"])
@_role("admin", "accountant")
def add_pay_item_catalog():
    c = _db()
    try:
        name = request.form.get("name", "").strip()
        item_type = request.form.get("item_type", "")
        if not name or len(name) > 120 or item_type not in {"allowance", "deduction"}:
            raise ValueError("اكتب اسم البند وحدد هل هو استحقاق أم استقطاع.")
        execute_compat(c, "INSERT INTO employee_pay_item_catalog(name,item_type) VALUES(?,?)", (name,item_type))
        _audit(c, "employee_pay_item_catalog_created", name + " | " + item_type)
        c.commit()
        flash("تم حفظ اسم البند في قائمة البنود. يمكنك اختياره عند إسناده للموظفين.", "success")
    except DB_INTEGRITY_ERROR:
        c.rollback()
        flash("اسم البند موجود بالفعل ضمن نفس النوع؛ اختره من القائمة بدل إعادة تعريفه.", "warning")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر حفظ اسم البند.", "error")
    c.close()
    return redirect(url_for("workforce.pay_items"))


@workforce_bp.route("/hr/pay-items", methods=["GET", "POST"])
@_role("admin", "accountant")
def pay_items():
    c = _db()
    if request.method == "POST":
        try:
            amount = M(request.form.get("amount", "0"))
            employee_ids = sorted({int(x) for x in request.form.getlist("employee_ids")})
            catalog_id = request.form.get("item_catalog_id", "").strip()
            if catalog_id:
                catalog_item = execute_compat(c, "SELECT name,item_type FROM employee_pay_item_catalog WHERE id=? AND active=1",
                                          (int(catalog_id),)).fetchone()
                if not catalog_item:
                    raise ValueError("اختر اسم بند نشطًا من قائمة البنود.")
                item_name, item_type = catalog_item["name"], catalog_item["item_type"]
            else:
                # Backward compatibility for saved forms and integrations using the former field names.
                item_type = request.form.get("item_type", "")
                item_name = request.form.get("item_name", "").strip()
            if item_type not in {"allowance", "deduction"} or not item_name or amount < 0 or not employee_ids:
                raise ValueError("اختر اسم البند من القائمة وقيمته وحدد موظفًا واحدًا على الأقل.")
            placeholders = ",".join("?" for _ in employee_ids)
            valid_ids = {int(x["id"]) for x in execute_compat(c, 
                "SELECT id FROM employees WHERE active=1 AND id IN ("+placeholders+")", employee_ids).fetchall()}
            if valid_ids != set(employee_ids):
                raise ValueError("بعض الموظفين المختارين غير نشطين أو غير موجودين.")
            begin_write_transaction(c)
            if not catalog_id:
                execute_compat(c, "INSERT INTO employee_pay_item_catalog(name,item_type) VALUES(?,?) ON CONFLICT DO NOTHING",
                          (item_name,item_type))
            created_count = 0
            updated_count = 0
            for employee_id in employee_ids:
                existing = execute_compat(c, """SELECT id FROM employee_pay_items
                    WHERE employee_id=? AND item_name=? AND item_type=?
                    ORDER BY active DESC,id LIMIT 1""",(employee_id,item_name,item_type)).fetchone()
                if existing:
                    execute_compat(c, "UPDATE employee_pay_items SET amount=?,active=1 WHERE id=?",
                              (str(amount),existing["id"]))
                    updated_count += 1
                else:
                    execute_compat(c, """INSERT INTO employee_pay_items(employee_id,item_name,item_type,amount,active)
                        VALUES(?,?,?,?,1)""", (employee_id,item_name,item_type,str(amount)))
                    created_count += 1
            _audit(c,"employee_pay_item_group_created",
                   item_name+" | employees="+str(len(employee_ids))+" | created="+str(created_count)+" | updated="+str(updated_count))
            c.commit()
            flash("تم حفظ البند لـ "+str(len(employee_ids))+" موظفًا دون تكرار. جديد: "+str(created_count)+"، أعيد تفعيله/تحديثه: "+str(updated_count)+".", "success")
        except _DB_VALUE_KEY_ERRORS as exc:
            c.rollback()
            flash(str(exc) or "تعذر حفظ بند الاستحقاق/الاستقطاع.", "error")
        c.close()
        return redirect(url_for("workforce.pay_items"))
    employees = execute_compat(c, """SELECT id,employee_code,full_name,work_status,department FROM employees
        WHERE active=1 ORDER BY work_status,employee_code""").fetchall()
    catalog_items = execute_compat(c, """SELECT id,name,item_type FROM employee_pay_item_catalog
        WHERE active=1 ORDER BY item_type,name""").fetchall()
    assignments = execute_compat(c, """SELECT p.*,e.employee_code,e.full_name,e.work_status FROM employee_pay_items p
        JOIN employees e ON e.id=p.employee_id
        WHERE p.active=0 OR p.id=(SELECT MAX(p2.id) FROM employee_pay_items p2
            WHERE p2.employee_id=p.employee_id AND p2.item_name=p.item_name
              AND p2.item_type=p.item_type AND p2.active=1)
        ORDER BY p.item_type,p.item_name,e.employee_code,p.id""").fetchall()
    grouped = {}
    for item in assignments:
        key = (item["item_type"],item["item_name"])
        grouped.setdefault(key,[]).append(dict(item))
    groups = [{"item_type":key[0],"item_name":key[1],"employees":value} for key,value in grouped.items()]
    c.close()
    return render_template("pay_items.html",employees=employees,groups=groups,catalog_items=catalog_items)


@workforce_bp.route("/hr/pay-items/<int:item_id>/update", methods=["POST"])
@_role("admin", "accountant")
def update_pay_item(item_id):
    c = _db()
    try:
        amount = M(request.form.get("amount", "0"))
        if amount < 0:
            raise ValueError("لا يمكن أن تكون قيمة الاستحقاق أو الاستقطاع سالبة.")
        item = execute_compat(c, "SELECT * FROM employee_pay_items WHERE id=?", (item_id,)).fetchone()
        if not item:
            raise ValueError("البند غير موجود.")
        execute_compat(c, "UPDATE employee_pay_items SET amount=? WHERE id=?", (str(amount),item_id))
        _audit(c,"employee_pay_item_amount_updated",
               item["item_name"]+" employee="+str(item["employee_id"])+" amount="+str(amount))
        c.commit()
        flash("تم تحديث قيمة البند لهذا الموظف. ستنعكس في كشف المرتبات الجديد فقط.", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc),"error")
    c.close()
    return redirect(url_for("workforce.pay_items"))


@workforce_bp.route("/hr/pay-items/<int:item_id>/toggle", methods=["POST"])
@_role("admin", "accountant")
def toggle_pay_item(item_id):
    c = _db()
    try:
        item = execute_compat(c, "SELECT * FROM employee_pay_items WHERE id=?", (item_id,)).fetchone()
        if not item:
            raise ValueError("البند غير موجود.")
        execute_compat(c, "UPDATE employee_pay_items SET active=? WHERE id=?", (0 if item["active"] else 1,item_id))
        _audit(c,"employee_pay_item_toggled",item["item_name"]+" employee="+str(item["employee_id"]))
        c.commit()
        flash("تم تحديث حالة البند. لن يُحذف أي أثر سابق في كشوف المرتبات.", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc),"error")
    c.close()
    return redirect(url_for("workforce.pay_items"))



@workforce_bp.route("/hr/payroll-reconciliation", methods=["GET"])
@_role("admin", "accountant", "viewer")
def payroll_reconciliation():
    """Read-only audit of posted payroll snapshots, duplicate coverage and linked journals."""
    c = _db()
    month_from = request.args.get("month_from", "").strip()
    month_to = request.args.get("month_to", "").strip()
    for label, value in (("من شهر", month_from), ("إلى شهر", month_to)):
        if value:
            try:
                parsed = date.fromisoformat(value + "-01")
                if parsed.strftime("%Y-%m") != value:
                    raise ValueError()
            except ValueError:
                c.close()
                flash("صيغة الشهر غير صحيحة في حقل " + label + ".", "error")
                return redirect(url_for("workforce.payroll_reconciliation"))
    if month_from and month_to and month_from > month_to:
        c.close()
        flash("شهر البداية يجب ألا يتجاوز شهر النهاية.", "error")
        return redirect(url_for("workforce.payroll_reconciliation"))

    where = ["r.status IN ('posted','reversed')"]
    params = []
    if month_from:
        where.append("r.period>=?")
        params.append(month_from)
    if month_to:
        where.append("r.period<=?")
        params.append(month_to)
    runs = execute_compat(c, """
        SELECT r.id,r.period,r.status,r.journal_entry_id,
               COUNT(l.id) AS employee_count,
               COALESCE(SUM(CAST(l.net_pay AS NUMERIC)),0) AS net_total
        FROM payroll_runs r LEFT JOIN payroll_lines l ON l.run_id=r.id
        WHERE """ + " AND ".join(where) + """
        GROUP BY r.id ORDER BY r.period DESC,r.id DESC
    """, params).fetchall()

    issues = []
    reconciled = []
    for run in runs:
        run_issues = []
        employee_duplicates = execute_compat(c, """
            SELECT e.employee_code,e.full_name,COUNT(*) AS occurrences
            FROM payroll_lines l JOIN employees e ON e.id=l.employee_id
            WHERE l.run_id=? GROUP BY l.employee_id
            HAVING COUNT(*)>1
        """, (run["id"],)).fetchall()
        for duplicate in employee_duplicates:
            run_issues.append("تكرار الموظف داخل كشف المرتبات: " + duplicate["employee_code"] + " - " + duplicate["full_name"])

        if run["status"] == "posted":
            if not run["journal_entry_id"]:
                run_issues.append("كشف مرحّل بدون رقم قيد محاسبي.")
                journal = None
            else:
                journal = execute_compat(c, """
                    SELECT j.id,j.status,j.reference,
                           COALESCE(SUM(CAST(l.debit AS NUMERIC)),0) AS debit_total,
                           COALESCE(SUM(CAST(l.credit AS NUMERIC)),0) AS credit_total
                    FROM journal_entries j LEFT JOIN journal_lines l ON l.entry_id=j.id
                    WHERE j.id=? GROUP BY j.id
                """, (run["journal_entry_id"],)).fetchone()
                if not journal:
                    run_issues.append("القيد المرتبط بكشف المرتبات غير موجود.")
                else:
                    expected_reference = "PAYROLL-" + run["period"] + "-RUN-" + str(run["id"])
                    if journal["status"] != "posted":
                        run_issues.append("حالة القيد المرتبط ليست مرحّلة.")
                    if journal["reference"] != expected_reference:
                        run_issues.append("مرجع القيد لا يطابق رقم كشف المرتبات.")
                    debit_total = M(journal["debit_total"])
                    credit_total = M(journal["credit_total"])
                    if debit_total != credit_total:
                        run_issues.append("القيد المحاسبي غير متوازن.")
                    line_totals = execute_compat(c, """
                        SELECT
                          COALESCE(SUM(CAST(gross_pay AS NUMERIC)),0) AS gross_total,
                          COALESCE(SUM(CAST(absence_deduction AS NUMERIC)),0) AS absence_total,
                          COALESCE(SUM(CAST(employer_insurance AS NUMERIC)),0) AS employer_ins_total,
                          COALESCE(SUM(CAST(net_pay AS NUMERIC)),0) AS net_total,
                          COALESCE(SUM(CAST(advance_deduction AS NUMERIC)),0) AS advance_total,
                          COALESCE(SUM(CAST(other_deductions AS NUMERIC)),0) AS other_total,
                          COALESCE(SUM(CAST(penalty_deduction AS NUMERIC)),0) AS penalty_total,
                          COALESCE(SUM(CAST(taxable_income_tax AS NUMERIC)),0) AS tax_total,
                          COALESCE(SUM(CAST(stamp_duty AS NUMERIC)),0) AS stamp_total,
                          COALESCE(SUM(CAST(employee_insurance AS NUMERIC)),0) AS employee_ins_total
                        FROM payroll_lines WHERE run_id=?
                    """, (run["id"],)).fetchone()
                    expected_debit = M(D(line_totals["gross_total"]) + D(line_totals["employer_ins_total"]))
                    expected_credit = M(
                        D(line_totals["net_total"]) + D(line_totals["absence_total"])
                        + D(line_totals["advance_total"]) + D(line_totals["other_total"])
                        + D(line_totals["penalty_total"]) + D(line_totals["tax_total"])
                        + D(line_totals["stamp_total"]) + D(line_totals["employee_ins_total"])
                        + D(line_totals["employer_ins_total"])
                    )
                    if debit_total != expected_debit or credit_total != expected_credit:
                        run_issues.append(
                            "إجمالي القيد لا يطابق ملخص كشف المرتبات "
                            "(مدين " + str(debit_total) + " مقابل " + str(expected_debit)
                            + "، دائن " + str(credit_total) + " مقابل " + str(expected_credit) + ")."
                        )
        else:
            journal = None

        # Audit actual cash/bank payroll payments separately from the accrual journal.
        payment_rows = execute_compat(c, """
            SELECT p.id,p.amount,p.payment_date,p.reference,p.journal_entry_id,
                   COALESCE(SUM(CAST(a.amount AS NUMERIC)),0) AS allocated_total,
                   COUNT(a.id) AS allocation_count
            FROM payroll_payments p
            LEFT JOIN payroll_payment_allocations a ON a.payment_id=p.id
            WHERE p.run_id=?
            GROUP BY p.id ORDER BY p.payment_date,p.id
        """, (run["id"],)).fetchall() if table_exists(c, "payroll_payment_allocations") else execute_compat(c, """
            SELECT p.id,p.amount,p.payment_date,p.reference,p.journal_entry_id,
                   0 AS allocated_total,0 AS allocation_count
            FROM payroll_payments p WHERE p.run_id=? ORDER BY p.payment_date,p.id
        """, (run["id"],)).fetchall()
        paid_total = Decimal("0")
        for payment in payment_rows:
            payment_amount = M(payment["amount"])
            paid_total += payment_amount
            if payment_amount <= 0:
                run_issues.append("دفعة المرتبات رقم " + str(payment["id"]) + " تحتوي مبلغًا غير موجب.")
            if run["status"] == "reversed":
                run_issues.append("تم تسجيل دفعة صرف على كشف مرتبات معكوس: دفعة " + str(payment["id"]) + ".")
            payment_journal = execute_compat(c, """
                SELECT j.id,j.status,j.reference,
                       COALESCE(SUM(CAST(l.debit AS NUMERIC)),0) AS debit_total,
                       COALESCE(SUM(CAST(l.credit AS NUMERIC)),0) AS credit_total
                FROM journal_entries j LEFT JOIN journal_lines l ON l.entry_id=j.id
                WHERE j.id=? GROUP BY j.id
            """, (payment["journal_entry_id"],)).fetchone()
            if not payment_journal:
                run_issues.append("قيد دفعة المرتبات رقم " + str(payment["id"]) + " غير موجود.")
            else:
                if payment_journal["status"] != "posted":
                    run_issues.append("قيد دفعة المرتبات رقم " + str(payment["id"]) + " ليس مرحّلًا.")
                if payment_journal["reference"] != payment["reference"]:
                    run_issues.append("مرجع قيد دفعة المرتبات رقم " + str(payment["id"]) + " لا يطابق مرجع الدفعة.")
                payment_debit = M(payment_journal["debit_total"])
                payment_credit = M(payment_journal["credit_total"])
                if payment_debit != payment_credit:
                    run_issues.append("قيد دفعة المرتبات رقم " + str(payment["id"]) + " غير متوازن.")
                if payment_debit != payment_amount or payment_credit != payment_amount:
                    run_issues.append(
                        "قيمة قيد دفعة المرتبات رقم " + str(payment["id"]) +
                        " لا تطابق مبلغ الدفعة (" + str(payment_amount) + ")."
                    )
            if payment["allocation_count"] == 0:
                run_issues.append(
                    "دفعة المرتبات رقم " + str(payment["id"]) +
                    " غير موزعة على الموظفين؛ يلزم مراجعة توزيعها التاريخي دون إنشاء قيد جديد."
                )
            elif M(payment["allocated_total"]) != payment_amount:
                run_issues.append(
                    "إجمالي توزيع دفعة المرتبات رقم " + str(payment["id"]) +
                    " لا يساوي مبلغ الدفعة."
                )
        if paid_total > M(run["net_total"]):
            run_issues.append(
                "إجمالي دفعات الصرف (" + str(M(paid_total)) +
                ") يتجاوز صافي المرتبات (" + str(M(run["net_total"])) + ")."
            )

        if table_exists(c, "payroll_payment_allocations"):
            invalid_allocations = execute_compat(c, """
                SELECT a.id,a.payment_id,a.payroll_line_id,p.run_id AS payment_run_id,
                       pl.run_id AS line_run_id
                FROM payroll_payment_allocations a
                JOIN payroll_payments p ON p.id=a.payment_id
                JOIN payroll_lines pl ON pl.id=a.payroll_line_id
                WHERE p.run_id<>pl.run_id AND p.run_id=?
            """, (run["id"],)).fetchall()
            if invalid_allocations:
                run_issues.append("توجد توزيعات صرف مرتبطة بموظفين من كشف مرتبات آخر.")
            invalid_amount_allocations = execute_compat(c, """
                SELECT a.id,a.amount FROM payroll_payment_allocations a
                JOIN payroll_payments p ON p.id=a.payment_id
                JOIN payroll_lines pl ON pl.id=a.payroll_line_id
                WHERE p.run_id=? AND CAST(a.amount AS NUMERIC)<=0
            """, (run["id"],)).fetchall()
            if invalid_amount_allocations:
                run_issues.append("توجد توزيعات صرف بمبالغ صفرية أو سالبة.")
            over_allocated_lines = execute_compat(c, """
                SELECT pl.id,e.employee_code,e.full_name,pl.net_pay,
                       SUM(CAST(a.amount AS NUMERIC)) AS allocated
                FROM payroll_lines pl
                JOIN employees e ON e.id=pl.employee_id
                JOIN payroll_payment_allocations a ON a.payroll_line_id=pl.id
                JOIN payroll_payments p ON p.id=a.payment_id
                WHERE pl.run_id=?
                GROUP BY pl.id
                HAVING SUM(CAST(a.amount AS NUMERIC))>CAST(pl.net_pay AS NUMERIC)
            """, (run["id"],)).fetchall()
            for allocation in over_allocated_lines:
                run_issues.append(
                    "توزيعات الصرف للموظف " + allocation["employee_code"] + " - " +
                    allocation["full_name"] + " تتجاوز صافي استحقاقه."
                )

        if run_issues:
            for message in run_issues:
                issues.append({"period":run["period"],"run_id":run["id"],"message":message})
        reconciled.append({
            "period":run["period"],"run_id":run["id"],"status":run["status"],
            "employee_count":run["employee_count"],"net_total":M(run["net_total"]),
            "paid_total":M(paid_total),
            "remaining_total":M(max(Decimal("0"),M(run["net_total"])-M(paid_total))),
            "journal_entry_id":run["journal_entry_id"],
            "result":"سليم" if not run_issues and run["status"]=="posted"
                     else ("معكوس تاريخيًا" if run["status"]=="reversed" and not run_issues else "يحتاج مراجعة"),
            "issue_count":len(run_issues)
        })

    duplicate_periods = execute_compat(c, f"""
        SELECT period,COUNT(*) AS run_count,{_group_concat_expression(c, 'id')} AS run_ids
        FROM payroll_runs WHERE status='posted'
        """ + (" AND period>=?" if month_from else "") +
        (" AND period<=?" if month_to else "") + """
        GROUP BY period HAVING COUNT(*)>1 ORDER BY period DESC
    """, ([month_from] if month_from else []) + ([month_to] if month_to else [])).fetchall()
    for duplicate in duplicate_periods:
        issues.append({"period":duplicate["period"],"run_id":"—",
            "message":"أكثر من كشف مرتبات مرحّل لنفس الشهر: " + str(duplicate["run_ids"])})

    duplicate_employees = execute_compat(c, f"""
        SELECT r.period,e.employee_code,e.full_name,COUNT(*) AS occurrences,
               {_group_concat_expression(c, 'r.id')} AS run_ids
        FROM payroll_runs r
        JOIN payroll_lines l ON l.run_id=r.id
        JOIN employees e ON e.id=l.employee_id
        WHERE r.status='posted'
        """ + (" AND r.period>=?" if month_from else "") +
        (" AND r.period<=?" if month_to else "") + """
        GROUP BY r.period,l.employee_id HAVING COUNT(*)>1
        ORDER BY r.period DESC,e.employee_code
    """, ([month_from] if month_from else []) + ([month_to] if month_to else [])).fetchall()
    for duplicate in duplicate_employees:
        issues.append({"period":duplicate["period"],"run_id":duplicate["run_ids"],
            "message":"تكرار صرف الموظف " + duplicate["employee_code"] + " - " +
            duplicate["full_name"] + " في كشوف مرحّلة للشهر نفسه."})

    overlap_rows = execute_compat(c, """
        SELECT r.period,r.id AS run_id,e.employee_code,e.full_name,
               f.final_payroll_no,f.status AS final_status
        FROM payroll_runs r
        JOIN payroll_lines l ON l.run_id=r.id
        JOIN employees e ON e.id=l.employee_id
        JOIN employee_final_payrolls f
          ON f.employee_id=e.id AND f.period=r.period AND f.status IN ('approved','paid')
        WHERE r.status='posted'
        """ + (" AND r.period>=?" if month_from else "") +
        (" AND r.period<=?" if month_to else "") + """
        ORDER BY r.period DESC,e.employee_code
    """, ([month_from] if month_from else []) + ([month_to] if month_to else [])).fetchall()
    for overlap in overlap_rows:
        issues.append({"period":overlap["period"],"run_id":overlap["run_id"],
            "message":"احتمال تكرار استحقاق بين المرتب الشهري والمرتب النهائي للموظف " +
            overlap["employee_code"] + " - " + overlap["full_name"] +
            " (كشف نهائي " + overlap["final_payroll_no"] + ")."})

    duplicate_final = execute_compat(c, f"""
        SELECT period,e.employee_code,e.full_name,COUNT(*) AS occurrences,
               {_group_concat_expression(c, 'final_payroll_no')} AS final_numbers
        FROM employee_final_payrolls f JOIN employees e ON e.id=f.employee_id
        WHERE f.status IN ('approved','paid')
        """ + (" AND period>=?" if month_from else "") +
        (" AND period<=?" if month_to else "") + """
        GROUP BY period,employee_id HAVING COUNT(*)>1
        ORDER BY period DESC,e.employee_code
    """, ([month_from] if month_from else []) + ([month_to] if month_to else [])).fetchall()
    for duplicate in duplicate_final:
        issues.append({"period":duplicate["period"],"run_id":"—",
            "message":"أكثر من كشف مرتب نهائي معتمد للموظف " + duplicate["employee_code"] +
            " - " + duplicate["full_name"] + ": " + duplicate["final_numbers"]})

    journal_duplicates = execute_compat(c, f"""
        SELECT reference,COUNT(*) AS occurrences,{_group_concat_expression(c, 'id')} AS journal_ids
        FROM journal_entries WHERE reference LIKE 'PAYROLL-%'
        GROUP BY reference HAVING COUNT(*)>1
        ORDER BY reference
    """).fetchall()
    for duplicate in journal_duplicates:
        issues.append({"period":"—","run_id":"—",
            "message":"مرجع قيد مرتبات مكرر " + str(duplicate["reference"]) +
            " (القيود " + str(duplicate["journal_ids"]) + ")."})

    c.close()
    summary = {
        "runs":len(reconciled),
        "clean":sum(1 for item in reconciled if item["result"]=="سليم"),
        "reversed":sum(1 for item in reconciled if item["result"]=="معكوس تاريخيًا"),
        "needs_review":sum(1 for item in reconciled if item["result"]=="يحتاج مراجعة"),
        "issues":len(issues),
        "paid_total":M(sum((D(item["paid_total"]) for item in reconciled),Decimal("0"))),
        "remaining_total":M(sum((D(item["remaining_total"]) for item in reconciled),Decimal("0")))
    }
    return render_template("payroll_reconciliation.html", runs=reconciled, issues=issues,
        summary=summary, month_from=month_from, month_to=month_to)


@workforce_bp.route("/hr/payroll-report", methods=["GET"])
@_role("admin", "accountant", "viewer")
def payroll_report():
    """Historical employee earnings statement sourced only from posted payroll runs."""
    c = _db()
    month_from = request.args.get("month_from", "").strip()
    month_to = request.args.get("month_to", "").strip()
    employee_id = request.args.get("employee_id", type=int)
    for label, value in (("من شهر", month_from), ("إلى شهر", month_to)):
        if value:
            try:
                parsed = date.fromisoformat(value + "-01")
                if parsed.strftime("%Y-%m") != value:
                    raise ValueError()
            except ValueError:
                c.close()
                flash("صيغة الشهر غير صحيحة في حقل " + label + ".", "error")
                return redirect(url_for("workforce.payroll_report"))
    if month_from and month_to and month_from > month_to:
        c.close()
        flash("شهر البداية يجب ألا يتجاوز شهر النهاية.", "error")
        return redirect(url_for("workforce.payroll_report"))
    where = ["r.status='posted'"]
    params = []
    if month_from:
        where.append("r.period>=?")
        params.append(month_from)
    if month_to:
        where.append("r.period<=?")
        params.append(month_to)
    if employee_id:
        where.append("l.employee_id=?")
        params.append(employee_id)
    rows = execute_compat(c, """
        SELECT r.id AS run_id,l.id AS payroll_line_id,r.period,e.id AS employee_id,e.employee_code,e.full_name,e.department,
               l.basic_salary,l.salary_raise,l.allowances,l.incentives,l.incentive_name,l.stamp_duty,
               l.overtime_hours,l.overtime_rate_percent,l.overtime_amount,l.gross_pay,
               l.absent_hours,l.absence_deduction,l.advance_deduction,
               COALESCE(l.loan_deduction,'0') AS loan_deduction,l.other_deductions,
               COALESCE(l.penalty_deduction,'0') AS penalty_deduction,l.taxable_income_tax,l.employee_insurance,l.employer_insurance,
               l.total_entitlements,l.total_deductions,l.net_pay
        FROM payroll_lines l
        JOIN payroll_runs r ON r.id=l.run_id
        JOIN employees e ON e.id=l.employee_id
        WHERE """ + " AND ".join(where) + """
        ORDER BY r.period DESC,e.employee_code
    """, params).fetchall()
    payment_rows = execute_compat(c, 
        "SELECT run_id,amount FROM payroll_payments ORDER BY run_id,id"
    ).fetchall()
    paid_by_run = {}
    for payment in payment_rows:
        run_key = payment["run_id"]
        paid_by_run[run_key] = paid_by_run.get(run_key, Decimal("0")) + D(payment["amount"])
    rows = [dict(row) for row in rows]
    run_net_pay = {}
    allocated_total_by_run = {}
    if table_exists(c, "payroll_payment_allocations"):
        for allocation_row in execute_compat(c, """SELECT l.run_id,a.amount FROM payroll_payment_allocations a
            JOIN payroll_lines l ON l.id=a.payroll_line_id""").fetchall():
            run_key = allocation_row["run_id"]
            allocated_total_by_run[run_key] = allocated_total_by_run.get(run_key,Decimal("0")) + D(allocation_row["amount"])
    for row in rows:
        allocated = Decimal("0")
        if table_exists(c, "payroll_payment_allocations"):
            allocated = sum((D(x["amount"]) for x in execute_compat(c, 
                "SELECT amount FROM payroll_payment_allocations WHERE payroll_line_id=?",
                (row["payroll_line_id"],)).fetchall()),Decimal("0"))
        row["paid_amount"] = M(allocated)
        row["remaining_payable"] = M(max(Decimal("0"),D(row["net_pay"])-allocated))
        loan = D(row["loan_deduction"])
        row["manual_advance_deduction"] = M(max(Decimal("0"), D(row["advance_deduction"]) - loan))
        row["employee_deductions"] = M(
            D(row["absence_deduction"]) + row["manual_advance_deduction"] + loan
            + D(row["other_deductions"]) + D(row["penalty_deduction"]) + D(row["taxable_income_tax"])
            + D(row["stamp_duty"]) + D(row["employee_insurance"])
        )
        row["employer_total_cost"] = M(
            D(row["gross_pay"]) - D(row["absence_deduction"]) + D(row["employer_insurance"])
        )
        run_net_pay[row["run_id"]] = run_net_pay.get(row["run_id"], Decimal("0")) + D(row["net_pay"])
    # Only approved/paid final payrolls have been posted to accounting; drafts and cancelled records must not inflate historical payroll totals.
    final_where = ["f.status IN ('approved','paid')"]
    final_params = []
    if month_from:
        final_where.append("f.period>=?")
        final_params.append(month_from)
    if month_to:
        final_where.append("f.period<=?")
        final_params.append(month_to)
    if employee_id:
        final_where.append("f.employee_id=?")
        final_params.append(employee_id)
    final_rows = execute_compat(c, """SELECT f.*,e.employee_code,e.full_name,e.department
        FROM employee_final_payrolls f JOIN employees e ON e.id=f.employee_id
        WHERE """ + " AND ".join(final_where) + """
        ORDER BY f.period DESC,e.employee_code""", final_params).fetchall()
    final_paid_rows = execute_compat(c, """SELECT p.final_payroll_id,p.amount
        FROM employee_final_payroll_payments p JOIN employee_final_payrolls f ON f.id=p.final_payroll_id
        WHERE f.period>=? AND f.period<=?""",
        (month_from or "0000-01",month_to or "9999-12")).fetchall()
    final_paid_map = {}
    for payment_row in final_paid_rows:
        key = int(payment_row["final_payroll_id"])
        final_paid_map[key] = M(final_paid_map.get(key,Decimal("0")) + D(payment_row["amount"]))
    if employee_id:
        final_paid_rows = execute_compat(c, """SELECT p.final_payroll_id,p.amount
            FROM employee_final_payroll_payments p JOIN employee_final_payrolls f ON f.id=p.final_payroll_id
            WHERE f.employee_id=?""", (employee_id,)).fetchall()
        final_paid_map = {}
        for payment_row in final_paid_rows:
            key = int(payment_row["final_payroll_id"])
            final_paid_map[key] = M(final_paid_map.get(key,Decimal("0")) + D(payment_row["amount"]))
    final_totals = {
        "gross_pay": M(sum((D(x["gross_pay"]) for x in final_rows if x["status"] != "cancelled"),Decimal("0"))),
        "net_pay": M(sum((D(x["net_pay"]) for x in final_rows if x["status"] != "cancelled"),Decimal("0"))),
        "paid": M(sum((final_paid_map.get(int(x["id"]),Decimal("0")) for x in final_rows if x["status"] != "cancelled"),Decimal("0"))),
        "remaining": M(sum((max(Decimal("0"),D(x["net_pay"])-final_paid_map.get(int(x["id"]),Decimal("0")))
                            for x in final_rows if x["status"] != "cancelled"),Decimal("0"))),
        "count": sum(1 for x in final_rows if x["status"] != "cancelled"),
    }
    employee_options = execute_compat(c, 
        "SELECT id,employee_code,full_name FROM employees ORDER BY employee_code"
    ).fetchall()
    fields = ("basic_salary","salary_raise","allowances","incentives","overtime_amount","gross_pay",
              "absence_deduction","manual_advance_deduction","loan_deduction","other_deductions","penalty_deduction",
              "taxable_income_tax","stamp_duty","employee_insurance","employer_insurance","employee_deductions",
              "employer_total_cost","net_pay","paid_amount","remaining_payable")
    totals = {key: M(sum((D(row[key]) for row in rows), Decimal("0"))) for key in fields}
    # Legacy run-level payments are deliberately not attributed to employees.
    # Report them separately so old records remain visible without fabricated allocations.
    selected_run_ids = set(run_net_pay)
    totals["unallocated_legacy_payments"] = M(sum(
        (max(Decimal("0"),paid_by_run.get(run_id,Decimal("0"))-allocated_total_by_run.get(run_id,Decimal("0")))
         for run_id in selected_run_ids),Decimal("0")))
    c.close()
    return render_template("payroll_report.html", rows=rows, totals=totals,
                           final_rows=final_rows, final_totals=final_totals,
                           employees=employee_options, month_from=month_from,
                           month_to=month_to, selected_employee=employee_id)


@workforce_bp.route("/hr/final-payroll", methods=["GET", "POST"])
@_role("admin", "accountant")
def final_payroll():
    c = _db()
    try:
        if request.method == "POST":
            employee_id = int(request.form.get("employee_id", "0"))
            period = request.form.get("period", "").strip()
            period_start = date.fromisoformat(period + "-01")
            if period != period_start.strftime("%Y-%m"):
                raise ValueError("شهر المرتب النهائي غير صحيح.")
            next_month = date(period_start.year + (period_start.month == 12),
                              1 if period_start.month == 12 else period_start.month + 1, 1)
            period_end = next_month - date.resolution
            employee = execute_compat(c, "SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
            if not employee or employee["work_status"] not in ("resigned", "retired") or not employee["termination_date"]:
                raise ValueError("الموظف غير مسجل كمنتهية خدمته.")
            termination = date.fromisoformat(employee["termination_date"])
            if termination.strftime("%Y-%m") != period:
                raise ValueError("شهر الكشف يجب أن يطابق شهر انتهاء خدمة الموظف.")
            if termination < date.fromisoformat(employee["hire_date"]):
                raise ValueError("تاريخ انتهاء الخدمة يسبق تاريخ التعيين.")
            days_worked = (termination - period_start).days + 1
            if days_worked < 1 or days_worked > (period_end - period_start).days + 1:
                raise ValueError("عدد أيام العمل غير صحيح.")
            existing_monthly_line = execute_compat(c, """SELECT r.id,r.status FROM payroll_runs r
                JOIN payroll_lines l ON l.run_id=r.id
                WHERE r.period=? AND l.employee_id=? LIMIT 1""", (period,employee_id)).fetchone()
            if existing_monthly_line:
                raise ValueError("يوجد لهذا الموظف سطر بالفعل في كشف المرتبات الشهري لهذا الشهر. راجع الكشف واحذف المسودة القديمة أو عالج الصرف السابق قبل إعداد مرتب نهائي لمنع التكرار.")
            basic_monthly, _insured_monthly = _employee_salary_basis(c, employee_id, termination.isoformat())
            if basic_monthly <= 0:
                raise ValueError("لا يوجد راتب أساسي معتمد لهذا الموظف. سجّل أول كشف مرتبات مرحّل أو راجع بيانات الأجر قبل إعداد المرتب النهائي.")
            allowances_monthly = M(sum((D(x["amount"]) for x in execute_compat(c, """
                SELECT p.amount FROM employee_pay_items p WHERE p.employee_id=? AND p.active=1
                  AND p.item_type='allowance'
                  AND p.id=(SELECT MAX(p2.id) FROM employee_pay_items p2
                    WHERE p2.employee_id=p.employee_id AND p2.item_name=p.item_name
                      AND p2.item_type=p.item_type AND p2.active=1)""", (employee_id,)).fetchall()), Decimal("0")))
            basic_prorated = M(basic_monthly * Decimal(days_worked) / Decimal("30"))
            allowances_prorated = M(allowances_monthly * Decimal(days_worked) / Decimal("30"))
            gross = M(basic_prorated + allowances_prorated)
            tax = M(request.form.get("tax_deduction", "0"))
            employee_ins = M(request.form.get("employee_insurance", "0"))
            employer_ins = M(request.form.get("employer_insurance", "0"))
            stamp = M(request.form.get("stamp_duty", "0"))
            other = M(request.form.get("other_deductions", "0"))
            deductions = M(tax + employee_ins + stamp + other)
            if min(tax, employee_ins, employer_ins, stamp, other) < 0 or deductions > gross:
                raise ValueError("راجع الاستقطاعات؛ يجب ألا تكون سالبة أو تتجاوز إجمالي المستحق.")
            net = M(gross - deductions)
            final_no = "FINAL-" + employee["employee_code"] + "-" + termination.strftime("%Y%m%d")
            begin_write_transaction(c)
            execute_compat(c, """INSERT INTO employee_final_payrolls
                (final_payroll_no,employee_id,period,termination_date,days_worked,basic_salary_snapshot,
                 allowances_snapshot,gross_pay,tax_deduction,employee_insurance,employer_insurance,
                 stamp_duty,other_deductions,net_pay,notes,created_by)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (final_no,employee_id,period,termination.isoformat(),days_worked,str(basic_prorated),
                 str(allowances_prorated),str(gross),str(tax),str(employee_ins),str(employer_ins),
                 str(stamp),str(other),str(net),request.form.get("notes","").strip(),session["user_id"]))
            _audit(c,"employee_final_payroll_draft_created",final_no+" net="+str(net))
            c.commit()
            flash("تم حفظ المرتب النهائي كمسودة مستقلة. لم يدخل في كشف المرتبات العادي ولم يتم اعتماده أو صرفه أو إنشاء قيد.", "success")
            return redirect(url_for("workforce.final_payroll", period=period))

        selected_period = request.args.get("period", date.today().strftime("%Y-%m"))
        try:
            period_start = date.fromisoformat(selected_period + "-01")
            if period_start.strftime("%Y-%m") != selected_period:
                raise ValueError()
        except ValueError:
            selected_period = date.today().strftime("%Y-%m")
            period_start = date.fromisoformat(selected_period + "-01")
        next_month = date(period_start.year + (period_start.month == 12),
                          1 if period_start.month == 12 else period_start.month + 1, 1)
        period_end = next_month - date.resolution
        eligible_rows = execute_compat(c, """SELECT e.* FROM employees e WHERE e.work_status IN ('resigned','retired')
            AND e.termination_date>=? AND e.termination_date<=?
            AND NOT EXISTS (SELECT 1 FROM employee_final_payrolls f
                WHERE f.employee_id=e.id AND f.termination_date=e.termination_date)
            AND NOT EXISTS (SELECT 1 FROM payroll_runs r JOIN payroll_lines l ON l.run_id=r.id
                WHERE r.period=? AND l.employee_id=e.id)
            ORDER BY e.employee_code""", (period_start.isoformat(),period_end.isoformat(),selected_period)).fetchall()
        eligible = []
        for eligible_row in eligible_rows:
            item = dict(eligible_row)
            item["basic_salary"] = str(_employee_salary_basis(c, item["id"], item["termination_date"])[0])
            eligible.append(item)
        rows = [dict(x) for x in execute_compat(c, """SELECT f.*,e.employee_code,e.full_name FROM employee_final_payrolls f
            JOIN employees e ON e.id=f.employee_id WHERE f.period=? ORDER BY f.created_at DESC,f.id DESC""",
            (selected_period,)).fetchall()]
        paid_rows = execute_compat(c, """SELECT final_payroll_id,amount
            FROM employee_final_payroll_payments ORDER BY id""").fetchall()
        paid_map = {}
        for payment_row in paid_rows:
            key = int(payment_row["final_payroll_id"])
            paid_map[key] = M(paid_map.get(key,Decimal("0")) + D(payment_row["amount"]))
        for item in rows:
            item["paid_amount"] = paid_map.get(int(item["id"]), Decimal("0"))
            item["remaining_amount"] = M(max(Decimal("0"),D(item["net_pay"])-item["paid_amount"]))
        payment_history = execute_compat(c, """SELECT p.*,a.code AS account_code,a.name_ar AS account_name
            FROM employee_final_payroll_payments p JOIN accounts a ON a.id=p.cash_account_id
            JOIN employee_final_payrolls f ON f.id=p.final_payroll_id
            WHERE f.period=? ORDER BY p.payment_date,p.id""", (selected_period,)).fetchall()
        payment_accounts = execute_compat(c, """SELECT a.id,a.code,a.name_ar FROM accounts a
            JOIN cash_accounts ca ON ca.account_id=a.id
            WHERE a.active=1 AND a.is_group=0 AND a.account_type='asset' AND ca.active=1
            ORDER BY a.code""").fetchall()
        return render_template("final_payroll.html", employees=eligible, rows=rows,
                               accounts=_accounts(c), payment_accounts=payment_accounts,
                               payment_history=payment_history, selected_period=selected_period)
    except Exception as exc:
        try:
            c.rollback()
        except Exception:
            pass
        flash(safe_error_message(exc), "error")
        return redirect(url_for("workforce.final_payroll"))
    finally:
        c.close()


@workforce_bp.route("/hr/final-payroll/<int:final_id>/edit", methods=["GET", "POST"])
@_role("admin", "accountant")
def edit_final_payroll(final_id):
    c = _db()
    try:
        if request.method == "POST":
            begin_write_transaction(c)
            row = execute_compat(c, "SELECT * FROM employee_final_payrolls WHERE id=?", (final_id,)).fetchone()
            if not row or row["status"] != "draft":
                raise ValueError("التعديل متاح لمسودة المرتب النهائي فقط.")
            tax = M(request.form.get("tax_deduction", "0"))
            employee_ins = M(request.form.get("employee_insurance", "0"))
            employer_ins = M(request.form.get("employer_insurance", "0"))
            stamp = M(request.form.get("stamp_duty", "0"))
            other = M(request.form.get("other_deductions", "0"))
            gross = M(row["gross_pay"])
            deductions = M(tax + employee_ins + stamp + other)
            if min(tax, employee_ins, employer_ins, stamp, other) < 0 or deductions > gross:
                raise ValueError("راجع الاستقطاعات؛ يجب ألا تكون سالبة أو تتجاوز إجمالي المستحق.")
            net = M(gross - deductions)
            notes = request.form.get("notes", "").strip()[:500]
            update_cursor = execute_compat(c, """UPDATE employee_final_payrolls SET tax_deduction=?,employee_insurance=?,
                employer_insurance=?,stamp_duty=?,other_deductions=?,net_pay=?,notes=?
                WHERE id=? AND status='draft'""",
                (str(tax),str(employee_ins),str(employer_ins),str(stamp),str(other),str(net),notes,final_id))
            if update_cursor.rowcount != 1:
                raise ValueError("لم يتم حفظ التعديلات؛ ربما تغيرت حالة الكشف.")
            _audit(c, "employee_final_payroll_draft_updated",
                   str(row["final_payroll_no"]) + " net=" + str(net))
            c.commit()
            flash("تم تحديث مسودة المرتب النهائي.", "success")
            return redirect(url_for("workforce.final_payroll", period=row["period"]))
        row = execute_compat(c, """SELECT f.*,e.employee_code,e.full_name FROM employee_final_payrolls f
            JOIN employees e ON e.id=f.employee_id WHERE f.id=?""", (final_id,)).fetchone()
        if not row:
            flash("مسودة المرتب النهائي غير موجودة.", "error")
            return redirect(url_for("workforce.final_payroll"))
        if row["status"] != "draft":
            flash("لا يمكن تعديل كشف غير مسودة.", "error")
            return redirect(url_for("workforce.final_payroll", period=row["period"]))
        return render_template("final_payroll_edit.html", row=row)
    except Exception as exc:
        c.rollback()
        flash(safe_error_message(exc), "error")
        return redirect(url_for("workforce.final_payroll"))
    finally:
        c.close()


@workforce_bp.route("/hr/final-payroll/<int:final_id>/cancel", methods=["POST"])
@_role("admin", "accountant")
def cancel_final_payroll(final_id):
    c = _db()
    try:
        begin_write_transaction(c)
        row = execute_compat(c, "SELECT * FROM employee_final_payrolls WHERE id=?", (final_id,)).fetchone()
        if not row or row["status"] != "draft":
            raise ValueError("إلغاء المرتب النهائي متاح للمسودة فقط.")
        update_cursor = execute_compat(c, "UPDATE employee_final_payrolls SET status='cancelled' WHERE id=? AND status='draft'",
                  (final_id,))
        if update_cursor.rowcount != 1:
            raise ValueError("لم يتم إلغاء المسودة؛ ربما تغيرت حالتها.")
        _audit(c, "employee_final_payroll_draft_cancelled", row["final_payroll_no"])
        c.commit()
        flash("تم إلغاء المسودة. احتفظ النظام بسجلها ولم يحذفها.", "success")
        return redirect(url_for("workforce.final_payroll", period=row["period"]))
    except Exception as exc:
        c.rollback()
        flash(safe_error_message(exc), "error")
        return redirect(url_for("workforce.final_payroll"))
    finally:
        c.close()


@workforce_bp.route("/hr/final-payroll/<int:final_id>/reopen", methods=["POST"])
@_role("admin", "accountant")
def reopen_final_payroll(final_id):
    c = _db()
    try:
        begin_write_transaction(c)
        row = execute_compat(c, "SELECT * FROM employee_final_payrolls WHERE id=?", (final_id,)).fetchone()
        if not row or row["status"] != "cancelled" or row["journal_entry_id"]:
            raise ValueError("إعادة الفتح متاحة للمسودة الملغاة فقط بشرط عدم وجود قيد أو دفعات.")
        if execute_compat(c, "SELECT 1 FROM employee_final_payroll_payments WHERE final_payroll_id=? LIMIT 1",
                     (final_id,)).fetchone():
            raise ValueError("لا يمكن إعادة فتح كشف مرتبط بدفعات صرف.")
        update_cursor = execute_compat(c, "UPDATE employee_final_payrolls SET status='draft' WHERE id=? AND status='cancelled'",
                  (final_id,))
        if update_cursor.rowcount != 1:
            raise ValueError("لم تتم إعادة فتح المسودة.")
        _audit(c, "employee_final_payroll_draft_reopened", row["final_payroll_no"])
        c.commit()
        flash("تمت إعادة فتح المسودة ويمكن تعديلها أو اعتمادها.", "success")
        return redirect(url_for("workforce.final_payroll", period=row["period"]))
    except Exception as exc:
        c.rollback()
        flash(safe_error_message(exc), "error")
        return redirect(url_for("workforce.final_payroll"))
    finally:
        c.close()


@workforce_bp.route("/hr/final-payroll/<int:final_id>/approve", methods=["POST"])
@_role("admin", "accountant", permission="workforce.approve")
def approve_final_payroll(final_id):
    c = _db()
    try:
        begin_write_transaction(c)
        row = execute_compat(c, """SELECT f.*,e.full_name,e.employee_code FROM employee_final_payrolls f
            JOIN employees e ON e.id=f.employee_id WHERE f.id=?""", (final_id,)).fetchone()
        if not row or row["status"] != "draft" or row["journal_entry_id"] or row["approval_status"] != "pending":
            raise ValueError("لا يمكن اعتماد إلا مسودة مرتب نهائي بانتظار المراجعة وغير مرحلة.")
        if row["created_by"] is None or row["created_by"] == session["user_id"]:
            raise ValueError("لا يجوز اعتماد المرتب النهائي بواسطة منشئه، أو دون بيانات منشئ موثقة.")
        salary_expense = int(request.form["salary_expense_account"])
        employer_expense = int(request.form["employer_ins_expense_account"])
        salaries_payable = int(request.form["salaries_payable_account"])
        tax_payable = int(request.form["tax_payable_account"])
        insurance_payable = int(request.form["insurance_payable_account"])
        other_account = int(request.form["other_deductions_account"])
        expected = [
            (salary_expense, "expense"), (employer_expense, "expense"),
            (salaries_payable, "liability"), (tax_payable, "liability"),
            (insurance_payable, "liability"),
        ]
        for account_id, account_type in expected:
            account = execute_compat(c, """SELECT id,account_type,is_group,active FROM accounts
                WHERE id=?""", (account_id,)).fetchone()
            if not account or not account["active"] or account["is_group"] or account["account_type"] != account_type:
                raise ValueError("راجع الحسابات المختارة؛ يجب أن تكون تفصيلية ونشطة وبطبيعة الحساب المطلوبة.")
        other_row = execute_compat(c, "SELECT id,is_group,active FROM accounts WHERE id=?", (other_account,)).fetchone()
        if not other_row or not other_row["active"] or other_row["is_group"]:
            raise ValueError("اختر حسابًا تفصيليًا ونشطًا مقابلًا للاستقطاعات الأخرى.")
        gross = M(row["gross_pay"])
        employer_ins = M(row["employer_insurance"])
        net = M(row["net_pay"])
        tax_and_stamp = M(D(row["tax_deduction"]) + D(row["stamp_duty"]))
        employee_ins = M(row["employee_insurance"])
        other = M(row["other_deductions"])
        lines = [
            (salary_expense, gross, 0, "الأجر النهائي - " + row["employee_code"] + " " + row["full_name"]),
            (employer_expense, employer_ins, 0, "حصة الشركة بالتأمينات - " + row["employee_code"]),
            (salaries_payable, 0, net, "صافي المرتب النهائي المستحق"),
            (tax_payable, 0, tax_and_stamp, "ضريبة ودمغة المرتب النهائي"),
            (insurance_payable, 0, M(employee_ins + employer_ins), "تأمينات المرتب النهائي"),
            (other_account, 0, other, "استقطاعات أخرى من المرتب النهائي"),
        ]
        debit_total = M(sum((D(x[1]) for x in lines), Decimal("0")))
        credit_total = M(sum((D(x[2]) for x in lines), Decimal("0")))
        if debit_total <= 0 or debit_total != credit_total:
            raise ValueError("القيد المقترح غير متوازن. لم يتم ترحيل المرتب النهائي.")
        reference = "FINAL-PAYROLL-POST-" + str(final_id)
        journal_id = post_journal(c, row["termination_date"], reference,
            "اعتماد المرتب النهائي للموظف " + row["full_name"], lines, session["user_id"])
        update_cursor = execute_compat(c, """UPDATE employee_final_payrolls SET status='approved',journal_entry_id=?,
            approved_by=?,approved_at=CURRENT_TIMESTAMP,approval_status='approved'
            WHERE id=? AND status='draft' AND journal_entry_id IS NULL AND approval_status='pending'""",
            (journal_id, session["user_id"], final_id))
        if update_cursor.rowcount != 1:
            raise ValueError("تعذر تثبيت اعتماد المرتب النهائي؛ راجع حالة الكشف.")
        _audit(c, "employee_final_payroll_approved",
               row["final_payroll_no"] + " journal=" + str(journal_id))
        c.commit()
        flash("تم اعتماد المرتب النهائي وترحيل قيده المحاسبي.", "success")
        return redirect(url_for("workforce.final_payroll", period=row["period"]))
    except Exception as exc:
        c.rollback()
        flash(safe_error_message(exc), "error")
        return redirect(url_for("workforce.final_payroll"))
    finally:
        c.close()


@workforce_bp.route("/hr/final-payroll/<int:final_id>/pay", methods=["POST"])
@_role("admin", "accountant")
def pay_final_payroll(final_id):
    c = _db()
    try:
        payment_date = date.fromisoformat(request.form.get("payment_date") or date.today().isoformat()).isoformat()
        amount = M(request.form.get("amount", "0"))
        if amount <= 0:
            raise ValueError("مبلغ صرف المرتب النهائي يجب أن يكون أكبر من صفر.")
        cash_id = int(request.form["cash_account_id"])
        begin_write_transaction(c)
        _cash_or_bank_account(c, cash_id)
        row = execute_compat(c, "SELECT * FROM employee_final_payrolls WHERE id=?", (final_id,)).fetchone()
        if not row or row["status"] not in ("approved", "paid") or not row["journal_entry_id"]:
            raise ValueError("يجب اعتماد المرتب النهائي وترحيل قيده قبل الصرف.")
        if row["approved_by"] is not None and row["approved_by"] == session["user_id"]:
            raise ValueError("يجب أن ينفذ الصرف مستخدم مختلف عن معتمد المرتب النهائي.")
        paid = M(sum((D(x["amount"]) for x in execute_compat(c, 
            "SELECT amount FROM employee_final_payroll_payments WHERE final_payroll_id=?",
            (final_id,)).fetchall()), Decimal("0")))
        outstanding = M(max(Decimal("0"), D(row["net_pay"]) - paid))
        if amount > outstanding or outstanding <= 0:
            raise ValueError("مبلغ الصرف يتجاوز صافي المرتب النهائي المتبقي.")
        payable = execute_compat(c, """SELECT account_id FROM journal_lines
            WHERE entry_id=? AND memo='صافي المرتب النهائي المستحق' AND CAST(credit AS NUMERIC)>0
            ORDER BY id LIMIT 1""", (row["journal_entry_id"],)).fetchone()
        if not payable:
            raise ValueError("لم يتم العثور على حساب صافي المرتب المستحق في القيد الأصلي.")
        reference = "FINAL-PAYROLL-PAY-" + str(final_id) + "-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        journal_id = post_journal(c, payment_date, reference,
            "صرف المرتب النهائي رقم " + row["final_payroll_no"],
            [(int(payable["account_id"]), amount, 0, "تخفيض صافي المرتب النهائي المستحق"),
             (cash_id, 0, amount, "صرف المرتب النهائي من البنك/الصندوق")],
            session["user_id"])
        execute_compat(c, """INSERT INTO employee_final_payroll_payments
            (final_payroll_id,payment_date,amount,cash_account_id,journal_entry_id,reference,notes,created_by)
            VALUES(?,?,?,?,?,?,?,?)""",
            (final_id,payment_date,str(amount),cash_id,journal_id,reference,
             request.form.get("notes","").strip()[:250],session["user_id"]))
        new_paid = M(paid + amount)
        new_status = "paid" if new_paid >= D(row["net_pay"]) else "approved"
        execute_compat(c, "UPDATE employee_final_payrolls SET status=? WHERE id=? AND status IN ('approved','paid')",
                  (new_status,final_id))
        _audit(c,"employee_final_payroll_payment",
               row["final_payroll_no"] + " amount=" + str(amount) + " journal=" + str(journal_id))
        c.commit()
        flash("تم صرف دفعة المرتب النهائي وترحيل قيد السداد." if new_status=="paid"
              else "تم تسجيل دفعة جزئية للمرتب النهائي وترحيل قيدها.", "success")
        return redirect(url_for("workforce.final_payroll", period=row["period"]))
    except Exception as exc:
        c.rollback()
        flash(safe_error_message(exc), "error")
        return redirect(url_for("workforce.final_payroll"))
    finally:
        c.close()


def _payroll_basic_salary_variance_limit(c):
    row = execute_compat(c, 
        "SELECT setting_value FROM company_settings WHERE setting_key='payroll_basic_salary_variance_limit_percent'"
    ).fetchone()
    try:
        limit = D(row["setting_value"]) if row else Decimal("10")
    except (ValueError, TypeError, InvalidOperation):
        limit = Decimal("10")
    if limit < 0 or limit > 100:
        limit = Decimal("10")
    return limit


@workforce_bp.route("/hr/payroll/settings", methods=["POST"])
@_role("admin")
def save_payroll_settings():
    c = _db()
    if session.get("role") != "admin":
        c.close()
        flash("اعتماد إعدادات المرتبات متاح لمدير النظام فقط.", "error")
        return redirect(url_for("workforce.payroll"))
    try:
        limit = M(request.form.get("basic_salary_variance_limit_percent", "10"))
        if limit < 0 or limit > 100:
            raise ValueError("حد اختلاف الراتب يجب أن يكون بين صفر و100%.")
        execute_compat(c, """INSERT INTO company_settings(setting_key,setting_value)
            VALUES('payroll_basic_salary_variance_limit_percent',?)
            ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
            (str(limit),))
        _audit(c, "payroll_basic_salary_variance_limit_updated", "limit=" + str(limit))
        c.commit()
        flash("تم حفظ حد اختلاف الراتب الأساسي بنجاح.", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر حفظ إعدادات المرتبات.", "error")
    finally:
        c.close()
    return redirect(url_for("workforce.payroll"))


@workforce_bp.route("/hr/payroll/<int:run_id>/salary-approval/<int:line_id>", methods=["POST"])
@_role("admin", permission="workforce.salary_approve")
def approve_payroll_basic_salary_change(run_id, line_id):
    c = _db()
    if session.get("role") != "admin":
        c.close()
        flash("اعتماد فروق الراتب الأساسي متاح لمدير النظام فقط.", "error")
        return redirect(url_for("workforce.payroll_run", run_id=run_id))
    try:
        begin_write_transaction(c)
        run = execute_compat(c, "SELECT status,created_by FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        line = execute_compat(c, """SELECT l.*,e.full_name,e.employee_code
            FROM payroll_lines l JOIN employees e ON e.id=l.employee_id
            WHERE l.id=? AND l.run_id=?""", (line_id, run_id)).fetchone()
        if not run or run["status"] != "draft" or not line:
            raise ValueError("لا يمكن اعتماد تعديل راتب خارج مسودة مرتبات مفتوحة.")
        if run["created_by"] is None or run["created_by"] == session["user_id"]:
            raise ValueError("اعتماد فرق الراتب الأساسي متاح لمدير مختلف عن منشئ كشف المرتبات.")
        if not line["basic_salary_approval_required"]:
            raise ValueError("هذا التعديل لا يتطلب موافقة مدير.")
        if line["basic_salary_approved_by"]:
            raise ValueError("تم اعتماد تعديل الراتب بالفعل.")
        approval_note = request.form.get("approval_note", "").strip()
        if not approval_note:
            raise ValueError("اكتب ملاحظة اعتماد المدير قبل الموافقة على تعديل الراتب.")
        update_cursor = execute_compat(c, """UPDATE payroll_lines
            SET basic_salary_approved_by=?,basic_salary_approved_at=CURRENT_TIMESTAMP
            WHERE id=? AND run_id=? AND basic_salary_approval_required=1
              AND basic_salary_approved_by IS NULL""",
            (session["user_id"], line_id, run_id))
        if update_cursor.rowcount != 1:
            raise ValueError("تعذر تسجيل موافقة المدير؛ أعد تحميل الشاشة.")
        _audit(c, "payroll_basic_salary_change_approved",
               "run=" + str(run_id) + " employee=" + line["employee_code"] +
               " variance=" + str(line["basic_salary_variance_percent"]) +
               "% note=" + approval_note[:250])
        c.commit()
        flash("تم اعتماد تعديل الراتب الأساسي للموظف " + line["full_name"] + ".", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر اعتماد تعديل الراتب.", "error")
    finally:
        c.close()
    return redirect(url_for("workforce.payroll_run", run_id=run_id))


@workforce_bp.route("/hr/payroll", methods=["GET", "POST"])
@_role("admin", "accountant")
def payroll():
    c = _db()
    if request.method == "POST":
        period = request.form.get("period", "")
        try:
            start = date.fromisoformat(period + "-01")
            if period != start.strftime("%Y-%m"):
                raise ValueError()
            begin_write_transaction(c)
            run_id = insert_and_get_id(c, "INSERT INTO payroll_runs(period,status,created_by) VALUES(?,'draft',?)",
                            (period, session["user_id"]))
            month_end = (date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1) - date.resolution)
            period_end = month_end.isoformat()
            # Resigned/retired employees are excluded from ordinary payroll.
            # Their final-period salary is prepared separately in /hr/final-payroll.
            employees = execute_compat(c, """SELECT * FROM employees
                WHERE active=1 AND work_status IN ('comprehensive_bonus','permanent','temporary','unpaid_leave')
                ORDER BY employee_code""").fetchall()
            if not employees:
                raise ValueError("لا يوجد موظفون مستحقون للراتب في هذا الشهر حسب الموقف الوظيفي.")
            salary_variance_limit = _payroll_basic_salary_variance_limit(c)
            for e in employees:
                # Default to the employee master salary; monthly edits remain possible
                # but are compared against the effective master value before snapshotting.
                master_basic, master_insured_wage = _employee_salary_basis(c, e["id"], period_end)
                basic_raw = request.form.get("basic_" + str(e["id"]), "")
                basic_salary = M(basic_raw) if basic_raw is not None and str(basic_raw).strip() else master_basic
                raise_amount = Decimal("0")
                salary_reference = master_basic
                if e["raise_effective_date"] and e["raise_effective_date"] <= (period + "-01"):
                    proposed_raise = (M(master_basic * D(e["raise_value"]) / Decimal("100"))
                                      if e["raise_type"] == "percentage" else M(e["raise_value"]))
                    expected_after_raise = M(master_basic + proposed_raise) if e["raise_mode"] == "basic" else master_basic
                    salary_reference = expected_after_raise if e["raise_mode"] == "basic" else master_basic
                    # Apply only when the monthly form still contains the unchanged master salary.
                    # If the user already entered the raised/current salary, never add it twice.
                    if e["raise_mode"] == "basic" and basic_salary == master_basic:
                        raise_amount = proposed_raise
                        basic_salary = M(basic_salary + raise_amount)
                    elif e["raise_mode"] == "basic" and basic_salary == expected_after_raise:
                        raise_amount = proposed_raise
                    elif e["raise_mode"] == "allowance" and basic_salary == master_basic:
                        raise_amount = proposed_raise
                if basic_salary <= 0 and e["work_status"] != "unpaid_leave":
                    raise ValueError("الراتب الأساسي للموظف " + e["full_name"] + " يجب أن يكون أكبر من صفر.")
                salary_change_reason = request.form.get("basic_override_reason_" + str(e["id"]), "").strip()
                if e["work_status"] == "unpaid_leave":
                    salary_difference = Decimal("0")
                    salary_variance_percent = Decimal("0.00")
                else:
                    salary_difference = abs(M(basic_salary - salary_reference))
                    salary_variance_percent = (
                        M(salary_difference * Decimal("100") / salary_reference)
                        if salary_reference > 0 else (Decimal("100.00") if salary_difference else Decimal("0.00"))
                    )
                salary_changed = salary_difference > Decimal("0") and e["work_status"] != "unpaid_leave"
                if salary_changed and not salary_change_reason:
                    raise ValueError("اكتب سبب تعديل الراتب الأساسي للموظف " + e["full_name"] + ".")
                salary_approval_required = int(
                    salary_changed and (
                        salary_reference <= 0 or salary_variance_percent > salary_variance_limit
                    )
                )
                monthly_basic_salary = basic_salary
                payroll_days = 30
                if e["work_status"] in {"resigned", "retired"}:
                    end_date = date.fromisoformat(e["termination_date"])
                    payroll_days = min(30, max(0, (end_date - start).days + 1))
                    if payroll_days <= 0:
                        raise ValueError("تاريخ انتهاء الخدمة لا يقع ضمن شهر المرتبات للموظف " + e["full_name"] + ".")
                payroll_factor = Decimal(payroll_days) / Decimal("30")
                employee_period_end = e["termination_date"] if e["work_status"] in {"resigned", "retired"} else period_end
                fixed_items = execute_compat(c, """SELECT p.id,p.item_name,p.item_type,p.amount FROM employee_pay_items p
                    WHERE p.employee_id=? AND p.active=1
                      AND p.id=(SELECT MAX(p2.id) FROM employee_pay_items p2
                                WHERE p2.employee_id=p.employee_id
                                  AND p2.item_name=p.item_name
                                  AND p2.item_type=p.item_type
                                  AND p2.active=1)
                    ORDER BY p.id""", (e["id"],)).fetchall()
                fixed_item_amounts = {}
                for fixed_item in fixed_items:
                    fixed_amount = M(request.form.get("pay_item_" + str(fixed_item["id"]), fixed_item["amount"]))
                    if fixed_amount < 0:
                        raise ValueError("لا يمكن أن يكون بند " + fixed_item["item_name"] + " سالبًا للموظف " + e["full_name"] + ".")
                    fixed_item_amounts[fixed_item["id"]] = fixed_amount
                fixed_allowances = M(sum((fixed_item_amounts[x["id"]] for x in fixed_items if x["item_type"] == "allowance"), Decimal("0")))
                fixed_deductions = M(sum((fixed_item_amounts[x["id"]] for x in fixed_items if x["item_type"] == "deduction"), Decimal("0")))
                fixed_pay_deductions = fixed_deductions
                allowances = M(fixed_allowances)
                if raise_amount and e["raise_mode"] == "allowance":
                    allowances = M(allowances + raise_amount)
                overtime_hours = D(request.form.get("overtime_hours_" + str(e["id"]), "0"))
                unpaid_days = Decimal("0")
                if request.form.get("use_attendance_data") == "1":
                    attendance_rows = execute_compat(c, """SELECT status,absence_hours,approved_overtime_hours
                        FROM attendance_records WHERE employee_id=? AND attendance_date>=? AND attendance_date<=?""",
                        (e["id"],period+"-01",employee_period_end)).fetchall()
                    # Count absence hours only on days explicitly marked absent.
                    # Hours entered against leave/present/holiday records must not create a second deduction.
                    attendance_absent_hours = M(sum(
                        (D(x["absence_hours"]) for x in attendance_rows if x["status"] == "absent"),
                        Decimal("0")))
                    attendance_overtime_hours = M(sum(
                        (D(x["approved_overtime_hours"]) for x in attendance_rows),
                        Decimal("0")))
                    overtime_hours = attendance_overtime_hours
                overtime_rate = D(request.form.get("overtime_rate_percent_" + str(e["id"]), e["overtime_rate_percent"]))
                overtime_raw = request.form.get("overtime_amount_" + str(e["id"]), "").strip()
                overtime_override_reason = request.form.get("overtime_override_reason_" + str(e["id"]), "").strip()
                if overtime_rate < 0 or overtime_rate > 500:
                    raise ValueError("نسبة الإضافي يجب أن تكون بين صفر و500.")
                overtime_calculated_amount = M(D(overtime_hours) * monthly_basic_salary / D(e["standard_hours"]) * overtime_rate / Decimal("100"))
                overtime_manual_override = bool(overtime_raw) and M(overtime_raw) != overtime_calculated_amount
                if overtime_manual_override and not overtime_override_reason and e["work_status"] != "unpaid_leave":
                    raise ValueError("اكتب سبب اختلاف مبلغ الإضافي اليدوي عن المحسوب للموظف " + e["full_name"] + ".")
                overtime_amount = M(overtime_raw) if overtime_manual_override else overtime_calculated_amount
                absent_hours = D(request.form.get("absent_hours_" + str(e["id"]), "0"))
                absence = M(request.form.get("absence_deduction_" + str(e["id"]), "0"))
                if request.form.get("use_attendance_data") == "1":
                    absent_hours = attendance_absent_hours
                    next_month = date(start.year + (start.month==12), 1 if start.month==12 else start.month+1, 1)
                    month_end = (next_month-date.resolution).isoformat()
                    unpaid_requests = execute_compat(c, """SELECT r.start_date,r.end_date,r.days FROM employee_leave_requests r
                        JOIN leave_types t ON t.id=r.leave_type_id
                        WHERE r.employee_id=? AND r.status='approved' AND t.is_paid=0
                        AND r.start_date<=? AND r.end_date>=?""",(e["id"],employee_period_end,period+"-01")).fetchall()
                    unpaid_days = Decimal("0")
                    for leave_row in unpaid_requests:
                        leave_start=date.fromisoformat(leave_row["start_date"])
                        leave_end=date.fromisoformat(leave_row["end_date"])
                        overlap_start=max(leave_start,start)
                        overlap_end=min(leave_end,date.fromisoformat(employee_period_end))
                        total_days=Decimal((leave_end-leave_start).days+1)
                        overlap_days=Decimal((overlap_end-overlap_start).days+1)
                        unpaid_days += D(leave_row["days"])*overlap_days/total_days
                    # Payroll uses a transparent 30-day divisor for approved unpaid leave.
                    unpaid_days = min(Decimal("30"),unpaid_days)
                    absence = M(absent_hours*basic_salary/D(e["standard_hours"]) +
                                unpaid_days*basic_salary/Decimal("30"))
                # Grants and exceptional bonuses are paid outside monthly payroll.
                # Recurring allowances and salary raises remain part of payroll.
                incentives = Decimal("0")
                # Prorate monthly salary and recurring allowances for a mid-month termination.
                # Loan installments remain contractual amounts and are not prorated by service days.
                if payroll_days < 30:
                    basic_salary = M(monthly_basic_salary * payroll_factor)
                    allowances = M(allowances * payroll_factor)
                    raise_amount = M(raise_amount * payroll_factor)
                manual_advance_deduction = Decimal("0")
                due_installments = execute_compat(c, """SELECT i.due_amount,i.paid_amount
                    FROM employee_loan_installments i JOIN employee_loans l ON l.id=i.loan_id
                    WHERE l.employee_id=? AND l.status='active' AND i.due_period<=? AND i.status='pending'
                    ORDER BY l.id,i.id""", (e["id"], period)).fetchall()
                loan_deduction = M(sum((max(Decimal("0"),D(i["due_amount"])-D(i["paid_amount"]))
                                         for i in due_installments),Decimal("0")))
                advance_deduction = M(manual_advance_deduction + loan_deduction)
                other_deductions = M(request.form.get("other_deductions_" + str(e["id"]), "0"))
                other_deductions = M(other_deductions + fixed_deductions)
                penalty_rows = []
                penalty_deduction = Decimal("0")
                if table_exists(c, "employee_penalties"):
                    penalty_rows = execute_compat(c, """SELECT id,amount,duration_days,reason FROM employee_penalties
                        WHERE employee_id=? AND effective_period=? AND status='approved' ORDER BY id""",
                        (e["id"],period)).fetchall()
                    for penalty in penalty_rows:
                        penalty_amount = M(penalty["amount"] or "0")
                        if penalty_amount <= 0 and D(penalty["duration_days"]) > 0:
                            penalty_amount = M(basic_salary * D(penalty["duration_days"]) / Decimal("30"))
                        penalty_deduction = M(penalty_deduction + penalty_amount)
                tax = M(request.form.get("tax_" + str(e["id"]), "0"))
                stamp_duty = M(request.form.get("stamp_duty_" + str(e["id"]), "0"))
                emp_ins = M(request.form.get("employee_ins_" + str(e["id"]), "0"))
                employer_ins = M(request.form.get("employer_ins_" + str(e["id"]), "0"))
                # Absence is a deduction, not part of gross wage.
                gross = M(basic_salary + allowances + overtime_amount + incentives)
                rule_set_id = None
                taxable_annual = Decimal("0")
                insured_wage = M(request.form.get(
                    "insured_wage_" + str(e["id"]),
                    str(master_insured_wage if master_insured_wage > 0 else basic_salary)
                ))
                if request.form.get("auto_calculate_statutory") == "1" and e["work_status"] != "unpaid_leave":
                    from payroll_compliance import calculate_payroll_deductions
                    statutory_employee = dict(e)
                    # The monthly payroll input is authoritative for this run; do not
                    # silently calculate insurance from a stale employee-master wage.
                    statutory_employee["insured_wage"] = str(insured_wage)
                    automatic = calculate_payroll_deductions(
                        c, statutory_employee, gross, period,
                        taxable_gross_monthly=M(max(Decimal("0"), gross - absence))
                    )
                    if automatic is None:
                        raise ValueError("لا توجد قواعد ضريبية وتأمينية سارية للموظف " + e["full_name"] + ". راجع إعدادات قواعد الرواتب أو ألغِ خيار الحساب التلقائي.")
                    tax = automatic["tax"]
                    emp_ins = automatic["employee_insurance"]
                    employer_ins = automatic["employer_insurance"]
                    rule_set_id = automatic["rule_set_id"]
                    taxable_annual = automatic["taxable_annual"]
                    insured_wage = automatic["insured_wage"]
                # Collect scheduled loan installments only up to the employee's available net pay.
                # Any unpaid remainder stays on the installment and rolls forward to later payrolls.
                fixed_deductions = M(employer_ins + emp_ins + tax + stamp_duty + manual_advance_deduction + other_deductions + absence + penalty_deduction)
                available_for_loan = M(max(Decimal("0"),gross + employer_ins - fixed_deductions))
                loan_due_total = M(sum((max(Decimal("0"),D(i["due_amount"])-D(i["paid_amount"]))
                                        for i in due_installments),Decimal("0")))
                loan_deduction = min(loan_due_total,available_for_loan)
                advance_deduction = M(manual_advance_deduction + loan_deduction)
                total_entitlements = M(gross + employer_ins)
                total_deductions = M(employer_ins + emp_ins + tax + stamp_duty + advance_deduction + other_deductions + absence + penalty_deduction)
                net = M(total_entitlements - total_deductions)
                # A full-month unpaid-leave employment status is retained in the payroll
                # snapshot for traceability, but generates no earnings, deductions,
                # statutory charges, loan recovery, overtime, or incentives. Approved
                # salary changes remain on the employee master for future paid periods.
                if e["work_status"] == "unpaid_leave":
                    basic_salary = monthly_basic_salary = allowances = raise_amount = Decimal("0")
                    fixed_allowances = fixed_deductions = fixed_pay_deductions = Decimal("0")
                    fixed_item_amounts = {key: Decimal("0") for key in fixed_item_amounts}
                    overtime_hours = overtime_amount = overtime_calculated_amount = Decimal("0")
                    overtime_manual_override = False
                    absent_hours = absence = unpaid_days = Decimal("0")
                    manual_advance_deduction = loan_deduction = advance_deduction = Decimal("0")
                    other_deductions = penalty_deduction = Decimal("0")
                    penalty_rows = []
                    tax = stamp_duty = emp_ins = employer_ins = Decimal("0")
                    gross = total_entitlements = total_deductions = net = Decimal("0")
                    insured_wage = taxable_annual = Decimal("0")
                    rule_set_id = None
                if min(basic_salary, allowances, overtime_hours, overtime_amount, absent_hours,
                       absence, incentives, advance_deduction, other_deductions, tax, stamp_duty, emp_ins,
                       employer_ins, gross, total_entitlements, total_deductions, net) < 0:
                    raise ValueError("لا يمكن أن تكون الأجور أو الساعات أو الاستقطاعات أو صافي المرتب سالبة.")
                payroll_line_id = insert_and_get_id(c, """INSERT INTO payroll_lines(run_id,employee_id,basic_salary,allowances,overtime_hours,
                    overtime_amount,absent_hours,absence_deduction,other_deductions,taxable_income_tax,
                    employee_insurance,employer_insurance,gross_pay,net_pay,stamp_duty,rule_set_id,taxable_annual,insured_wage,
                    incentives,advance_deduction,loan_deduction,total_entitlements,total_deductions,salary_raise,
                    overtime_rate_percent,incentive_rule_id,incentive_name,incentive_days,incentive_method,
                    overtime_calculated_amount,overtime_manual_override,overtime_override_reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id,e["id"],str(basic_salary),str(allowances),str(overtime_hours),str(overtime_amount),
                     str(absent_hours),str(absence),str(other_deductions),str(tax),str(emp_ins),
                     str(employer_ins),str(gross),str(net),str(stamp_duty),
                     rule_set_id, str(taxable_annual), str(insured_wage),
                     str(incentives),str(advance_deduction),str(loan_deduction),str(total_entitlements),str(total_deductions),
                     str(raise_amount),str(overtime_rate),None,"",
                      "0","",str(overtime_calculated_amount),
                     int(overtime_manual_override),overtime_override_reason))
                salary_reference_snapshot = (
                    Decimal("0") if e["work_status"] == "unpaid_leave"
                    else (M(salary_reference * payroll_factor) if payroll_days < 30 else salary_reference)
                )
                execute_compat(c, """UPDATE payroll_lines SET
                    basic_salary_reference=?,basic_salary_variance_percent=?,
                    basic_salary_variance_limit_percent=?,basic_salary_change_reason=?,
                    basic_salary_approval_required=?
                    WHERE id=?""",
                    (str(salary_reference_snapshot), str(salary_variance_percent),
                     str(salary_variance_limit), salary_change_reason,
                     salary_approval_required, payroll_line_id))
                if salary_changed:
                    _audit(c, "payroll_basic_salary_changed",
                           "run=" + str(run_id) + " employee=" + e["employee_code"] +
                           " reference=" + str(salary_reference) +
                           " entered=" + str(monthly_basic_salary) +
                           " variance=" + str(salary_variance_percent) +
                           "% limit=" + str(salary_variance_limit) +
                           "% reason=" + salary_change_reason[:250])
                if table_exists(c, "payroll_line_items"):
                    execute_compat(c, "UPDATE payroll_lines SET penalty_deduction=? WHERE id=?",
                              (str(penalty_deduction),payroll_line_id))
                    def snapshot_item(section, item_name, amount, source_type="payroll_input", source_id=None, sort_order=0):
                        value = M(amount)
                        if value:
                            execute_compat(c, """INSERT INTO payroll_line_items
                                (payroll_line_id,section,item_name,amount,source_type,source_id,sort_order)
                                VALUES(?,?,?,?,?,?,?)""",
                                (payroll_line_id,section,item_name,str(value),source_type,source_id,sort_order))
                    snapshot_item("earning","الراتب الأساسي",basic_salary,"employee_salary",e["id"],10)
                    if raise_amount:
                        snapshot_item("information","العلاوة المطبقة (مدرجة ضمن الأساسي/البدلات)",raise_amount,
                                      "salary_raise",e["id"],15)
                    for fixed_item in fixed_items:
                        fixed_value = fixed_item_amounts[fixed_item["id"]]
                        item_amount = M(fixed_value * payroll_factor) if fixed_item["item_type"] == "allowance" else fixed_value
                        if fixed_item["item_type"] == "allowance":
                            snapshot_item("earning",fixed_item["item_name"],item_amount,"employee_pay_item",fixed_item["id"],20)
                        else:
                            snapshot_item("deduction",fixed_item["item_name"],item_amount,"employee_pay_item",fixed_item["id"],70)
                    if raise_amount and e["raise_mode"] == "allowance":
                        snapshot_item("earning","علاوة مستقلة",M(raise_amount * payroll_factor),"salary_raise",e["id"],35)
                    snapshot_item("earning","عمل إضافي",overtime_amount,"attendance_or_manual",None,50)
                    snapshot_item("deduction","خصم الغياب والإجازة بدون أجر",absence,"attendance_leave",None,60)
                    snapshot_item("deduction","سلفة / دفعة مقدمة يدوية",manual_advance_deduction,"payroll_input",None,75)
                    snapshot_item("deduction","أقساط السلف والقروض",loan_deduction,"loan_installments",None,80)
                    for penalty in penalty_rows:
                        penalty_amount = M(penalty["amount"] or "0")
                        if penalty_amount <= 0 and D(penalty["duration_days"]) > 0:
                            penalty_amount = M(basic_salary * D(penalty["duration_days"]) / Decimal("30"))
                        snapshot_item("deduction","جزاء: "+penalty["reason"],penalty_amount,"employee_penalty",penalty["id"],85)
                    manual_other = M(max(Decimal("0"),D(other_deductions)-fixed_pay_deductions))
                    snapshot_item("deduction","خصومات أخرى يدوية",manual_other,"payroll_input",None,90)
                    snapshot_item("deduction","ضريبة المرتبات",tax,"payroll_compliance",None,100)
                    snapshot_item("deduction","الدمغة",stamp_duty,"payroll_input",None,105)
                    snapshot_item("deduction","حصة الموظف في التأمينات",emp_ins,"payroll_compliance",None,110)
                    snapshot_item("employer_cost","حصة الشركة في التأمينات",employer_ins,"payroll_compliance",None,120)
            _audit(c, "payroll_draft_created", period)
            c.commit()
            g.audit_created_record = ("payroll_runs", run_id, "payroll_lines", "run_id")
            flash("تم حفظ المرتبات كمسودة. راجع البنود ثم اعتمدها.", "success")
            return redirect(url_for("workforce.payroll_run", run_id=run_id))
        except _DB_VALUE_INVALID_KEY_ERRORS as e:
            c.rollback()
            error_text = str(e)
            if isinstance(e, DB_INTEGRITY_ERROR) and "payroll_runs.period" in error_text:
                flash("يوجد كشف مرتبات مسجل بالفعل لهذا الشهر. افتح الكشف الموجود أو راجع حالته قبل إنشاء كشف جديد.", "error")
            else:
                flash(safe_error_message(e, "تعذر إعداد المرتبات بسبب تعارض في البيانات. لم يتم حفظ كشف المرتبات."), "error")
    display_period = date.today().strftime("%Y-%m")
    # Final-period employees are handled on the separate final-payroll screen.
    employee_rows = execute_compat(c, """SELECT * FROM employees
        WHERE active=1 AND work_status IN ('comprehensive_bonus','permanent','temporary','unpaid_leave')
        ORDER BY employee_code""").fetchall()
    employees = []
    for employee_row in employee_rows:
        employee = dict(employee_row)
        current_basic, current_insured = _employee_salary_basis(c, employee["id"], date.today().isoformat())
        employee["basic_salary"] = str(current_basic)
        employee["insured_wage"] = str(current_insured)
        fixed_rows = execute_compat(c, """SELECT item_name,item_type,amount FROM employee_pay_items
            WHERE employee_id=? AND active=1 ORDER BY id""", (employee["id"],)).fetchall()
        employee["fixed_allowance_total"] = M(sum(
            (D(x["amount"]) for x in fixed_rows if x["item_type"] == "allowance"), Decimal("0")))
        employee["fixed_deduction_total"] = M(sum(
            (D(x["amount"]) for x in fixed_rows if x["item_type"] == "deduction"), Decimal("0")))
        employee["fixed_pay_items_summary"] = "، ".join(
            x["item_name"] + ": " + str(M(x["amount"])) +
            (" بدل" if x["item_type"] == "allowance" else " خصم")
            for x in fixed_rows
        )
        employee["fixed_pay_items"] = [dict(x) for x in fixed_rows]
        employees.append(employee)
    today_period = display_period
    runs_page = fetch_page(c,"SELECT * FROM payroll_runs ORDER BY period DESC,id DESC",
        "SELECT COUNT(*) FROM payroll_runs",
        page=request.args.get("runs_page",1,type=int),
        per_page=request.args.get("runs_per_page",50,type=int))
    runs = runs_page["rows"]
    basic_salary_variance_limit = _payroll_basic_salary_variance_limit(c)
    c.close()
    return render_template("payroll.html", employees=employees, runs=runs, runs_page=runs_page,
                           today=today_period, basic_salary_variance_limit=basic_salary_variance_limit)


@workforce_bp.route("/hr/payroll/<int:run_id>", methods=["GET"])
@_role("admin", "accountant", "viewer")
def payroll_run(run_id):
    c = _db()
    run = execute_compat(c, "SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        c.close()
        flash("بيانات المرتبات غير موجودة.", "error")
        return redirect(url_for("workforce.payroll"))
    rows = execute_compat(c, """SELECT l.*,e.employee_code,e.full_name,e.department FROM payroll_lines l
                        JOIN employees e ON e.id=l.employee_id WHERE l.run_id=? ORDER BY e.employee_code""", (run_id,)).fetchall()
    accounts = _accounts(c)
    payment_accounts = execute_compat(c, """SELECT a.id,a.code,a.name_ar,a.account_type
        FROM accounts a JOIN cash_accounts ca ON ca.account_id=a.id
        WHERE a.active=1 AND a.is_group=0 AND a.account_type='asset' AND ca.active=1
        ORDER BY a.code""").fetchall()
    allocation_count_expr = "(SELECT COUNT(*) FROM payroll_payment_allocations pa WHERE pa.payment_id=p.id)" if table_exists(c, "payroll_payment_allocations") else "0"
    payments = execute_compat(c, """SELECT p.*,a.code AS account_code,a.name_ar AS account_name,"""+
        allocation_count_expr+""" AS allocation_count
        FROM payroll_payments p JOIN accounts a ON a.id=p.cash_account_id
        WHERE p.run_id=? ORDER BY p.payment_date,p.id""", (run_id,)).fetchall()
    line_paid = {}
    if table_exists(c, "payroll_payment_allocations"):
        for payroll_row in rows:
            line_paid[payroll_row["id"]] = M(sum((D(x["amount"]) for x in execute_compat(c, 
                "SELECT amount FROM payroll_payment_allocations WHERE payroll_line_id=?",
                (payroll_row["id"],)).fetchall()),Decimal("0")))
    reversal = execute_compat(c, """SELECT r.*,j.reference FROM payroll_reversals r
        JOIN journal_entries j ON j.id=r.reversal_journal_entry_id WHERE r.run_id=?""",
        (run_id,)).fetchone()
    paid_amount = M(sum((D(p["amount"]) for p in payments), Decimal("0")))
    totals = {
        "basic_salary": M(sum((D(x["basic_salary"]) for x in rows), Decimal("0"))),
        "allowances": M(sum((D(x["allowances"]) for x in rows), Decimal("0"))),
        "overtime_amount": M(sum((D(x["overtime_amount"]) for x in rows), Decimal("0"))),
        "absence_deduction": M(sum((D(x["absence_deduction"]) for x in rows), Decimal("0"))),
        "gross_pay": M(sum((D(x["gross_pay"]) for x in rows), Decimal("0"))),
        "salary_raise": M(sum((D(x["salary_raise"]) for x in rows), Decimal("0"))),
        "incentives": M(sum((D(x["incentives"]) for x in rows), Decimal("0"))),
        "advance_deduction": M(sum((D(x["advance_deduction"]) for x in rows), Decimal("0"))),
        "total_entitlements": M(sum((D(x["total_entitlements"]) for x in rows), Decimal("0"))),
        "total_deductions": M(sum((D(x["total_deductions"]) for x in rows), Decimal("0"))),
        "other_deductions": M(sum((D(x["other_deductions"]) for x in rows), Decimal("0"))),
        "tax": M(sum((D(x["taxable_income_tax"]) for x in rows), Decimal("0"))),
        "stamp_duty": M(sum((D(x["stamp_duty"]) if "stamp_duty" in x.keys() else Decimal("0") for x in rows), Decimal("0"))),
        "employee_insurance": M(sum((D(x["employee_insurance"]) for x in rows), Decimal("0"))),
        "employer_insurance": M(sum((D(x["employer_insurance"]) for x in rows), Decimal("0"))),
        "penalty_deduction": M(sum((D(x["penalty_deduction"]) if "penalty_deduction" in x.keys() else Decimal("0") for x in rows), Decimal("0"))),
        "net_pay": M(sum((D(x["net_pay"]) for x in rows), Decimal("0"))),
    }
    remaining_payable = M(max(Decimal("0"), totals["net_pay"] - paid_amount))
    period_start = date.fromisoformat(run["period"] + "-01")
    next_month = date(period_start.year + (period_start.month == 12),
                      1 if period_start.month == 12 else period_start.month + 1, 1)
    period_end = (next_month - date.resolution).isoformat()
    c.close()
    return render_template("payroll_run.html", run=run, rows=rows, accounts=accounts,
                           payment_accounts=payment_accounts, payments=payments, reversal=reversal,
                           can_reverse=(run["status"] == "posted" and paid_amount == 0 and reversal is None),
                           paid_amount=paid_amount, remaining_payable=remaining_payable,
                           today=date.today().isoformat(), totals=totals, period_end=period_end,
                           line_paid=line_paid)



@workforce_bp.route("/hr/payroll/<int:run_id>/employee/<int:employee_id>/payslip", methods=["GET"])
@_role("admin", "accountant", "viewer")
def payroll_employee_payslip(run_id, employee_id):
    """Printable employee payslip based exclusively on the saved payroll snapshot."""
    c = _db()
    row = execute_compat(c, """
        SELECT r.id AS run_id,r.period,r.status,
               e.id AS employee_id,e.employee_code,e.full_name,e.job_title,e.department,
               e.hire_date,e.insurance_number,
               l.*
        FROM payroll_runs r
        JOIN payroll_lines l ON l.run_id=r.id
        JOIN employees e ON e.id=l.employee_id
        WHERE r.id=? AND e.id=?
    """, (run_id, employee_id)).fetchone()
    if not row:
        c.close()
        flash("قسيمة الراتب غير موجودة لهذا الموظف في هذه المرتبات.", "error")
        return redirect(url_for("workforce.payroll"))
    if row["status"] not in {"posted", "reversed"}:
        c.close()
        flash("القسيمة النهائية متاحة بعد ترحيل المرتبات فقط (بعد اعتماد وترحيل المرتبات).", "error")
        return redirect(url_for("workforce.payroll_run", run_id=run_id))

    data = dict(row)
    loan_deduction = M(data.get("loan_deduction") or "0")
    total_advance = M(data.get("advance_deduction") or "0")
    data["loan_deduction"] = loan_deduction
    data["manual_advance_deduction"] = M(max(Decimal("0"), total_advance - loan_deduction))
    data["employee_deductions"] = M(
        D(data["absence_deduction"]) + total_advance + D(data["other_deductions"])
        + D(data.get("penalty_deduction") or "0")
        + D(data["taxable_income_tax"]) + D(data.get("stamp_duty") or "0")
        + D(data["employee_insurance"])
    )
    data["net_pay"] = M(data["net_pay"])
    data["total_entitlements"] = M(data["total_entitlements"])
    data["total_deductions"] = M(data["total_deductions"])
    data["gross_pay"] = M(data["gross_pay"])
    detail_items = []
    if table_exists(c, "payroll_line_items"):
        detail_items = execute_compat(c, """SELECT * FROM payroll_line_items WHERE payroll_line_id=?
            ORDER BY section,sort_order,id""",(row["id"],)).fetchall()
    payments = []
    if table_exists(c, "payroll_payment_allocations"):
        payments = execute_compat(c, """SELECT a.amount,p.payment_date,p.reference,p.journal_entry_id
            FROM payroll_payment_allocations a JOIN payroll_payments p ON p.id=a.payment_id
            WHERE a.payroll_line_id=? ORDER BY p.payment_date,p.id""",(row["id"],)).fetchall()
    data["paid_amount"] = M(sum((D(p["amount"]) for p in payments),Decimal("0")))
    data["remaining_amount"] = M(max(Decimal("0"),D(data["net_pay"])-data["paid_amount"]))
    c.close()
    return render_template("payroll_payslip.html", slip=data, detail_items=detail_items,payments=payments)


@workforce_bp.route("/hr/payroll/<int:run_id>/print", methods=["GET"])
@_role("admin", "accountant", "viewer")
def print_payroll_run(run_id):
    c = _db()
    run = execute_compat(c, "SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        c.close()
        flash("بيانات المرتبات غير موجودة.", "error")
        return redirect(url_for("workforce.payroll"))
    rows = execute_compat(c, """SELECT l.*,e.employee_code,e.full_name,e.department
        FROM payroll_lines l JOIN employees e ON e.id=l.employee_id
        WHERE l.run_id=? ORDER BY e.employee_code""", (run_id,)).fetchall()
    if not rows:
        c.close()
        flash("لا توجد تفاصيل لطباعة المرتبات.", "error")
        return redirect(url_for("workforce.payroll_run", run_id=run_id))
    details = {}
    has_items = table_exists(c, "payroll_line_items")
    for row in rows:
        details[row["id"]] = execute_compat(c, """SELECT section,item_name,amount FROM payroll_line_items
            WHERE payroll_line_id=? ORDER BY section,sort_order,id""", (row["id"],)).fetchall() if has_items else []
    fields = ("basic_salary","salary_raise","allowances","overtime_amount","gross_pay","insured_wage",
              "employer_insurance","total_entitlements","employee_insurance","taxable_income_tax",
              "stamp_duty","advance_deduction","other_deductions","absence_deduction",
              "penalty_deduction","total_deductions","net_pay")
    totals = {key: M(sum((D(row[key] or "0") for row in rows), Decimal("0"))) for key in fields}
    c.close()
    return render_template("payroll_run_print.html", run=run, rows=rows, details=details, totals=totals)


@workforce_bp.route("/hr/payroll/<int:run_id>/delete", methods=["POST"])
@_role("admin", "accountant")
def delete_payroll_run(run_id):
    c = _db()
    try:
        begin_write_transaction(c)
        run = execute_compat(c, "SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "draft" or run["journal_entry_id"]:
            raise ValueError("لا يمكن حذف كشف المرتبات بعد اعتماده أو إقفاله. الحذف متاح للمسودة فقط.")
        if execute_compat(c, "SELECT 1 FROM payroll_payments WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
            raise ValueError("لا يمكن حذف كشف مرتبات مرتبط بعملية صرف.")
        line_ids=[int(x["id"]) for x in execute_compat(c, "SELECT id FROM payroll_lines WHERE run_id=?", (run_id,)).fetchall()]
        if line_ids:
            marks=",".join("?" for _ in line_ids)
            allocations=execute_compat(c, """SELECT a.amount,i.id AS installment_id,i.due_amount,
                    i.paid_amount,i.loan_id
                FROM payroll_loan_allocations a
                JOIN employee_loan_installments i ON i.id=a.installment_id
                WHERE a.payroll_line_id IN ("""+marks+")", line_ids).fetchall()
            affected_loans=set()
            for allocation in allocations:
                restored=max(Decimal("0"),D(allocation["paid_amount"])-D(allocation["amount"]))
                execute_compat(c, "UPDATE employee_loan_installments SET paid_amount=?,status=? WHERE id=?",
                          (str(M(restored)),"paid" if restored>=D(allocation["due_amount"]) else "pending",
                           allocation["installment_id"]))
                affected_loans.add(int(allocation["loan_id"]))
            for loan_id in affected_loans:
                loan=execute_compat(c, "SELECT approved_amount FROM employee_loans WHERE id=?",(loan_id,)).fetchone()
                if loan:
                    paid_total=M(sum((D(x["paid_amount"]) for x in execute_compat(c, 
                        "SELECT paid_amount FROM employee_loan_installments WHERE loan_id=?",(loan_id,)).fetchall()),
                        Decimal("0")))
                    balance=M(max(Decimal("0"),D(loan["approved_amount"])-paid_total))
                    execute_compat(c, "UPDATE employee_loans SET remaining_balance=?,status=? WHERE id=?",
                              (str(balance),"closed" if balance<=0 else "active",loan_id))
            execute_compat(c, "DELETE FROM payroll_loan_allocations WHERE payroll_line_id IN ("+marks+")", line_ids)
        for dependent_table in ("payroll_line_items","payroll_payment_allocations"):
            if table_exists(c, dependent_table):
                execute_compat(c, "DELETE FROM "+dependent_table+" WHERE payroll_line_id IN ("+
                          (",".join("?" for _ in line_ids) if line_ids else "NULL")+")", line_ids)
        execute_compat(c, "DELETE FROM payroll_lines WHERE run_id=?", (run_id,))
        delete_cursor = execute_compat(c, "DELETE FROM payroll_runs WHERE id=? AND status='draft' AND journal_entry_id IS NULL", (run_id,))
        if delete_cursor.rowcount != 1:
            raise ValueError("لم يتم حذف كشف المرتبات؛ ربما تم اعتماده في عملية أخرى.")
        _audit(c, "payroll_draft_deleted", "run=" + str(run_id) + " period=" + run["period"])
        c.commit()
        flash("تم حذف مسودة المرتبات المفتوحة فقط.", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر حذف مسودة المرتبات.", "error")
    c.close()
    return redirect(url_for("workforce.payroll"))


@workforce_bp.route("/hr/payroll/<int:run_id>/post", methods=["POST"])
@_role("admin", "accountant")
def post_payroll(run_id):
    c = _db()
    try:
        # Acquire the write lock before reading the run status. This prevents two
        # concurrent requests from both observing a draft and posting it twice.
        begin_write_transaction(c)
        run = execute_compat(c, "SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "draft" or run["approval_status"] != "pending":
            raise ValueError("كشف المرتبات غير موجود أو ليس بانتظار الاعتماد.")
        if run["created_by"] is None or run["created_by"] == session["user_id"]:
            raise ValueError("لا يجوز اعتماد وترحيل المرتبات بواسطة منشئ الكشف، أو دون بيانات منشئ موثقة.")
        rows = execute_compat(c, """SELECT l.*,e.branch_id,e.cost_center_id,e.project_id,e.full_name,e.employee_code
            FROM payroll_lines l JOIN employees e ON e.id=l.employee_id WHERE l.run_id=?
            ORDER BY e.employee_code""", (run_id,)).fetchall()
        if not rows:
            raise ValueError("لا توجد تفاصيل للمرتبات.")
        pending_salary_approvals = [
            row for row in rows
            if row["basic_salary_approval_required"] and not row["basic_salary_approved_by"]
        ]
        if pending_salary_approvals:
            names = "، ".join(row["full_name"] for row in pending_salary_approvals[:5])
            raise ValueError("لا يمكن ترحيل المرتبات قبل موافقة المدير على فروق الراتب الأساسي التي تجاوزت الحد المحدد: " + names)
        salary_expense = int(request.form["salary_expense_account"])
        salaries_payable = int(request.form["salaries_payable_account"])
        tax_payable = int(request.form["tax_payable_account"])
        insurance_payable = int(request.form["insurance_payable_account"])
        employer_ins_expense = int(request.form["employer_ins_expense_account"])
        net = sum((D(x["net_pay"]) for x in rows), Decimal("0"))
        tax = sum((D(x["taxable_income_tax"]) for x in rows), Decimal("0"))
        stamp_duty = sum((D(x["stamp_duty"]) if "stamp_duty" in x.keys() else Decimal("0") for x in rows), Decimal("0"))
        emp_ins = sum((D(x["employee_insurance"]) for x in rows), Decimal("0"))
        employer_ins = sum((D(x["employer_insurance"]) for x in rows), Decimal("0"))
        other_deductions = sum((D(x["other_deductions"]) for x in rows), Decimal("0"))
        penalty_deduction = sum((D(x["penalty_deduction"]) if "penalty_deduction" in x.keys() else Decimal("0") for x in rows), Decimal("0"))
        advance_deduction = sum((D(x["advance_deduction"]) for x in rows), Decimal("0"))
        other_deductions_account = int(request.form["other_deductions_account"])
        advances_account = int(request.form["advances_account"])
        total_loan_deduction = M(sum((D(x["loan_deduction"]) for x in rows),Decimal("0")))
        # Validate account nature server-side; form filtering alone is not an accounting control.
        required_account_types = (
            (salary_expense, "expense"), (employer_ins_expense, "expense"),
            (salaries_payable, "liability"), (tax_payable, "liability"),
            (insurance_payable, "liability"),
        )
        for selected_id, expected_type in required_account_types:
            selected_account = execute_compat(c, 
                "SELECT account_type,is_group,active FROM accounts WHERE id=?", (selected_id,)
            ).fetchone()
            if (not selected_account or not selected_account["active"] or selected_account["is_group"]
                    or selected_account["account_type"] != expected_type):
                raise ValueError("الحسابات المختارة غير صحيحة؛ اختر حساب مصروف تفصيليًا للأجور والتأمين، وحساب التزام تفصيليًا للمستحقات والضرائب والتأمينات.")
        if advance_deduction > 0:
            _employee_advance_account(c, advances_account)
        if total_loan_deduction > 0:
            loan_installment_rows = execute_compat(c, """SELECT DISTINCT l.loan_account_id,
                    i.due_amount,i.paid_amount
                FROM employee_loans l
                JOIN employee_loan_installments i ON i.loan_id=l.id
                JOIN payroll_lines pl ON pl.employee_id=l.employee_id
                WHERE pl.run_id=? AND l.status='active' AND i.due_period<=? AND i.status='pending'""",
                (run_id, run["period"])).fetchall()
            loan_account_ids = {
                row["loan_account_id"] for row in loan_installment_rows
                if D(row["due_amount"]) > D(row["paid_amount"])
            }
            if None in loan_account_ids or loan_account_ids != {advances_account}:
                raise ValueError("اختر نفس حساب سلف الموظفين المسجل عند صرف القرض؛ لا يجوز ترحيل قسط القرض إلى حساب مختلف.")
        lines = []
        for payroll_line in rows:
            dims = {key: payroll_line[key] for key in ("branch_id", "cost_center_id", "project_id")}
            employee_label = payroll_line["employee_code"] + " - " + payroll_line["full_name"]
            gross_cost = M(payroll_line["gross_pay"])
            absence_cost = M(payroll_line["absence_deduction"])
            if gross_cost:
                lines.append((salary_expense, gross_cost, 0, "أجور ومرتبات - " + employee_label, dims))
            if absence_cost:
                lines.append((salary_expense, 0, absence_cost, "تخفيض مصروف الغياب - " + employee_label, dims))
            employer_cost = M(payroll_line["employer_insurance"])
            if employer_cost:
                lines.append((employer_ins_expense, employer_cost, 0, "حصة الشركة بالتأمينات - " + employee_label, dims))
        lines.extend([
            (salaries_payable, 0, M(net), "صافي مستحقات الموظفين"),
            (advances_account, 0, M(advance_deduction), "استرداد سلف العاملين"),
            (other_deductions_account, 0, M(other_deductions), "الحساب المقابل للخصومات الأخرى"),
            (other_deductions_account, 0, M(penalty_deduction), "جزاءات الموظفين"),
            (tax_payable, 0, M(tax), "ضريبة مرتبات مستحقة"),
            (tax_payable, 0, M(stamp_duty), "دمغة مستحقة على المرتبات"),
            (insurance_payable, 0, M(emp_ins + employer_ins), "تأمينات اجتماعية مستحقة"),
        ])
        # Include the immutable payroll run ID in the reference. A reversed payroll
        # journal remains posted in the ledger for auditability, so a corrected
        # replacement run must not collide with the original journal reference.
        payroll_reference = "PAYROLL-" + run["period"] + "-RUN-" + str(run_id)
        journal_id = post_journal(c, run["period"] + "-01", payroll_reference,
                                  "مرتبات شهر " + run["period"], lines, session["user_id"])
        for payroll_line in rows:
            left = D(payroll_line["loan_deduction"])
            if left <= 0:
                continue
            due_rows = execute_compat(c, """SELECT i.id,i.due_amount,i.paid_amount
                FROM employee_loan_installments i JOIN employee_loans l ON l.id=i.loan_id
                WHERE l.employee_id=(SELECT employee_id FROM payroll_lines WHERE id=?)
                  AND l.status='active' AND i.due_period<=? AND i.status='pending'
                ORDER BY i.due_period,l.id,i.id""", (payroll_line["id"],run["period"])).fetchall()
            for item in due_rows:
                outstanding = max(Decimal("0"),D(item["due_amount"])-D(item["paid_amount"]))
                amount = min(left,outstanding)
                if amount <= 0:
                    continue
                execute_compat(c, "INSERT INTO payroll_loan_allocations(payroll_line_id,installment_id,amount) VALUES(?,?,?)",
                          (payroll_line["id"],item["id"],str(M(amount))))
                paid = M(D(item["paid_amount"])+amount)
                execute_compat(c, "UPDATE employee_loan_installments SET paid_amount=?,status=? WHERE id=?",
                          (str(paid),"paid" if paid>=D(item["due_amount"]) else "pending",item["id"]))
                left = M(left-amount)
                if left <= 0:
                    break
            if left > 0:
                raise ValueError("تعذر توزيع خصم قسط السلفة على جدول الأقساط.")
        for loan in execute_compat(c, "SELECT id,approved_amount FROM employee_loans WHERE status='active'").fetchall():
            paid = M(sum((D(x["paid_amount"]) for x in execute_compat(c, 
                "SELECT paid_amount FROM employee_loan_installments WHERE loan_id=?",(loan["id"],)).fetchall()),Decimal("0")))
            balance = M(max(Decimal("0"),D(loan["approved_amount"])-paid))
            execute_compat(c, "UPDATE employee_loans SET remaining_balance=?,status=? WHERE id=?",
                      (str(balance),"closed" if balance<=0 else "active",loan["id"]))
        if table_exists(c, "employee_penalties"):
            for payroll_line in rows:
                if "penalty_deduction" in payroll_line.keys() and D(payroll_line["penalty_deduction"]) > 0:
                    execute_compat(c, """UPDATE employee_penalties SET status='deducted',payroll_line_id=?,decided_by=COALESCE(decided_by,?)
                        WHERE employee_id=? AND effective_period=? AND status='approved'""",
                        (payroll_line["id"],session["user_id"],payroll_line["employee_id"],run["period"]))
        update_cursor = execute_compat(c, """UPDATE payroll_runs SET status='posted',journal_entry_id=?,
            approved_by=?,approved_at=CURRENT_TIMESTAMP,approval_status='approved',
            posted_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='draft' AND approval_status='pending'""",
            (journal_id,session["user_id"],run_id))
        if update_cursor.rowcount != 1:
            raise ValueError("تم تغيير حالة كشف المرتبات؛ أعد تحميل الصفحة.")
        _audit(c, "payroll_posted", run["period"] + " journal=" + str(journal_id))
        c.commit()
        flash("تم ترحيل المرتبات وإنشاء القيد المحاسبي.", "success")
    except _DB_VALUE_KEY_ERRORS as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("workforce.payroll_run", run_id=run_id))


@workforce_bp.route("/hr/payroll/<int:run_id>/reverse", methods=["POST"])
@_role("admin", "accountant")
def reverse_payroll(run_id):
    c = _db()
    try:
        begin_write_transaction(c)
        run = execute_compat(c, "SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "posted" or not run["journal_entry_id"]:
            raise ValueError("لا يمكن عكس قيد مرتبات غير مرحّل. لا يمكن عكس قيد مرتبات غير مرحّل أو سبق عكسه.")
        if execute_compat(c, "SELECT 1 FROM payroll_reversals WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError("تم عكس قيد المرتبات هذا من قبل.")
        payments = execute_compat(c, "SELECT COUNT(*) FROM payroll_payments WHERE run_id=?", (run_id,)).fetchone()[0]
        if payments:
            raise ValueError("لا يمكن عكس قيد المرتبات مع وجود دفعات صرف مسجلة. لا يمكن عكس قيد المرتبات مع وجود دفعات صرف مسجلة؛ سوِّ الدفعات أولًا ثم راجع حسابات الموظفين.")
        reversal_date = date.fromisoformat(request.form.get("reversal_date") or date.today().isoformat()).isoformat()
        reason = request.form.get("reason", "").strip()
        if not reason:
            raise ValueError("سبب عكس قيد المرتبات مطلوب ويجب توثيقه.")
        available_columns = table_columns(c, "journal_lines")
        dimension_keys = ("branch_id", "cost_center_id", "project_id", "customer_id", "salesperson_id", "supplier_id")
        selected_dimensions = [key for key in dimension_keys if key in available_columns]
        select_columns = ["account_id", "debit", "credit", "memo"] + selected_dimensions
        original_lines = execute_compat(c, 
            "SELECT " + ",".join(select_columns) + " FROM journal_lines WHERE entry_id=? ORDER BY id",
            (run["journal_entry_id"],)
        ).fetchall()
        if len(original_lines) < 2:
            raise ValueError("القيد الأصلي غير مكتمل ولا يمكن عكسه بأمان.")
        reversal_lines = []
        for line in original_lines:
            dimensions = {key: line[key] for key in selected_dimensions if line[key] not in (None, "")}
            reversal_lines.append((
                int(line["account_id"]), M(line["credit"]), M(line["debit"]),
                "عكس قيد المرتبات: " + str(line["memo"] or ""), dimensions
            ))
        reference = "PAYROLL-REV-" + str(run_id)
        reversal_journal_id = post_journal(
            c, reversal_date, reference,
            "عكس مرتبات شهر " + run["period"] + " — " + reason,
            reversal_lines, session["user_id"]
        )
        execute_compat(c, """INSERT INTO payroll_reversals(run_id,reversal_date,reason,
            reversal_journal_entry_id,created_by) VALUES(?,?,?,?,?)""",
            (run_id, reversal_date, reason, reversal_journal_id, session["user_id"]))
        # Restore loan installments allocated by this payroll because its salary deduction is now reversed.
        allocations = execute_compat(c, """SELECT a.installment_id,a.amount,i.loan_id,i.paid_amount,i.due_amount
            FROM payroll_loan_allocations a
            JOIN payroll_lines pl ON pl.id=a.payroll_line_id
            JOIN employee_loan_installments i ON i.id=a.installment_id
            WHERE pl.run_id=? ORDER BY a.id""", (run_id,)).fetchall()
        affected_loans = set()
        for allocation in allocations:
            restored_paid = max(Decimal("0"), D(allocation["paid_amount"]) - D(allocation["amount"]))
            execute_compat(c, "UPDATE employee_loan_installments SET paid_amount=?,status='pending' WHERE id=?",
                      (str(M(restored_paid)), allocation["installment_id"]))
            affected_loans.add(int(allocation["loan_id"]))
        for loan_id in affected_loans:
            loan = execute_compat(c, "SELECT approved_amount FROM employee_loans WHERE id=?", (loan_id,)).fetchone()
            if not loan:
                continue
            total_paid = M(sum((D(item["paid_amount"]) for item in execute_compat(c, 
                "SELECT paid_amount FROM employee_loan_installments WHERE loan_id=?", (loan_id,)).fetchall()), Decimal("0")))
            remaining = M(max(Decimal("0"), D(loan["approved_amount"]) - total_paid))
            execute_compat(c, "UPDATE employee_loans SET remaining_balance=?,status=? WHERE id=?",
                      (str(remaining), "closed" if remaining <= 0 else "active", loan_id))
        if table_exists(c, "employee_penalties"):
            execute_compat(c, """UPDATE employee_penalties SET status='approved',payroll_line_id=NULL
                WHERE payroll_line_id IN (SELECT id FROM payroll_lines WHERE run_id=?) AND status='deducted'""",
                (run_id,))
        execute_compat(c, "UPDATE payroll_runs SET status='reversed' WHERE id=?", (run_id,))
        _audit(c, "payroll_run_reversed", reference + " reason=" + reason)
        c.commit()
        flash("تم إنشاء قيد عكسي مستقل للمرتبات. القيد الأصلي وتفاصيل المرتبات لم يتم حذفها أو تعديلها.", "success")
    except _DB_VALUE_INVALID_KEY_ERRORS as exc:
        c.rollback()
        flash(str(exc), "error")
    c.close()
    return redirect(url_for("workforce.payroll_run", run_id=run_id))


@workforce_bp.route("/hr/payroll/<int:run_id>/payment", methods=["POST"])
@_role("admin", "accountant")
def pay_payroll(run_id):
    c = _db()
    try:
        run = execute_compat(c, "SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "posted":
            raise ValueError("لا يمكن صرف المرتبات قبل ترحيلها.")
        rows = execute_compat(c, "SELECT net_pay FROM payroll_lines WHERE run_id=?", (run_id,)).fetchall()
        if not rows:
            raise ValueError("لا توجد تفاصيل للمرتبات.")
        net_payable = M(sum((D(row["net_pay"]) for row in rows), Decimal("0")))
        paid = M(sum((D(row["amount"]) for row in execute_compat(c, 
            "SELECT amount FROM payroll_payments WHERE run_id=?", (run_id,)).fetchall()), Decimal("0")))
        amount = M(request.form.get("amount"))
        if amount <= 0:
            raise ValueError("مبلغ صرف المرتب يجب أن يكون أكبر من صفر.")
        if amount > net_payable - paid:
            raise ValueError("مبلغ الصرف يتجاوز صافي المرتبات المتبقي.")
        payment_date = date.fromisoformat(request.form.get("payment_date") or date.today().isoformat()).isoformat()
        cash_account_id = int(request.form["cash_account_id"])
        _cash_or_bank_account(c, cash_account_id)
        reference = "PAYROLL-PAY-" + str(run_id) + "-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        begin_write_transaction(c)
        # Re-read the payroll state after acquiring the write lock. A reversal may
        # have completed after the initial page read but before this payment began.
        run = execute_compat(c, "SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "posted" or not run["journal_entry_id"]:
            raise ValueError("لا يمكن صرف مرتبات تم عكسها أو لم تعد مرحّلة.")
        if run["approved_by"] is not None and run["approved_by"] == session["user_id"]:
            raise ValueError("يجب أن ينفذ صرف المرتبات مستخدم مختلف عن معتمد كشف المرتبات.")
        rows = execute_compat(c, "SELECT net_pay FROM payroll_lines WHERE run_id=?", (run_id,)).fetchall()
        if not rows:
            raise ValueError("لا توجد تفاصيل للمرتبات.")
        net_payable = M(sum((D(row["net_pay"]) for row in rows), Decimal("0")))
        # Recheck outstanding value inside the write transaction to prevent concurrent overpayment.
        paid_now = M(sum((D(row["amount"]) for row in execute_compat(c, 
            "SELECT amount FROM payroll_payments WHERE run_id=?", (run_id,)).fetchall()), Decimal("0")))
        if amount > net_payable - paid_now:
            raise ValueError("مبلغ الصرف يتجاوز صافي المرتبات المتبقي بعد احتساب الدفعات الأخرى.")
        journal_id = post_journal(c, payment_date, reference,
            "صرف مرتبات شهر " + run["period"],
            [(int(execute_compat(c, """SELECT jl.account_id FROM journal_lines jl
                WHERE jl.entry_id=? AND jl.memo='صافي مستحقات الموظفين' AND jl.CAST(credit AS NUMERIC)>0
                ORDER BY jl.id LIMIT 1""", (run["journal_entry_id"],)).fetchone()[0]),
              amount, 0, "إقفال صافي مستحقات الموظفين"),
             (cash_account_id, 0, amount, "صرف صافي المرتبات من البنك/الصندوق")],
            session["user_id"])
        payment_id = insert_and_get_id(c, """INSERT INTO payroll_payments(run_id,payment_date,amount,cash_account_id,
            journal_entry_id,reference,notes,created_by) VALUES(?,?,?,?,?,?,?,?)""",
            (run_id,payment_date,str(amount),cash_account_id,journal_id,reference,
             request.form.get("notes","").strip(),session["user_id"]))
        if request.form.get("allocation_mode") == "manual":
            allocation_rows = execute_compat(c, "SELECT id,employee_id,net_pay FROM payroll_lines WHERE run_id=? ORDER BY id",(run_id,)).fetchall()
            allocation_total = Decimal("0")
            pending_allocations = []
            for line in allocation_rows:
                allocation = M(request.form.get("allocation_" + str(line["id"]),"0"))
                already = M(sum((D(x["amount"]) for x in execute_compat(c, 
                    "SELECT amount FROM payroll_payment_allocations WHERE payroll_line_id=?",(line["id"],)).fetchall()),Decimal("0")))
                if allocation < 0 or allocation > M(D(line["net_pay"])-already):
                    raise ValueError("توزيع الصرف على الموظف يتجاوز صافي مستحقه المتبقي.")
                allocation_total += allocation
                if allocation:
                    pending_allocations.append((payment_id,line["id"],str(allocation)))
            if M(allocation_total) != amount:
                raise ValueError("يجب أن يساوي مجموع توزيعات الموظفين مبلغ دفعة الصرف بالضبط.")
            for allocation in pending_allocations:
                execute_compat(c, """INSERT INTO payroll_payment_allocations(payment_id,payroll_line_id,amount)
                    VALUES(?,?,?)""",allocation)
        _audit(c, "payroll_payment_posted", reference + " amount=" + str(amount))
        c.commit()
        flash("تم صرف دفعة المرتبات وترحيل قيدها بنجاح.", "success")
    except _DB_VALUE_KEY_ERRORS as exc:
        c.rollback()
        flash(str(exc), "error")
    c.close()
    return redirect(url_for("workforce.payroll_run", run_id=run_id))


@workforce_bp.route("/hr/loans", methods=["GET", "POST"])
@_role("admin", "accountant")
def employee_loans():
    c = _db()
    if request.method == "POST":
        try:
            employee_id = int(request.form["employee_id"])
            amount = M(request.form.get("requested_amount", "0"))
            count = int(request.form.get("installment_count", "0"))
            first = request.form.get("first_due_period", "")
            date.fromisoformat(first + "-01")
            if not execute_compat(c, "SELECT 1 FROM employees WHERE id=? AND active=1", (employee_id,)).fetchone() or amount <= 0 or not 1 <= count <= 60:
                raise ValueError("راجع الموظف ومبلغ السلفة وعدد الأقساط من 1 إلى 60.")
            loan_no = "EMP-LOAN-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
            execute_compat(c, """INSERT INTO employee_loans(loan_no,employee_id,request_date,requested_amount,
                installment_count,first_due_period,notes,created_by) VALUES(?,?,?,?,?,?,?,?)""",
                (loan_no,employee_id,date.today().isoformat(),str(amount),count,first,
                 request.form.get("notes","").strip(),session["user_id"]))
            _audit(c,"employee_loan_requested",loan_no)
            c.commit()
            flash("تم تسجيل طلب السلفة في انتظار الموافقة.","success")
        except _DB_VALUE_KEY_ERRORS as exc:
            c.rollback()
            flash(str(exc) or "تعذر تسجيل طلب السلفة.","error")
        c.close()
        return redirect(url_for("workforce.employee_loans"))
    loans = execute_compat(c, """SELECT l.*,e.employee_code,e.full_name FROM employee_loans l
        JOIN employees e ON e.id=l.employee_id ORDER BY l.id DESC""").fetchall()
    employees = execute_compat(c, "SELECT id,employee_code,full_name FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    cash_accounts = execute_compat(c, """SELECT a.id,a.code,a.name_ar FROM accounts a
        JOIN cash_accounts ca ON ca.account_id=a.id
        WHERE a.active=1 AND a.is_group=0 AND a.account_type='asset' AND ca.active=1
        ORDER BY a.code""").fetchall()
    loan_accounts = execute_compat(c, """SELECT a.id,a.code,a.name_ar FROM accounts a
        WHERE a.active=1 AND a.is_group=0 AND a.account_type='asset'
          AND NOT EXISTS(SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id)
        ORDER BY a.code""").fetchall()
    c.close()
    return render_template("employee_loans.html",loans=loans,employees=employees,
        cash_accounts=cash_accounts,loan_accounts=loan_accounts,today=date.today().strftime("%Y-%m"))


@workforce_bp.route("/hr/loans/<int:loan_id>/approve", methods=["POST"])
@_role("admin", "accountant", permission="workforce.approve")
def approve_employee_loan(loan_id):
    c = _db()
    try:
        begin_write_transaction(c)
        loan = execute_compat(c, "SELECT * FROM employee_loans WHERE id=?", (loan_id,)).fetchone()
        if not loan or loan["status"] != "requested":
            raise ValueError("طلب السلفة غير موجود أو سبق اتخاذ قرار.")
        if loan["created_by"] is None or loan["created_by"] == session["user_id"]:
            raise ValueError("لا يجوز اعتماد طلب السلفة بواسطة منشئه، أو دون بيانات منشئ موثقة.")
        amount = M(request.form.get("approved_amount",loan["requested_amount"]))
        count = int(request.form.get("installment_count",loan["installment_count"]))
        first = request.form.get("first_due_period",loan["first_due_period"])
        date.fromisoformat(first + "-01")
        if amount <= 0 or amount > D(loan["requested_amount"]) or not 1 <= count <= 60:
            raise ValueError("المبلغ المعتمد لا يتجاوز المطلوب وعدد الأقساط من 1 إلى 60.")
        update_cursor = execute_compat(c, """UPDATE employee_loans SET approved_amount=?,installment_count=?,first_due_period=?,
            status='approved',approved_by=?,approved_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='requested'""",
            (str(amount),count,first,session["user_id"],loan_id))
        if update_cursor.rowcount != 1:
            raise ValueError("تم اتخاذ قرار على طلب السلفة بواسطة مستخدم آخر.")
        _audit(c,"employee_loan_approved",loan["loan_no"])
        c.commit()
        flash("تم اعتماد السلفة؛ يلزم صرفها لإنشاء جدول الأقساط.","success")
    except _DB_VALUE_KEY_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر اعتماد الطلب.","error")
    c.close()
    return redirect(url_for("workforce.employee_loans"))


@workforce_bp.route("/hr/loans/<int:loan_id>/reject", methods=["POST"])
@_role("admin", "accountant", permission="workforce.approve")
def reject_employee_loan(loan_id):
    c = _db()
    try:
        begin_write_transaction(c)
        loan = execute_compat(c, "SELECT * FROM employee_loans WHERE id=?", (loan_id,)).fetchone()
        if not loan or loan["status"] != "requested":
            raise ValueError("لا يمكن رفض الطلب في حالته الحالية.")
        if loan["created_by"] is None or loan["created_by"] == session["user_id"]:
            raise ValueError("لا يجوز رفض طلب السلفة بواسطة منشئه، أو دون بيانات منشئ موثقة.")
        update_cursor = execute_compat(c, """UPDATE employee_loans SET status='rejected',approved_by=?,approved_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='requested'""", (session["user_id"],loan_id))
        if update_cursor.rowcount != 1:
            raise ValueError("تم اتخاذ قرار على طلب السلفة بواسطة مستخدم آخر.")
        _audit(c,"employee_loan_rejected",loan["loan_no"])
        c.commit()
        flash("تم رفض الطلب مع الاحتفاظ بسجله.","success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر رفض الطلب.","error")
    c.close()
    return redirect(url_for("workforce.employee_loans"))


@workforce_bp.route("/hr/loans/<int:loan_id>/disburse", methods=["POST"])
@_role("admin", "accountant")
def disburse_employee_loan(loan_id):
    c = _db()
    try:
        loan = execute_compat(c, """SELECT l.*,e.full_name FROM employee_loans l
            JOIN employees e ON e.id=l.employee_id WHERE l.id=?""",(loan_id,)).fetchone()
        if not loan or loan["status"] != "approved":
            raise ValueError("لا يمكن صرف إلا سلفة معتمدة لم تُصرف.")
        if loan["approved_by"] is not None and loan["approved_by"] == session["user_id"]:
            raise ValueError("يجب أن ينفذ صرف السلفة مستخدم مختلف عن معتمدها.")
        loan_account_id = _employee_advance_account(c, int(request.form["loan_account_id"]))
        cash_id = int(request.form["cash_account_id"])
        _cash_or_bank_account(c, cash_id)
        if cash_id == loan_account_id:
            raise ValueError("لا يجوز صرف السلفة من حساب السلف نفسه.")
        disb_date = date.fromisoformat(request.form.get("disbursement_date") or date.today().isoformat()).isoformat()
        amount = M(loan["approved_amount"])
        cents = int(amount * 100)
        base, rem = divmod(cents, int(loan["installment_count"]))
        begin_write_transaction(c)
        journal = post_journal(c,disb_date,"EMP-LOAN-DISB-"+str(loan_id),
            "صرف سلفة للموظف "+loan["full_name"],
            [(loan_account_id,amount,0,"إثبات سلفة موظف"),(cash_id,0,amount,"صرف سلفة من البنك/الصندوق")],
            session["user_id"])
        first = date.fromisoformat(loan["first_due_period"] + "-01")
        for idx in range(int(loan["installment_count"])):
            month = first.year * 12 + first.month - 1 + idx
            period = f"{month//12:04d}-{month%12+1:02d}"
            due = Decimal(base + (1 if idx < rem else 0)) / 100
            execute_compat(c, "INSERT INTO employee_loan_installments(loan_id,due_period,due_amount) VALUES(?,?,?)",
                      (loan_id,period,str(M(due))))
        execute_compat(c, """UPDATE employee_loans SET status='active',remaining_balance=?,loan_account_id=?,
            disbursement_account_id=?,disbursement_date=?,journal_entry_id=? WHERE id=?""",
            (str(amount),loan_account_id,cash_id,disb_date,journal,loan_id))
        _audit(c,"employee_loan_disbursed",loan["loan_no"]+" journal="+str(journal))
        c.commit()
        flash("تم صرف السلفة وترحيل القيد وإنشاء جدول الأقساط.","success")
    except _DB_VALUE_KEY_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر صرف السلفة.","error")
    c.close()
    return redirect(url_for("workforce.employee_loans"))


@workforce_bp.route("/hr/loans/<int:loan_id>/installments/<int:installment_id>/defer", methods=["POST"])
@_role("admin", "accountant")
def defer_loan_installment(loan_id, installment_id):
    c = _db()
    try:
        begin_write_transaction(c)
        installment = execute_compat(c, """SELECT i.*,l.employee_id,l.status AS loan_status,l.loan_no
            FROM employee_loan_installments i JOIN employee_loans l ON l.id=i.loan_id
            WHERE i.id=? AND i.loan_id=?""",(installment_id,loan_id)).fetchone()
        if not installment or installment["loan_status"] != "active" or installment["status"] != "pending":
            raise ValueError("القسط غير موجود أو ليس قسطًا نشطًا قابلًا للتأجيل.")
        # Once the employee's payroll for this installment month is posted,
        # the installment has been used in an approved payroll and must not be changed.
        locked_payroll = execute_compat(c, """SELECT 1 FROM payroll_loan_allocations a
            JOIN payroll_lines pl ON pl.id=a.payroll_line_id
            JOIN payroll_runs r ON r.id=pl.run_id
            WHERE a.installment_id=? AND r.status='posted' LIMIT 1""",
            (installment_id,)).fetchone()
        if locked_payroll:
            raise ValueError("لا يمكن تعديل أو تأجيل قسط السلفة؛ تم اعتماد مرتب الموظف عن شهر " +
                             installment["due_period"] + ". يجب عكس كشف المرتبات وفق الإجراءات المحاسبية أولًا.")
        remaining = M(max(Decimal("0"),D(installment["due_amount"])-D(installment["paid_amount"])))
        if remaining <= 0:
            raise ValueError("لا يوجد رصيد متبقٍ في هذا القسط لتأجيله.")
        old_period = date.fromisoformat(installment["due_period"]+"-01")
        next_month = old_period.year*12+old_period.month
        next_period = f"{next_month//12:04d}-{next_month%12+1:02d}"
        target = execute_compat(c, """SELECT id,due_amount,paid_amount FROM employee_loan_installments
            WHERE loan_id=? AND due_period=?""",(loan_id,next_period)).fetchone()
        if target:
            new_due = M(D(target["due_amount"])+remaining)
            execute_compat(c, "UPDATE employee_loan_installments SET due_amount=? WHERE id=?",(str(new_due),target["id"]))
        else:
            execute_compat(c, """INSERT INTO employee_loan_installments(loan_id,due_period,due_amount,paid_amount,status)
                VALUES(?,?,?,'0','pending')""",(loan_id,next_period,str(remaining)))
        # Keep the original row for audit; its already-paid amount remains untouched.
        execute_compat(c, "UPDATE employee_loan_installments SET due_amount=paid_amount WHERE id=?",(installment_id,))
        _audit(c,"employee_loan_installment_deferred",
               installment["loan_no"]+" "+installment["due_period"]+" -> "+next_period+" amount="+str(remaining))
        c.commit()
        flash("تم تأجيل القسط إلى "+next_period+" مع بقاء المبلغ ضمن رصيد السلفة دون إسقاطه.","success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر تأجيل القسط.","error")
    c.close()
    return redirect(url_for("workforce.loan_schedule",loan_id=loan_id))


@workforce_bp.route("/hr/loans/<int:loan_id>/schedule", methods=["GET"])
@_role("admin", "accountant", "viewer")
def loan_schedule(loan_id):
    c = _db()
    loan = execute_compat(c, """SELECT l.*,e.employee_code,e.full_name FROM employee_loans l
        JOIN employees e ON e.id=l.employee_id WHERE l.id=?""",(loan_id,)).fetchone()
    if not loan:
        c.close()
        flash("السلفة غير موجودة.","error")
        return redirect(url_for("workforce.employee_loans"))
    installments = execute_compat(c, "SELECT * FROM employee_loan_installments WHERE loan_id=? ORDER BY due_period",(loan_id,)).fetchall()
    locked_installment_ids = {int(row["installment_id"]) for row in execute_compat(c, """SELECT DISTINCT a.installment_id
        FROM payroll_loan_allocations a
        JOIN payroll_lines pl ON pl.id=a.payroll_line_id
        JOIN payroll_runs r ON r.id=pl.run_id
        WHERE pl.employee_id=? AND r.status='posted'""",(loan["employee_id"],)).fetchall()}
    c.close()
    return render_template("employee_loan_schedule.html",loan=loan,installments=installments,
                           locked_installment_ids=locked_installment_ids)


@workforce_bp.route("/hr/grants", methods=["GET"])
@_role("admin", "accountant", "viewer")
def employee_grants():
    c = _db()
    grant_types = execute_compat(c, "SELECT * FROM employee_grant_types WHERE active=1 ORDER BY name").fetchall()
    employee_rows = execute_compat(c, """SELECT id,employee_code,full_name,basic_salary,work_status,department FROM employees
        WHERE active=1 AND work_status NOT IN ('unpaid_leave','resigned','retired') ORDER BY employee_code""").fetchall()
    employees = []
    for employee_row in employee_rows:
        item = dict(employee_row)
        item["basic_salary"] = str(_employee_salary_basis(c, item["id"], date.today().isoformat())[0])
        employees.append(item)
    accounts = _accounts(c)
    grant_rows = execute_compat(c, """SELECT g.*,t.name AS grant_type_name
        FROM employee_grants g JOIN employee_grant_types t ON t.id=g.grant_type_id
        ORDER BY g.id DESC LIMIT 100""").fetchall()
    grants = []
    grant_lines = {}
    for row in grant_rows:
        grant = dict(row)
        paid_total = M(sum((D(x["amount"]) for x in execute_compat(c, 
            "SELECT amount FROM employee_grant_payments WHERE grant_id=?", (grant["id"],)).fetchall()), Decimal("0")))
        grant["paid_total"] = str(paid_total)
        grant["outstanding_total"] = str(M(D(grant["total_amount"])-paid_total))
        line_rows = execute_compat(c, "SELECT * FROM employee_grant_lines WHERE grant_id=? ORDER BY employee_code_snapshot",
            (grant["id"],)).fetchall()
        grant_lines[grant["id"]] = []
        for line in line_rows:
            item = dict(line)
            item["remaining_amount"] = str(M(D(item["amount"])-D(item["paid_amount"])))
            grant_lines[grant["id"]].append(item)
        grants.append(grant)
    c.close()
    return render_template("employee_grants.html",grant_types=grant_types,employees=employees,accounts=accounts,
                           grants=grants,grant_lines=grant_lines,today=date.today().isoformat())


@workforce_bp.route("/hr/grants/types", methods=["POST"])
@_role("admin", "accountant")
def add_employee_grant_type():
    c = _db()
    try:
        name = request.form.get("name","").strip()
        if not name or len(name)>120:
            raise ValueError("اكتب اسم المنحة بحد أقصى 120 حرفًا.")
        execute_compat(c, "INSERT INTO employee_grant_types(name) VALUES(?)",(name,))
        _audit(c,"employee_grant_type_created",name)
        c.commit()
        flash("تم حفظ اسم المنحة.", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) if isinstance(exc,ValueError) else "اسم المنحة موجود بالفعل أو تعذر حفظه.","error")
    c.close()
    return redirect(url_for("workforce.employee_grants"))


@workforce_bp.route("/hr/grants/create", methods=["POST"])
@_role("admin", "accountant")
def create_employee_grant():
    c = _db()
    try:
        grant_type_id = int(request.form["grant_type_id"])
        grant_type = execute_compat(c, "SELECT * FROM employee_grant_types WHERE id=? AND active=1",(grant_type_id,)).fetchone()
        if not grant_type: raise ValueError("اختر اسم منحة نشطًا.")
        method = request.form.get("calculation_method","")
        if method not in {"fixed","percentage_basic","per_day"}: raise ValueError("طريقة حساب المنحة غير صحيحة.")
        value = M(request.form.get("calculation_value","0"))
        days_count = D(request.form.get("days_count","0"))
        if (method!="per_day" and value<=0) or days_count<0 or (method=="per_day" and days_count<=0):
            raise ValueError("أدخل نسبة أو مبلغًا موجبًا، وحدد عدد الأيام عند اختيار طريقة الأيام.")
        expense_id=int(request.form["expense_account_id"]); payable_id=int(request.form["payable_account_id"])
        expense=execute_compat(c, "SELECT id,account_type,is_group,active FROM accounts WHERE id=?",(expense_id,)).fetchone()
        payable=execute_compat(c, "SELECT id,account_type,is_group,active FROM accounts WHERE id=?",(payable_id,)).fetchone()
        if not expense or not expense["active"] or expense["is_group"] or expense["account_type"]!="expense":
            raise ValueError("اختر حساب مصروف منح تفصيليًا ونشطًا.")
        if not payable or not payable["active"] or payable["is_group"] or payable["account_type"]!="liability" or expense_id==payable_id:
            raise ValueError("اختر حساب منح مستحقة تفصيليًا ونشطًا ومختلفًا عن حساب المصروف.")
        selected_scopes=sorted(set(x.strip() for x in request.form.getlist("employee_scopes") if x.strip()))
        if not selected_scopes:
            legacy_scope=request.form.get("employee_scope","all_eligible")
            selected_scopes=["all_eligible" if legacy_scope=="all_eligible" else legacy_scope]
        scope_statuses={
            "permanent": ("permanent",),
            "temporary": ("temporary",),
            "comprehensive_bonus": ("comprehensive_bonus",),
            "all_eligible": ("permanent","temporary","comprehensive_bonus"),
        }
        if "all_eligible" in selected_scopes:
            selected_scopes=["all_eligible"]
        if not selected_scopes or any(x not in scope_statuses for x in selected_scopes):
            raise ValueError("حدد فئة واحدة على الأقل: مثبت أو مؤقت أو مكافأة شاملة.")
        statuses=tuple(sorted({status for scope_key in selected_scopes for status in scope_statuses[scope_key]}))
        ids=sorted({int(x) for x in request.form.getlist("employee_ids")})
        if request.form.get("all_employees")=="1" and not ids:
            marks=",".join("?" for _ in statuses)
            employees=execute_compat(c, f"""SELECT id,employee_code,full_name,basic_salary FROM employees
                WHERE active=1 AND work_status IN ({marks}) ORDER BY employee_code""",statuses).fetchall()
        else:
            if not ids: raise ValueError("أضف جميع الموظفين المستحقين أو اختر موظفين محددين.")
            id_marks=",".join("?" for _ in ids)
            status_marks=",".join("?" for _ in statuses)
            employees=execute_compat(c, f"""SELECT id,employee_code,full_name,basic_salary FROM employees
                WHERE id IN ({id_marks}) AND active=1 AND work_status IN ({status_marks})
                ORDER BY employee_code""",(*ids,*statuses)).fetchall()
            if len(employees)!=len(ids):
                raise ValueError("يوجد موظف غير نشط أو لا ينتمي إلى فئة الاستحقاق المحددة.")
        if not employees: raise ValueError("لا يوجد موظفون مستحقون للمنحة.")
        grant_date=date.fromisoformat(request.form.get("grant_date") or date.today().isoformat()).isoformat()
        snapshots=[]
        for emp in employees:
            basic=_employee_salary_basis(c, emp["id"], grant_date)[0]
            amount=M(basic*value/Decimal("100")) if method=="percentage_basic" else (M((basic/Decimal("30"))*days_count) if method=="per_day" else value)
            if amount<=0: raise ValueError("قيمة المنحة الناتجة يجب أن تكون أكبر من صفر.")
            snapshots.append((emp,basic,amount))
        total=M(sum((x[2] for x in snapshots),Decimal("0")))
        begin_write_transaction(c)
        grant_id=insert_and_get_id(c, """INSERT INTO employee_grants(grant_type_id,grant_name_snapshot,calculation_method,calculation_value,
            days_count,expense_account_id,payable_account_id,grant_date,total_amount,notes,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (grant_type_id,grant_type["name"],method,str(value),str(M(days_count)),expense_id,payable_id,
             grant_date,str(total),request.form.get("notes","").strip(),session["user_id"]))
        for emp,basic,amount in snapshots:
            execute_compat(c, """INSERT INTO employee_grant_lines(grant_id,employee_id,employee_code_snapshot,
                employee_name_snapshot,basic_salary_snapshot,amount) VALUES(?,?,?,?,?,?)""",
                (grant_id,emp["id"],emp["employee_code"],emp["full_name"],str(basic),str(amount)))
        _audit(c,"employee_grant_draft_created","grant="+str(grant_id)+" total="+str(total))
        c.commit()
        flash("تم إعداد كشف المنحة كمسودة. راجع المبالغ قبل الاعتماد.", "success")
    except _DB_VALUE_KEY_ERRORS as exc:
        c.rollback(); flash(str(exc) or "تعذر إعداد المنحة.","error")
    c.close()
    return redirect(url_for("workforce.employee_grants"))


@workforce_bp.route("/hr/grants/<int:grant_id>/delete", methods=["POST"])
@_role("admin", "accountant")
def delete_employee_grant(grant_id):
    c = _db()
    try:
        begin_write_transaction(c)
        grant = execute_compat(c, "SELECT * FROM employee_grants WHERE id=?", (grant_id,)).fetchone()
        if not grant or grant["status"] != "draft" or grant["journal_entry_id"]:
            raise ValueError("لا يمكن حذف الحافز بعد اعتماده أو إغلاقه. الحذف متاح للمسودة المفتوحة فقط.")
        if execute_compat(c, "SELECT 1 FROM employee_grant_payments WHERE grant_id=? LIMIT 1", (grant_id,)).fetchone():
            raise ValueError("لا يمكن حذف حافز له أي عملية صرف مسجلة.")
        execute_compat(c, "DELETE FROM employee_grant_lines WHERE grant_id=?", (grant_id,))
        delete_cursor = execute_compat(c, "DELETE FROM employee_grants WHERE id=? AND status='draft' AND journal_entry_id IS NULL", (grant_id,))
        if delete_cursor.rowcount != 1:
            raise ValueError("لم يتم حذف الحافز؛ ربما تم إغلاقه في عملية أخرى.")
        _audit(c, "employee_grant_draft_deleted", "grant=" + str(grant_id))
        c.commit()
        flash("تم حذف مسودة الحافز المفتوحة فقط.", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر حذف الحافز.", "error")
    c.close()
    return redirect(url_for("workforce.employee_grants"))


@workforce_bp.route("/hr/grants/<int:grant_id>/approve", methods=["POST"])
@_role("admin", "accountant", permission="workforce.approve")
def approve_employee_grant(grant_id):
    c=_db()
    try:
        # Lock before reading draft state so concurrent approvals cannot post twice.
        begin_write_transaction(c)
        grant=execute_compat(c, "SELECT * FROM employee_grants WHERE id=?",(grant_id,)).fetchone()
        if not grant:
            raise ValueError("المنحة غير موجودة.")
        if grant["status"] == "approved" or grant["approval_status"] == "approved" or grant["journal_entry_id"]:
            raise ValueError("سبق اعتمادها؛ لا يمكن اعتماد المنحة أو إنشاء قيد لها مرة أخرى.")
        if grant["status"] != "draft" or grant["approval_status"] != "pending":
            raise ValueError("المنحة غير موجودة أو ليست بانتظار الاعتماد.")
        if grant["created_by"] is None or grant["created_by"] == session["user_id"]:
            raise ValueError("لا يجوز اعتماد المنحة بواسطة منشئها، أو دون بيانات منشئ موثقة.")
        lines=execute_compat(c, "SELECT * FROM employee_grant_lines WHERE grant_id=? ORDER BY id",(grant_id,)).fetchall()
        total=M(sum((D(x["amount"]) for x in lines),Decimal("0")))
        if not lines or total<=0 or total!=M(grant["total_amount"]): raise ValueError("كشف المنحة غير مكتمل أو غير متوازن.")
        journal_id=post_journal(c,grant["grant_date"],"EMP-GRANT-"+str(grant_id),
            "إثبات استحقاق منحة: "+grant["grant_name_snapshot"],
            [(int(grant["expense_account_id"]),total,0,"مصروف المنح"),
             (int(grant["payable_account_id"]),0,total,"منح مستحقة للموظفين")],session["user_id"])
        update_cursor = execute_compat(c, """UPDATE employee_grants SET status='approved',journal_entry_id=?,
            approved_by=?,approved_at=CURRENT_TIMESTAMP,approval_status='approved'
            WHERE id=? AND status='draft' AND approval_status='pending'""",
            (journal_id,session["user_id"],grant_id))
        if update_cursor.rowcount != 1:
            raise ValueError("تعذر تثبيت اعتماد المنحة؛ أعد تحميل الشاشة.")
        _audit(c,"employee_grant_approved","grant="+str(grant_id)+" journal="+str(journal_id))
        c.commit(); flash("تم اعتماد المنحة وإثبات المصروف والمنح المستحقة بقيد يومية.","success")
    except _DB_VALUE_KEY_ERRORS as exc:
        c.rollback(); flash(str(exc) or "تعذر اعتماد المنحة.","error")
    c.close()
    return redirect(url_for("workforce.employee_grants"))


@workforce_bp.route("/hr/grants/<int:grant_id>/pay", methods=["POST"])
@_role("admin", "accountant")
def pay_employee_grant(grant_id):
    c=_db()
    try:
        grant=execute_compat(c, "SELECT * FROM employee_grants WHERE id=?",(grant_id,)).fetchone()
        if not grant or grant["status"] not in {"approved","partially_paid"}: raise ValueError("اعتمد المنحة أولًا قبل الصرف.")
        if grant["approved_by"] is not None and grant["approved_by"] == session["user_id"]:
            raise ValueError("يجب أن ينفذ صرف المنحة مستخدم مختلف عن معتمدها.")
        amount=M(request.form.get("amount","0"))
        if amount<=0: raise ValueError("مبلغ الصرف يجب أن يكون أكبر من صفر.")
        cash_id=int(request.form["cash_account_id"])
        cash=execute_compat(c, "SELECT id,account_type,is_group,active FROM accounts WHERE id=?",(cash_id,)).fetchone()
        if not cash or not cash["active"] or cash["is_group"] or cash["account_type"]!="asset":
            raise ValueError("اختر حساب صندوق أو بنك تفصيليًا ونشطًا.")
        payment_date=date.fromisoformat(request.form.get("payment_date") or date.today().isoformat()).isoformat()
        reference="EMP-GRANT-PAY-"+str(grant_id)+"-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
        begin_write_transaction(c)
        grant=execute_compat(c, "SELECT * FROM employee_grants WHERE id=?",(grant_id,)).fetchone()
        if not grant or grant["status"] not in {"approved","partially_paid"}:
            raise ValueError("المنحة لم تعد متاحة للصرف.")
        if grant["approved_by"] is not None and grant["approved_by"] == session["user_id"]:
            raise ValueError("يجب أن ينفذ صرف المنحة مستخدم مختلف عن معتمدها.")
        paid=M(sum((D(x["amount"]) for x in execute_compat(c, "SELECT amount FROM employee_grant_payments WHERE grant_id=?",(grant_id,)).fetchall()),Decimal("0")))
        outstanding=M(D(grant["total_amount"])-paid)
        if amount>outstanding: raise ValueError("مبلغ الصرف أكبر من المتبقي.")
        journal_id=post_journal(c,payment_date,reference,"صرف منحة: "+grant["grant_name_snapshot"],
            [(int(grant["payable_account_id"]),amount,0,"تخفيض المنح المستحقة"),
             (cash_id,0,amount,"صرف المنحة من النقدية/البنك")],session["user_id"])
        execute_compat(c, """INSERT INTO employee_grant_payments(grant_id,payment_date,amount,cash_account_id,
            journal_entry_id,reference,notes,created_by) VALUES(?,?,?,?,?,?,?,?)""",
            (grant_id,payment_date,str(amount),cash_id,journal_id,reference,request.form.get("notes","").strip(),session["user_id"]))
        lines=execute_compat(c, "SELECT id,amount,paid_amount FROM employee_grant_lines WHERE grant_id=? ORDER BY id",(grant_id,)).fetchall()
        outstanding_by_line=[(line,M(D(line["amount"])-D(line["paid_amount"]))) for line in lines]
        total_outstanding=M(sum((due for _line,due in outstanding_by_line),Decimal("0")))
        total_cents=int(amount*100)
        base_cents=int(total_outstanding*100)
        allocations=[]
        allocated_cents=0
        for line,due in outstanding_by_line:
            due_cents=int(due*100)
            share_cents=(total_cents*due_cents)//base_cents if base_cents else 0
            share_cents=min(due_cents,share_cents)
            allocations.append([line,due_cents,share_cents])
            allocated_cents+=share_cents
        remainder_cents=total_cents-allocated_cents
        for item in allocations:
            extra=min(item[1]-item[2],remainder_cents)
            item[2]+=extra
            remainder_cents-=extra
            if remainder_cents<=0: break
        for line,_due_cents,allocated in allocations:
            if allocated>0:
                allocation=Decimal(allocated)/Decimal("100")
                execute_compat(c, "UPDATE employee_grant_lines SET paid_amount=? WHERE id=?",
                    (str(M(D(line["paid_amount"])+allocation)),line["id"]))
        new_status="paid" if amount==outstanding else "partially_paid"
        execute_compat(c, "UPDATE employee_grants SET status=? WHERE id=?",(new_status,grant_id))
        _audit(c,"employee_grant_payment","grant="+str(grant_id)+" amount="+str(amount)+" journal="+str(journal_id))
        c.commit(); flash("تم تسجيل صرف المنحة وترحيل قيد السداد.","success")
    except _DB_VALUE_KEY_ERRORS as exc:
        c.rollback(); flash(str(exc) or "تعذر تسجيل صرف المنحة.","error")
    c.close()
    return redirect(url_for("workforce.employee_grants"))


@workforce_bp.route("/hr/grants/<int:grant_id>/print", methods=["GET"])
@_role("admin", "accountant", "viewer")
def print_employee_grant(grant_id):
    c=_db()
    grant=execute_compat(c, "SELECT * FROM employee_grants WHERE id=?",(grant_id,)).fetchone()
    lines=execute_compat(c, "SELECT * FROM employee_grant_lines WHERE grant_id=? ORDER BY employee_code_snapshot",(grant_id,)).fetchall() if grant else []
    c.close()
    if not grant:
        flash("المنحة غير موجودة.","error")
        return redirect(url_for("workforce.employee_grants"))
    return render_template("employee_grant_print.html",grant=grant,lines=lines,
                           show_basic=grant["calculation_method"]!="fixed")


@workforce_bp.route("/hr/friday-allowance", methods=["GET", "POST"])
@_role("admin", "accountant")
def friday_allowance():
    c = _db()
    if request.method == "POST":
        try:
            period = request.form.get("period", "").strip()
            month_start = date.fromisoformat(period + "-01")
            if period != month_start.strftime("%Y-%m"):
                raise ValueError("حدد شهرًا صحيحًا لبدل الجمعة.")
            next_month = date(month_start.year + (1 if month_start.month == 12 else 0),
                              1 if month_start.month == 12 else month_start.month + 1, 1)
            expense_account_id = int(request.form["expense_account_id"])
            payable_account_id = int(request.form["payable_account_id"])
            expense_account = execute_compat(c, """SELECT id,account_type,is_group,active FROM accounts WHERE id=?""",
                                        (expense_account_id,)).fetchone()
            payable_account = execute_compat(c, """SELECT id,account_type,is_group,active FROM accounts WHERE id=?""",
                                        (payable_account_id,)).fetchone()
            if (not expense_account or not expense_account["active"] or expense_account["is_group"]
                or expense_account["account_type"] != "expense"):
                raise ValueError("اختر حساب مصروف أجور إضافية تفصيليًا ونشطًا.")
            if (not payable_account or not payable_account["active"] or payable_account["is_group"]
                or payable_account["account_type"] != "liability"):
                raise ValueError("اختر حساب أجور إضافية مستحقة تفصيليًا ونشطًا.")
            begin_write_transaction(c)
            existing = execute_compat(c, "SELECT * FROM friday_allowance_batches WHERE period=?", (period,)).fetchone()
            if existing and existing["status"] == "posted":
                raise ValueError("تم ترحيل بدل الجمعة لهذا الشهر بالفعل ولا يمكن إعادة تكوينه.")
            if existing and existing["approved_by"] is not None:
                raise ValueError("لا يمكن تعديل كشف بدل الجمعة بعد اعتماده؛ أنشئ مراجعة جديدة وفق الإجراء المعتمد.")
            if existing:
                batch_id = existing["id"]
                execute_compat(c, "DELETE FROM friday_allowance_lines WHERE batch_id=?", (batch_id,))
                execute_compat(c, """UPDATE friday_allowance_batches SET expense_account_id=?,payable_account_id=?,
                    total_amount='0' WHERE id=? AND status='draft'""",
                    (expense_account_id,payable_account_id,batch_id))
            else:
                batch_id = insert_and_get_id(c, """INSERT INTO friday_allowance_batches(period,expense_account_id,payable_account_id,
                    total_amount,created_by) VALUES(?,?,?,'0',?)""",
                    (period,expense_account_id,payable_account_id,session["user_id"]))
            selected_employee_ids = sorted({int(value) for value in request.form.getlist("employee_ids")})
            if not selected_employee_ids:
                raise ValueError("أضف موظفًا واحدًا على الأقل إلى جدول بدل الجمعة.")
            selected_marks = ",".join("?" for _ in selected_employee_ids)
            eligible = execute_compat(c, f"""SELECT e.id,e.employee_code,e.full_name,e.basic_salary,
                    e.branch_id,e.cost_center_id,e.project_id,COUNT(a.id) AS attendance_days
                FROM employees e JOIN attendance_records a ON a.employee_id=e.id
                WHERE e.active=1 AND e.friday_work_eligible=1
                  AND e.id IN ({selected_marks})
                  AND a.status='present' AND {_friday_attendance_predicate(c)}
                  AND a.attendance_date>=? AND a.attendance_date<?
                  AND e.hire_date<=a.attendance_date
                  AND (e.work_status NOT IN ('resigned','retired')
                       OR e.termination_date>=a.attendance_date)
                GROUP BY e.id ORDER BY e.employee_code""",
                (*selected_employee_ids,month_start.isoformat(),next_month.isoformat())).fetchall()
            if len(eligible) != len(selected_employee_ids):
                raise ValueError("يوجد موظف مضاف غير مؤهل أو لا يملك حضور جمعة مسجلًا في الشهر المحدد. راجع الاختيار.")
            if not eligible:
                raise ValueError("لا يوجد موظفون مستحقون لديهم حضور مسجل في أيام الجمعة خلال هذا الشهر.")
            total = Decimal("0")
            period_as_of = (next_month - date.resolution).isoformat()
            for employee in eligible:
                basic = _employee_salary_basis(c, employee["id"], period_as_of)[0]
                if basic <= 0:
                    raise ValueError("لا يوجد راتب أساسي معتمد للموظف " + employee["full_name"] +
                                     ". رحّل أول كشف مرتبات أو راجع بيانات الأجر قبل احتساب بدل الجمعة.")
                raw_days = request.form.get("attendance_days_" + str(employee["id"]), str(employee["attendance_days"]))
                try:
                    days = int(raw_days)
                except (TypeError, ValueError):
                    raise ValueError("عدد أيام الجمعة يجب أن يكون عددًا صحيحًا لكل موظف.")
                if days < 1 or days > int(employee["attendance_days"]):
                    raise ValueError("عدد أيام بدل الجمعة للموظف " + employee["full_name"] +
                                      " يجب أن يكون من 1 إلى " + str(employee["attendance_days"]) + ".")
                amount = M(basic / Decimal("30") * Decimal("2") * Decimal(days))
                if amount <= 0:
                    continue
                total += amount
                execute_compat(c, """INSERT INTO friday_allowance_lines(batch_id,employee_id,employee_code_snapshot,
                    employee_name_snapshot,basic_salary_snapshot,attendance_days,amount,branch_id,cost_center_id,project_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id,employee["id"],employee["employee_code"],employee["full_name"],str(basic),
                     days,str(amount),employee["branch_id"],employee["cost_center_id"],employee["project_id"]))
            total = M(total)
            if total <= 0:
                raise ValueError("إجمالي بدل الجمعة يساوي صفرًا؛ راجع الرواتب وأيام الحضور.")
            execute_compat(c, "UPDATE friday_allowance_batches SET total_amount=? WHERE id=?",
                      (str(total),batch_id))
            _audit(c,"friday_allowance_prepared",period+" batch="+str(batch_id)+" total="+str(total))
            c.commit()
            flash("تم إعداد كشف بدل الجمعة للمراجعة. لم يتم ترحيل أي قيد حتى الآن.","success")
        except _DB_VALUE_KEY_ERRORS as exc:
            c.rollback()
            flash(str(exc) or "تعذر إعداد كشف بدل الجمعة.","error")
        c.close()
        return redirect(url_for("workforce.friday_allowance",period=request.form.get("period","")))
    selected_period = request.args.get("period",date.today().strftime("%Y-%m"))
    try:
        selected_start = date.fromisoformat(selected_period+"-01")
        if selected_start.strftime("%Y-%m") != selected_period:
            raise ValueError()
    except ValueError:
        selected_start = date.today().replace(day=1)
        selected_period = selected_start.strftime("%Y-%m")
    next_month = date(selected_start.year + (1 if selected_start.month == 12 else 0),
                       1 if selected_start.month == 12 else selected_start.month + 1, 1)
    eligible_rows = execute_compat(c, f"""SELECT e.id,e.employee_code,e.full_name,e.basic_salary,e.work_status,e.department,
            COUNT(a.id) AS attendance_days
        FROM employees e JOIN attendance_records a ON a.employee_id=e.id
        WHERE e.active=1 AND e.friday_work_eligible=1
          AND a.status='present' AND {_friday_attendance_predicate(c)}
          AND a.attendance_date>=? AND a.attendance_date<?
          AND e.hire_date<=a.attendance_date
          AND (e.work_status NOT IN ('resigned','retired') OR e.termination_date>=a.attendance_date)
        GROUP BY e.id ORDER BY e.employee_code""",
        (selected_start.isoformat(),next_month.isoformat())).fetchall()
    period_as_of = (next_month - date.resolution).isoformat()
    eligible_employees = []
    for eligible_row in eligible_rows:
        item = dict(eligible_row)
        item["basic_salary"] = str(_employee_salary_basis(c, item["id"], period_as_of)[0])
        eligible_employees.append(item)
    batches = execute_compat(c, """SELECT b.*,ea.code AS expense_code,ea.name_ar AS expense_name,
        pa.code AS payable_code,pa.name_ar AS payable_name
        FROM friday_allowance_batches b JOIN accounts ea ON ea.id=b.expense_account_id
        JOIN accounts pa ON pa.id=b.payable_account_id ORDER BY b.period DESC LIMIT 24""").fetchall()
    batch_lines = {}
    for batch in batches:
        batch_lines[batch["id"]] = execute_compat(c, """SELECT * FROM friday_allowance_lines
            WHERE batch_id=? ORDER BY employee_code_snapshot""",(batch["id"],)).fetchall()
    expense_accounts = execute_compat(c, """SELECT id,code,name_ar FROM accounts
        WHERE active=1 AND is_group=0 AND account_type='expense' ORDER BY code""").fetchall()
    payable_accounts = execute_compat(c, """SELECT id,code,name_ar FROM accounts
        WHERE active=1 AND is_group=0 AND account_type='liability' ORDER BY code""").fetchall()
    c.close()
    return render_template("friday_allowance.html",batches=batches,batch_lines=batch_lines,
        expense_accounts=expense_accounts,payable_accounts=payable_accounts,selected_period=selected_period,
        employees=eligible_employees)


@workforce_bp.route("/hr/friday-allowance/<int:batch_id>/approve", methods=["POST"])
@_role("admin", "accountant", permission="workforce.approve")
def approve_friday_allowance(batch_id):
    c = _db()
    try:
        begin_write_transaction(c)
        batch = execute_compat(c, 
            "SELECT * FROM friday_allowance_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch or batch["status"] != "draft" or batch["journal_entry_id"]:
            raise ValueError("لا يمكن اعتماد إلا مسودة بدل جمعة غير مرحلة.")
        if batch["approval_status"] != "pending" or batch["approved_by"] is not None:
            raise ValueError("كشف بدل الجمعة ليس بانتظار الاعتماد.")
        if batch["created_by"] is None or batch["created_by"] == session["user_id"]:
            raise ValueError("لا يجوز اعتماد كشف بدل الجمعة بواسطة منشئه، أو دون بيانات منشئ موثقة.")
        lines = execute_compat(c, 
            "SELECT * FROM friday_allowance_lines WHERE batch_id=? ORDER BY employee_code_snapshot",
            (batch_id,),
        ).fetchall()
        total = M(sum((D(line["amount"]) for line in lines), Decimal("0")))
        if not lines or total <= 0 or total != M(batch["total_amount"]):
            raise ValueError("تفاصيل بدل الجمعة لا تطابق إجمالي الكشف؛ لا يمكن اعتماده.")
        for account_id, expected, label in (
            (batch["expense_account_id"], "expense", "مصروف الأجور الإضافية"),
            (batch["payable_account_id"], "liability", "الأجور الإضافية المستحقة"),
        ):
            account = execute_compat(c, 
                "SELECT account_type,is_group,active FROM accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if not account or not account["active"] or account["is_group"] or account["account_type"] != expected:
                raise ValueError("حساب " + label + " غير صالح.")
        reason = (request.form.get("reason") or "").strip()[:500]
        changed = execute_compat(c, 
            """UPDATE friday_allowance_batches
               SET approved_by=?,approved_at=CURRENT_TIMESTAMP,approval_reason=?,approval_status='approved'
               WHERE id=? AND status='draft' AND approval_status='pending'
                 AND approved_by IS NULL AND journal_entry_id IS NULL""",
            (session["user_id"], reason, batch_id),
        ).rowcount
        if changed != 1:
            raise ValueError("تم اعتماد الكشف بواسطة مستخدم آخر؛ أعد تحميل الشاشة.")
        _audit(c, "friday_allowance_approved",
               "batch=" + str(batch_id) + " period=" + batch["period"] + " reason=" + reason)
        c.commit()
        flash("تم اعتماد كشف بدل الجمعة. أصبح جاهزًا للترحيل.", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر اعتماد كشف بدل الجمعة.", "error")
    finally:
        c.close()
    return redirect(url_for("workforce.friday_allowance"))


@workforce_bp.route("/hr/friday-allowance/<int:batch_id>/delete", methods=["POST"])
@_role("admin", "accountant")
def delete_friday_allowance(batch_id):
    c = _db()
    try:
        begin_write_transaction(c)
        batch = execute_compat(c, "SELECT * FROM friday_allowance_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] != "draft" or batch["journal_entry_id"] or batch["approved_by"] is not None:
            raise ValueError("لا يمكن حذف بدل الجمعة بعد اعتماده أو ترحيله. الحذف متاح للمسودة غير المعتمدة فقط.")
        execute_compat(c, "DELETE FROM friday_allowance_lines WHERE batch_id=?", (batch_id,))
        delete_cursor = execute_compat(c, "DELETE FROM friday_allowance_batches WHERE id=? AND status='draft' AND journal_entry_id IS NULL", (batch_id,))
        if delete_cursor.rowcount != 1:
            raise ValueError("لم يتم حذف الكشف؛ ربما تم إغلاقه في عملية أخرى.")
        _audit(c, "friday_allowance_draft_deleted", "batch=" + str(batch_id))
        c.commit()
        flash("تم حذف مسودة بدل الجمعة المفتوحة فقط.", "success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر حذف كشف بدل الجمعة.", "error")
    c.close()
    return redirect(url_for("workforce.friday_allowance"))


@workforce_bp.route("/hr/friday-allowance/<int:batch_id>/post", methods=["POST"])
@_role("admin", "accountant")
def post_friday_allowance(batch_id):
    c = _db()
    try:
        begin_write_transaction(c)
        batch = execute_compat(c, "SELECT * FROM friday_allowance_batches WHERE id=?",(batch_id,)).fetchone()
        if not batch or batch["status"] != "draft":
            raise ValueError("كشف بدل الجمعة غير موجود أو سبق ترحيله.")
        if batch["approved_by"] is None or batch["approval_status"] != "approved":
            raise ValueError("يجب اعتماد كشف بدل الجمعة بواسطة مستخدم آخر قبل الترحيل.")
        if batch["created_by"] is None or batch["created_by"] == session["user_id"]:
            raise ValueError("لا يجوز ترحيل كشف بدل الجمعة بواسطة منشئه، أو دون بيانات منشئ موثقة.")
        lines = execute_compat(c, """SELECT * FROM friday_allowance_lines WHERE batch_id=?
            ORDER BY employee_code_snapshot""",(batch_id,)).fetchall()
        total = M(sum((D(line["amount"]) for line in lines),Decimal("0")))
        if not lines or total <= 0 or total != M(batch["total_amount"]):
            raise ValueError("تفاصيل بدل الجمعة لا تطابق إجمالي الكشف. أعد تكوين الكشف قبل الترحيل.")
        expense_account = execute_compat(c, "SELECT id,account_type,is_group,active FROM accounts WHERE id=?",
                                     (batch["expense_account_id"],)).fetchone()
        payable_account = execute_compat(c, "SELECT id,account_type,is_group,active FROM accounts WHERE id=?",
                                    (batch["payable_account_id"],)).fetchone()
        if (not expense_account or not expense_account["active"] or expense_account["is_group"]
            or expense_account["account_type"]!="expense"):
            raise ValueError("حساب مصروف الأجور الإضافية غير صالح.")
        if (not payable_account or not payable_account["active"] or payable_account["is_group"]
            or payable_account["account_type"]!="liability"):
            raise ValueError("حساب الأجور الإضافية المستحقة غير صالح.")
        period_start = date.fromisoformat(batch["period"]+"-01")
        next_month = date(period_start.year+(1 if period_start.month==12 else 0),
                          1 if period_start.month==12 else period_start.month+1,1)
        accrual_date = (next_month-date.resolution).isoformat()
        journal_lines = []
        for line in lines:
            dims = {key:line[key] for key in ("branch_id","cost_center_id","project_id")}
            journal_lines.append((batch["expense_account_id"],M(line["amount"]),0,
                "بدل عمل أيام الجمعة - "+line["employee_code_snapshot"]+" "+line["employee_name_snapshot"],dims))
        journal_lines.append((batch["payable_account_id"],0,total,"إجمالي بدل العمل أيام الجمعة المستحق"))
        reference = "FRIDAY-ALLOWANCE-"+batch["period"]
        journal_id = post_journal(c,accrual_date,reference,
            "استحقاق بدل العمل أيام الجمعة عن "+batch["period"],journal_lines,session["user_id"])
        execute_compat(c, "UPDATE friday_allowance_batches SET status='posted',journal_entry_id=? WHERE id=? AND status='draft'",
                  (journal_id,batch_id))
        _audit(c,"friday_allowance_posted",batch["period"]+" journal="+str(journal_id)+" total="+str(total))
        currency_row = execute_compat(c, 
            "SELECT setting_value FROM company_settings WHERE setting_key='base_currency'"
        ).fetchone()
        create_cash_commitment(
            c, "friday_overtime", "بدل الجمعة - " + batch["period"], str(total),
            accrual_date, period_label=batch["period"],
            currency_code=currency_row["setting_value"] if currency_row else "",
            payable_account_id=batch["payable_account_id"],
            origin_type="friday_allowance_batch", origin_id=batch_id,
            created_by=session["user_id"],
        )
        c.commit()
        flash("تم ترحيل بدل الجمعة وإنشاء قيد الاستحقاق وإضافته تلقائيًا إلى سجل الالتزامات النقدية.","success")
    except _DB_VALUE_KEY_ERRORS as exc:
        c.rollback()
        flash(str(exc) or "تعذر ترحيل بدل الجمعة.","error")
    c.close()
    return redirect(url_for("workforce.friday_allowance"))


@workforce_bp.route("/hr/attendance", methods=["GET", "POST"])
@_role("admin", "accountant")
def attendance():
    c = _db()
    selected_date = request.values.get("attendance_date", date.today().isoformat())
    try:
        date.fromisoformat(selected_date)
    except ValueError:
        selected_date = date.today().isoformat()
    if request.method == "POST" and request.form.get("save_month") == "1":
        try:
            selected_month = request.form.get("month", date.today().strftime("%Y-%m"))
            month_start = date.fromisoformat(selected_month + "-01")
            next_month = date(month_start.year + (1 if month_start.month == 12 else 0),
                              1 if month_start.month == 12 else month_start.month + 1, 1)
            days_count = (next_month-month_start).days
            allowed = {"present","absent","permission","holiday","annual_leave","sick_leave","unpaid_leave",""}
            employee_ids = [int(x["id"]) for x in execute_compat(c, "SELECT id FROM employees WHERE active=1").fetchall()]
            begin_write_transaction(c)
            for employee_id in employee_ids:
                for day_number in range(1,days_count+1):
                    status = request.form.get(f"status_{employee_id}_{day_number}","").strip()
                    if status not in allowed:
                        raise ValueError("حالة حضور غير صحيحة.")
                    day_date = date(month_start.year,month_start.month,day_number).isoformat()
                    if not status:
                        execute_compat(c, "DELETE FROM attendance_records WHERE employee_id=? AND attendance_date=?",
                                  (employee_id,day_date))
                        continue
                    check_in = request.form.get(f"check_in_{employee_id}_{day_number}","").strip()
                    check_out = request.form.get(f"check_out_{employee_id}_{day_number}","").strip()
                    for tm in (check_in,check_out):
                        if tm: datetime.strptime(tm,"%H:%M")
                    if check_in and check_out and check_out<=check_in:
                        raise ValueError("وقت الانصراف يجب أن يكون بعد وقت الحضور.")
                    absence = D(request.form.get(f"absence_{employee_id}_{day_number}","0"))
                    overtime = D(request.form.get(f"overtime_{employee_id}_{day_number}","0"))
                    if min(absence,overtime)<0 or absence>24 or overtime>24:
                        raise ValueError("ساعات الغياب أو الإضافي خارج النطاق.")
                    if status=="absent" and absence<=0: absence=Decimal("8")
                    if status!="absent": absence=Decimal("0")
                    execute_compat(c, """INSERT INTO attendance_records(employee_id,attendance_date,status,check_in,check_out,
                        absence_hours,approved_overtime_hours,notes,recorded_by) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(employee_id,attendance_date) DO UPDATE SET status=excluded.status,
                        check_in=excluded.check_in,check_out=excluded.check_out,absence_hours=excluded.absence_hours,
                        approved_overtime_hours=excluded.approved_overtime_hours,notes=excluded.notes,
                        recorded_by=excluded.recorded_by""",
                        (employee_id,day_date,status,check_in,check_out,str(M(absence)),str(M(overtime)),
                         request.form.get(f"notes_{employee_id}_{day_number}","").strip(),session["user_id"]))
            _audit(c,"attendance_month_saved",selected_month)
            c.commit()
            flash("تم حفظ مصفوفة الحضور للشهر المحدد.", "success")
        except _DB_VALUE_KEY_ERRORS as exc:
            c.rollback()
            flash(str(exc) or "تعذر حفظ مصفوفة الحضور.", "error")
        c.close()
        return redirect(url_for("workforce.attendance",month=request.form.get("month",date.today().strftime("%Y-%m"))))
    if request.method == "POST":
        try:
            employee_id = int(request.form["employee_id"])
            status = request.form.get("status","present")
            if status not in {"present","absent","permission","holiday","annual_leave","sick_leave","unpaid_leave"}:
                raise ValueError("حالة الحضور غير صحيحة.")
            check_in = request.form.get("check_in","").strip()
            check_out = request.form.get("check_out","").strip()
            absence_hours = D(request.form.get("absence_hours","0"))
            overtime_hours = D(request.form.get("approved_overtime_hours","0"))
            if min(absence_hours,overtime_hours)<0 or absence_hours>24 or overtime_hours>24:
                raise ValueError("ساعات الغياب أو الإضافي خارج النطاق المسموح.")
            for tm in (check_in,check_out):
                if tm:
                    datetime.strptime(tm,"%H:%M")
            if check_in and check_out and check_out <= check_in:
                raise ValueError("وقت الانصراف يجب أن يكون بعد وقت الحضور في نفس اليوم.")
            if status=="absent" and absence_hours<=0:
                raise ValueError("حدد ساعات الغياب عند تسجيل يوم غياب.")
            if status!="absent" and absence_hours>0:
                raise ValueError("ساعات الغياب تسجل فقط مع حالة غياب.")
            if not execute_compat(c, "SELECT 1 FROM employees WHERE id=? AND active=1",(employee_id,)).fetchone():
                raise ValueError("الموظف غير موجود أو غير نشط.")
            execute_compat(c, """INSERT INTO attendance_records(employee_id,attendance_date,status,check_in,check_out,
                absence_hours,approved_overtime_hours,notes,recorded_by)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(employee_id,attendance_date) DO UPDATE SET status=excluded.status,
                check_in=excluded.check_in,check_out=excluded.check_out,absence_hours=excluded.absence_hours,
                approved_overtime_hours=excluded.approved_overtime_hours,notes=excluded.notes,
                recorded_by=excluded.recorded_by""",
                (employee_id,selected_date,status,check_in,check_out,str(M(absence_hours)),
                 str(M(overtime_hours)),request.form.get("notes","").strip(),session["user_id"]))
            _audit(c,"attendance_saved",str(employee_id)+" "+selected_date+" "+status)
            c.commit()
            flash("تم حفظ سجل الحضور والانصراف.","success")
        except _DB_VALUE_KEY_ERRORS as exc:
            c.rollback()
            flash(str(exc) or "تعذر حفظ سجل الحضور.","error")
        c.close()
        return redirect(url_for("workforce.attendance",attendance_date=selected_date))
    selected_month = request.values.get("month", selected_date[:7])
    try:
        month_start = date.fromisoformat(selected_month + "-01")
    except ValueError:
        month_start = date.today().replace(day=1)
        selected_month = month_start.strftime("%Y-%m")
    next_month = date(month_start.year + (1 if month_start.month == 12 else 0),
                      1 if month_start.month == 12 else month_start.month + 1, 1)
    days_count = (next_month-month_start).days
    employees = execute_compat(c, "SELECT id,employee_code,full_name FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    month_records = execute_compat(c, "SELECT * FROM attendance_records WHERE attendance_date>=? AND attendance_date<? ORDER BY employee_id,attendance_date",
        (month_start.isoformat(),next_month.isoformat())).fetchall()
    by_employee = {}
    for record in month_records:
        by_employee.setdefault(record["employee_id"], {})[int(record["attendance_date"][-2:])] = dict(record)
    grid = [{"employee":e,"days":[by_employee.get(e["id"],{}).get(day,{}) for day in range(1,days_count+1)]}
            for e in employees]
    friday_days = [day for day in range(1,days_count+1)
                   if date(month_start.year,month_start.month,day).weekday()==4]
    c.close()
    return render_template("attendance.html",employees=employees,records=[],selected_date=selected_date,
        selected_month=selected_month,days=range(1,days_count+1),friday_days=friday_days,grid=grid,
        status_labels={"present":"حاضر","absent":"غائب","permission":"إذن","holiday":"عطلة",
        "annual_leave":"إجازة سنوية","sick_leave":"إجازة مرضية","unpaid_leave":"إجازة بدون مرتب"})


@workforce_bp.route("/hr/attendance/report", methods=["GET"])
@_role("admin", "accountant", "viewer")
def attendance_report():
    c = _db()
    today = date.today()
    month_start = today.replace(day=1)
    default_from = request.args.get("date_from", month_start.isoformat())
    default_to = request.args.get("date_to", today.isoformat())
    employee_id = request.args.get("employee_id", type=int)
    try:
        start_date = date.fromisoformat(default_from)
        end_date = date.fromisoformat(default_to)
        if end_date < start_date:
            raise ValueError("تاريخ النهاية يجب أن يكون مساويًا أو بعد تاريخ البداية.")
    except ValueError as exc:
        c.close()
        flash(str(exc) or "حدد فترة صحيحة لتقرير الغياب.", "error")
        return redirect(url_for("workforce.attendance"))
    employees = execute_compat(c, 
        "SELECT id,employee_code,full_name FROM employees ORDER BY employee_code"
    ).fetchall()
    where_employee = " AND e.id=?" if employee_id else ""
    params = [start_date.isoformat(), end_date.isoformat()]
    if employee_id:
        params.append(employee_id)
    rows = execute_compat(c, f"""SELECT e.id AS employee_id,e.employee_code,e.full_name,e.department,
            COUNT(a.id) AS recorded_days,
            SUM(CASE WHEN a.status='absent' THEN 1 ELSE 0 END) AS absent_days,
            {decimal_sum_expression(c, "CASE WHEN a.status='absent' THEN a.absence_hours ELSE '0' END")} AS absent_hours,
            SUM(CASE WHEN a.status='unpaid_leave' THEN 1 ELSE 0 END) AS unpaid_leave_days,
            SUM(CASE WHEN a.status='permission' THEN 1 ELSE 0 END) AS permission_days
        FROM employees e
        LEFT JOIN attendance_records a ON a.employee_id=e.id
            AND a.attendance_date>=? AND a.attendance_date<=?
        WHERE 1=1 """ + where_employee + """
        GROUP BY e.id,e.employee_code,e.full_name,e.department
        ORDER BY absent_days DESC,e.employee_code""", params).fetchall()
    totals = {
        "employees": len(rows),
        "absent_days": sum(int(row["absent_days"] or 0) for row in rows),
        "absent_hours": M(sum((D(row["absent_hours"] or 0) for row in rows), Decimal("0"))),
        "unpaid_leave_days": sum(int(row["unpaid_leave_days"] or 0) for row in rows),
        "permission_days": sum(int(row["permission_days"] or 0) for row in rows),
    }
    c.close()
    return render_template("attendance_report.html", rows=rows, employees=employees,
        selected_employee=employee_id, date_from=start_date.isoformat(),
        date_to=end_date.isoformat(), totals=totals)


@workforce_bp.route("/hr/leaves", methods=["GET", "POST"])
@_role("admin", "accountant")
def leaves():
    c = _db()
    if request.method == "POST":
        try:
            employee_id=int(request.form["employee_id"]); leave_type_id=int(request.form["leave_type_id"])
            start=date.fromisoformat(request.form["start_date"]); end=date.fromisoformat(request.form["end_date"])
            days=D(request.form.get("days","0"))
            leave_type=execute_compat(c, "SELECT * FROM leave_types WHERE id=? AND active=1",(leave_type_id,)).fetchone()
            if end<start or days<=0 or not leave_type:
                raise ValueError("راجع نوع الإجازة والتواريخ وعدد الأيام.")
            if not execute_compat(c, "SELECT 1 FROM employees WHERE id=? AND active=1",(employee_id,)).fetchone():
                raise ValueError("الموظف غير موجود أو غير نشط.")
            execute_compat(c, """INSERT INTO employee_leave_requests(employee_id,leave_type_id,start_date,end_date,days,
                reason,requested_by) VALUES(?,?,?,?,?,?,?)""",
                (employee_id,leave_type_id,start.isoformat(),end.isoformat(),str(M(days)),
                 request.form.get("reason","").strip(),session["user_id"]))
            _audit(c,"leave_requested",str(employee_id)+" "+start.isoformat()+".."+end.isoformat())
            c.commit(); flash("تم تسجيل طلب الإجازة في انتظار الاعتماد.","success")
        except _DB_VALUE_KEY_ERRORS as exc:
            c.rollback(); flash(str(exc) or "تعذر تسجيل طلب الإجازة.","error")
        c.close(); return redirect(url_for("workforce.leaves"))
    employees=execute_compat(c, "SELECT id,employee_code,full_name FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    types=execute_compat(c, "SELECT * FROM leave_types WHERE active=1 ORDER BY id").fetchall()
    balances=execute_compat(c, """SELECT b.*,e.employee_code,e.full_name,t.name leave_name
        FROM employee_leave_balances b JOIN employees e ON e.id=b.employee_id
        JOIN leave_types t ON t.id=b.leave_type_id ORDER BY e.employee_code,t.id""").fetchall()
    requests=execute_compat(c, """SELECT r.*,e.employee_code,e.full_name,t.name leave_name,t.is_paid,t.requires_balance
        FROM employee_leave_requests r JOIN employees e ON e.id=r.employee_id
        JOIN leave_types t ON t.id=r.leave_type_id ORDER BY r.id DESC""").fetchall()
    c.close()
    return render_template("leaves.html",employees=employees,types=types,balances=balances,requests=requests)


@workforce_bp.route("/hr/leaves/balance", methods=["POST"])
@_role("admin", "accountant")
def set_leave_balance():
    c=_db()
    try:
        employee_id=int(request.form["employee_id"]); leave_type_id=int(request.form["leave_type_id"])
        days=M(request.form.get("balance_days","0"))
        if days<0 or not execute_compat(c, "SELECT 1 FROM employees WHERE id=?",(employee_id,)).fetchone() or not execute_compat(c, "SELECT 1 FROM leave_types WHERE id=? AND requires_balance=1",(leave_type_id,)).fetchone():
            raise ValueError("رصيد الإجازة غير صحيح.")
        execute_compat(c, """INSERT INTO employee_leave_balances(employee_id,leave_type_id,balance_days)
            VALUES(?,?,?) ON CONFLICT(employee_id,leave_type_id) DO UPDATE SET
            balance_days=excluded.balance_days,updated_at=CURRENT_TIMESTAMP""",(employee_id,leave_type_id,str(days)))
        _audit(c,"leave_balance_adjusted",str(employee_id)+" type="+str(leave_type_id)+" balance="+str(days))
        c.commit(); flash("تم تحديث رصيد الإجازة بعد المراجعة.","success")
    except _DB_VALUE_KEY_ERRORS as exc:
        c.rollback(); flash(str(exc) or "تعذر تحديث رصيد الإجازة.","error")
    c.close(); return redirect(url_for("workforce.leaves"))


@workforce_bp.route("/hr/leaves/<int:request_id>/decision", methods=["POST"])
@_role("admin", "accountant")
def decide_leave(request_id):
    c=_db()
    try:
        begin_write_transaction(c)
        leave=execute_compat(c, """SELECT r.*,t.is_paid,t.requires_balance,t.name leave_name
            FROM employee_leave_requests r JOIN leave_types t ON t.id=r.leave_type_id WHERE r.id=?""",
            (request_id,)).fetchone()
        decision=request.form.get("decision")
        if not leave or leave["status"]!="requested" or decision not in {"approved","rejected"}:
            raise ValueError("طلب الإجازة غير موجود أو سبق اتخاذ قرار.")
        if decision=="approved" and leave["is_paid"] and leave["requires_balance"]:
            balance=execute_compat(c, "SELECT balance_days FROM employee_leave_balances WHERE employee_id=? AND leave_type_id=?",
                (leave["employee_id"],leave["leave_type_id"])).fetchone()
            if not balance or D(balance["balance_days"])<D(leave["days"]):
                raise ValueError("رصيد الإجازة السنوية غير كافٍ.")
            new_balance=M(D(balance["balance_days"])-D(leave["days"]))
            execute_compat(c, "UPDATE employee_leave_balances SET balance_days=?,updated_at=CURRENT_TIMESTAMP WHERE employee_id=? AND leave_type_id=?",
                (str(new_balance),leave["employee_id"],leave["leave_type_id"]))
        execute_compat(c, "UPDATE employee_leave_requests SET status=?,decided_by=?,decided_at=CURRENT_TIMESTAMP WHERE id=?",
            (decision,session["user_id"],request_id))
        _audit(c,"leave_"+decision,str(request_id)+" "+leave["leave_name"])
        c.commit(); flash("تم اعتماد الإجازة." if decision=="approved" else "تم رفض الإجازة.","success")
    except _DB_VALUE_KEY_ERRORS as exc:
        c.rollback(); flash(str(exc) or "تعذر اتخاذ قرار الإجازة.","error")
    c.close(); return redirect(url_for("workforce.leaves"))


@workforce_bp.route("/hr/custody", methods=["GET", "POST"])
@_role("admin", "accountant")
def custody():
    c = _db()
    if request.method == "POST":
        try:
            amount = M(request.form.get("amount"))
            if amount <= 0:
                raise ValueError("قيمة العهدة يجب أن تكون أكبر من صفر.")
            employee_id = int(request.form["employee_id"])
            cash_id = int(request.form["cash_account_id"])
            custody_id = int(request.form["custody_account_id"])
            no = "CUST-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
            begin_write_transaction(c)
            journal_id = post_journal(c, request.form.get("issue_date") or date.today().isoformat(), no,
                "صرف عهدة موظف", [(custody_id, amount, 0, "عهدة موظف"),
                                  (cash_id, 0, amount, "صرف العهدة من الصندوق/البنك")], session["user_id"])
            execute_compat(c, """INSERT INTO employee_custodies(custody_no,employee_id,issue_date,amount,remaining_amount,
                description,custody_account_id,cash_account_id,journal_entry_id,created_by)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (no,employee_id,request.form.get("issue_date") or date.today().isoformat(),str(amount),str(amount),
                 request.form.get("description","").strip(),custody_id,cash_id,journal_id,session["user_id"]))
            _audit(c, "employee_custody_issued", no + " amount=" + str(amount))
            c.commit()
            flash("تم صرف العهدة وترحيل قيدها.", "success")
        except _DB_VALUE_KEY_ERRORS as e:
            c.rollback()
            flash(str(e), "error")
        c.close()
        return redirect(url_for("workforce.custody"))
    custody_page = fetch_page(c,
        """SELECT x.*,e.employee_code,e.full_name FROM employee_custodies x
           JOIN employees e ON e.id=x.employee_id ORDER BY x.id DESC""",
        "SELECT COUNT(*) FROM employee_custodies",
        page=request.args.get("custody_page",1,type=int),
        per_page=request.args.get("custody_per_page",50,type=int))
    rows = custody_page["rows"]
    employees = execute_compat(c, "SELECT id,employee_code,full_name FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    accounts = _accounts(c)
    c.close()
    return render_template("custody.html", rows=rows, custody_page=custody_page, employees=employees, accounts=accounts, today=date.today().isoformat())


@workforce_bp.route("/hr/custody/<int:custody_id>/expense", methods=["POST"])
@_role("admin", "accountant")
def custody_expense(custody_id):
    c = _db()
    try:
        row = execute_compat(c, "SELECT * FROM employee_custodies WHERE id=? AND status='open'", (custody_id,)).fetchone()
        if not row:
            raise ValueError("العهدة غير موجودة أو مغلقة.")
        amount = M(request.form.get("amount"))
        used = sum((D(x["amount"]) for x in execute_compat(c, "SELECT amount FROM custody_expenses WHERE custody_id=?", (custody_id,)).fetchall()), Decimal("0"))
        if amount <= 0 or used + amount > D(row["amount"]):
            raise ValueError("قيمة المصروف تتجاوز المتاح في العهدة.")
        execute_compat(c, "INSERT INTO custody_expenses(custody_id,expense_date,expense_account_id,amount,description,receipt_reference,created_by) VALUES(?,?,?,?,?,?,?)",
            (custody_id,request.form.get("expense_date") or date.today().isoformat(),int(request.form["expense_account_id"]),
             str(amount),request.form.get("description","").strip(),request.form.get("receipt_reference","").strip(),session["user_id"]))
        _audit(c, "employee_custody_expense_added", str(custody_id) + " amount=" + str(amount))
        c.commit()
        flash("تم تسجيل مصروف العهدة بانتظار التسوية والاعتماد.", "success")
    except _DB_VALUE_KEY_ERRORS as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("workforce.custody"))


@workforce_bp.route("/hr/custody/<int:custody_id>/settle", methods=["POST"])
@_role("admin", "accountant")
def settle_custody(custody_id):
    c = _db()
    try:
        row = execute_compat(c, "SELECT * FROM employee_custodies WHERE id=? AND status='open'", (custody_id,)).fetchone()
        if not row:
            raise ValueError("العهدة غير موجودة أو تمت تسويتها.")
        expenses = execute_compat(c, "SELECT * FROM custody_expenses WHERE custody_id=? ORDER BY id", (custody_id,)).fetchall()
        expense_total = sum((D(x["amount"]) for x in expenses), Decimal("0"))
        returned = M(request.form.get("returned_amount"))
        total = M(row["amount"])
        if returned < 0 or expense_total + returned != total:
            raise ValueError("لا بد أن يساوي مجموع المصروفات والمبلغ المرتد قيمة العهدة بالكامل.")
        lines = [(int(x["expense_account_id"]), M(x["amount"]), 0, x["description"] or "مصروف عهدة") for x in expenses]
        if returned:
            lines.append((int(row["cash_account_id"]), returned, 0, "رد المتبقي من العهدة"))
        lines.append((int(row["custody_account_id"]), 0, total, "تسوية عهدة الموظف"))
        begin_write_transaction(c)
        journal_id = post_journal(c, request.form.get("settlement_date") or date.today().isoformat(),
            "CUST-SETTLE-" + row["custody_no"], "تسوية عهدة " + row["custody_no"], lines, session["user_id"])
        execute_compat(c, "UPDATE employee_custodies SET remaining_amount='0',status='settled' WHERE id=?", (custody_id,))
        _audit(c, "employee_custody_settled", row["custody_no"] + " journal=" + str(journal_id))
        c.commit()
        flash("تمت تسوية العهدة بالكامل وترحيل القيد.", "success")
    except _DB_VALUE_KEY_ERRORS as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("workforce.custody"))


def _service_duration_30_day(service_start, termination_date):
    """Derive service months/days from dates using the existing 30-day company-policy basis."""
    elapsed_days = (termination_date - service_start).days
    if elapsed_days < 0:
        raise ValueError("تاريخ انتهاء الخدمة لا يجوز أن يسبق تاريخ التعيين.")
    return divmod(elapsed_days, 30)


@workforce_bp.route("/hr/end-of-service", methods=["GET", "POST"])
@_role("admin", "accountant")
def end_of_service():
    c = _db()
    try:
        if request.method == "POST":
            employee_id = int(request.form.get("employee_id", "0"))
            termination_date = date.fromisoformat(request.form.get("termination_date", ""))
            employee = execute_compat(c, "SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
            if not employee:
                raise ValueError("الموظف المحدد غير موجود.")
            service_start = date.fromisoformat(employee["hire_date"])
            if termination_date < service_start:
                raise ValueError("تاريخ انتهاء الخدمة لا يجوز أن يسبق تاريخ التعيين.")
            if employee["work_status"] not in ("resigned", "retired"):
                raise ValueError("يجب تسجيل الاستقالة أو الإحالة للمعاش في السجل الوظيفي أولًا.")
            if not employee["termination_date"] or employee["termination_date"] != termination_date.isoformat():
                raise ValueError("تاريخ التسوية يجب أن يطابق تاريخ انتهاء الخدمة المسجل في السجل الوظيفي.")
            basic_salary = _employee_salary_basis(c, employee_id, termination_date.isoformat())[0]
            if basic_salary <= 0:
                raise ValueError("لا يوجد راتب أساسي معتمد لهذا الموظف. سجّل أول كشف مرتبات مرحّل أو راجع بيانات الأجر قبل حساب نهاية الخدمة.")
            service_months, service_days = _service_duration_30_day(service_start, termination_date)
            retirement_age_raw = request.form.get("retirement_age", "").strip()
            retirement_age = int(retirement_age_raw) if retirement_age_raw else None
            stamp_duty = M(request.form.get("stamp_duty", "0"))
            if retirement_age is not None and not 1 <= retirement_age <= 100:
                raise ValueError("سن التقاعد يجب أن يكون بين 1 و100، أو اتركه فارغًا.")
            if stamp_duty < 0:
                raise ValueError("الدمغة لا يجوز أن تكون سالبة.")

            # Company policy: one basic salary per service month, prorating days over 30,
            # with a hard cap of 20 basic-salary months. Stamp duty is deducted afterward.
            eligible_months = min(Decimal(service_months) + Decimal(service_days) / Decimal("30"), Decimal("20"))
            gross = M(basic_salary * eligible_months)
            if stamp_duty > gross:
                raise ValueError("الدمغة لا يجوز أن تتجاوز إجمالي مكافأة نهاية الخدمة.")
            net = M(gross - stamp_duty)
            years = Decimal((termination_date - service_start).days) / Decimal("365.2425")
            settlement_no = "EOS-" + employee["employee_code"] + "-" + termination_date.strftime("%Y%m%d")
            begin_write_transaction(c)
            execute_compat(c, """INSERT INTO employee_end_of_service_settlements
                (settlement_no,employee_id,termination_date,reason,service_start_date,service_years,
                 gross_entitlement,deductions,net_payable,notes,created_by,basic_salary_snapshot,
                 service_months,service_days,retirement_age,stamp_duty)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (settlement_no,employee_id,termination_date.isoformat(),
                 request.form.get("reason","").strip(),service_start.isoformat(),str(years),
                 str(gross),str(stamp_duty),str(net),request.form.get("notes","").strip(),
                 session["user_id"],str(basic_salary),service_months,service_days,retirement_age,str(stamp_duty)))
            c.commit()
            flash("تم حفظ مسودة نهاية الخدمة. حُسبت مدة الخدمة من التواريخ المسجلة، أما قيمة المكافأة فهي وفق لائحة الشركة الداخلية بحد أقصى 20 شهرًا وليست معادلة قانونية عامة. لم يتم اعتمادها أو صرفها.", "success")
            return redirect(url_for("workforce.end_of_service"))
        employee_rows = execute_compat(c, """SELECT id,employee_code,full_name,hire_date,work_status,termination_date,basic_salary
            FROM employees WHERE work_status IN ('resigned','retired')
            ORDER BY termination_date DESC,employee_code""").fetchall()
        employees = []
        for employee_row in employee_rows:
            item = dict(employee_row)
            if item["termination_date"]:
                item["basic_salary"] = str(_employee_salary_basis(c, item["id"], item["termination_date"])[0])
            employees.append(item)
        settlement_rows = execute_compat(c, """SELECT s.*,e.employee_code,e.full_name,
                COALESCE((SELECT SUM(CAST(p.amount AS NUMERIC)) FROM employee_end_of_service_payments p
                          WHERE p.settlement_id=s.id),0) AS paid_amount
            FROM employee_end_of_service_settlements s JOIN employees e ON e.id=s.employee_id
            ORDER BY s.created_at DESC,s.id DESC""").fetchall()
        settlements = []
        for settlement_row in settlement_rows:
            item = dict(settlement_row)
            item["paid_amount"] = M(item["paid_amount"])
            item["remaining_amount"] = M(max(Decimal("0"),D(item["net_payable"])-item["paid_amount"]))
            settlements.append(item)
        payment_history = execute_compat(c, """SELECT p.*,a.code AS account_code,a.name_ar AS account_name,
                s.settlement_no,e.employee_code,e.full_name
            FROM employee_end_of_service_payments p JOIN accounts a ON a.id=p.cash_account_id
            JOIN employee_end_of_service_settlements s ON s.id=p.settlement_id
            JOIN employees e ON e.id=s.employee_id
            ORDER BY p.payment_date DESC,p.id DESC LIMIT 100""").fetchall()
        payment_accounts = execute_compat(c, """SELECT a.id,a.code,a.name_ar FROM accounts a
            JOIN cash_accounts ca ON ca.account_id=a.id
            WHERE a.active=1 AND a.is_group=0 AND a.account_type='asset' AND ca.active=1
            ORDER BY a.code""").fetchall()
        return render_template("end_of_service.html", employees=employees, settlements=settlements,
                               accounts=_accounts(c), payment_accounts=payment_accounts,
                               payment_history=payment_history,today=date.today().isoformat())
    except Exception as exc:
        try:
            c.rollback()
        except Exception:
            pass
        flash(safe_error_message(exc), "error")
        return redirect(url_for("workforce.end_of_service"))
    finally:
        c.close()

@workforce_bp.route("/hr/end-of-service/<int:settlement_id>/delete", methods=["POST"])
@_role("admin", "accountant")
def delete_end_of_service_draft(settlement_id):
    c=_db()
    try:
        begin_write_transaction(c)
        row=execute_compat(c, "SELECT * FROM employee_end_of_service_settlements WHERE id=?",(settlement_id,)).fetchone()
        if not row or row["status"]!="draft" or row["journal_entry_id"]:
            raise ValueError("حذف تسوية نهاية الخدمة متاح للمسودة غير المعتمدة فقط.")
        if execute_compat(c, "SELECT 1 FROM employee_end_of_service_payments WHERE settlement_id=? LIMIT 1",(settlement_id,)).fetchone():
            raise ValueError("لا يمكن حذف تسوية مرتبطة بدفعات صرف.")
        delete_cursor = execute_compat(c, "DELETE FROM employee_end_of_service_settlements WHERE id=? AND status='draft'",(settlement_id,))
        if delete_cursor.rowcount != 1: raise ValueError("لم يتم حذف المسودة؛ ربما تغيرت حالتها.")
        _audit(c,"employee_end_of_service_draft_deleted",row["settlement_no"])
        c.commit(); flash("تم حذف مسودة نهاية الخدمة فقط.","success")
    except _DB_VALUE_ERRORS as exc:
        c.rollback(); flash(str(exc) or "تعذر حذف مسودة نهاية الخدمة.","error")
    c.close()
    return redirect(url_for("workforce.end_of_service"))


@workforce_bp.route("/hr/end-of-service/<int:settlement_id>/approve", methods=["POST"])
@_role("admin", "accountant", permission="workforce.approve")
def approve_end_of_service(settlement_id):
    c=_db()
    try:
        begin_write_transaction(c)
        row=execute_compat(c, """SELECT s.*,e.full_name,e.employee_code FROM employee_end_of_service_settlements s
            JOIN employees e ON e.id=s.employee_id WHERE s.id=?""",(settlement_id,)).fetchone()
        if not row or row["status"]!="draft" or row["journal_entry_id"] or row["approval_status"]!="pending":
            raise ValueError("لا يمكن اعتماد إلا مسودة نهاية خدمة بانتظار المراجعة وغير مرحلة.")
        if row["created_by"] is None or row["created_by"] == session["user_id"]:
            raise ValueError("لا يجوز اعتماد تسوية نهاية الخدمة بواسطة منشئها، أو دون بيانات منشئ موثقة.")
        expense_id,payable_id=int(request.form["expense_account_id"]),int(request.form["payable_account_id"])
        stamp_id_raw=request.form.get("stamp_payable_account_id","").strip()
        gross,stamp,net=M(row["gross_entitlement"]),M(row["stamp_duty"]),M(row["net_payable"])
        if gross<=0 or M(gross-stamp)!=net: raise ValueError("بيانات المكافأة أو الدمغة غير متطابقة.")
        for account_id,expected,label in ((expense_id,"expense","مصروف المكافأة"),(payable_id,"liability","المكافأة المستحقة")):
            a=execute_compat(c, "SELECT account_type,is_group,active FROM accounts WHERE id=?",(account_id,)).fetchone()
            if not a or not a["active"] or a["is_group"] or a["account_type"]!=expected:
                raise ValueError("اختر حساب "+label+" تفصيليًا ونشطًا وبطبيعة صحيحة.")
        lines=[(expense_id,gross,0,"مصروف مكافأة نهاية الخدمة - "+row["employee_code"]),
               (payable_id,0,net,"صافي مكافأة نهاية الخدمة المستحقة")]
        if stamp>0:
            if not stamp_id_raw: raise ValueError("اختر حساب الدمغة المستحقة قبل اعتماد التسوية.")
            stamp_id=int(stamp_id_raw)
            a=execute_compat(c, "SELECT account_type,is_group,active FROM accounts WHERE id=?",(stamp_id,)).fetchone()
            if not a or not a["active"] or a["is_group"] or a["account_type"]!="liability":
                raise ValueError("اختر حساب دمغة مستحقة تفصيليًا ونشطًا.")
            lines.append((stamp_id,0,stamp,"دمغة مستحقة على مكافأة نهاية الخدمة"))
        if M(sum((D(x[1]) for x in lines),Decimal("0")))!=M(sum((D(x[2]) for x in lines),Decimal("0"))):
            raise ValueError("القيد المقترح غير متوازن؛ لم يتم اعتماد التسوية.")
        ref="EOS-POST-"+str(settlement_id)
        journal=post_journal(c,row["termination_date"],ref,"اعتماد مكافأة نهاية الخدمة للموظف "+row["full_name"],lines,session["user_id"])
        update_cursor = execute_compat(c, """UPDATE employee_end_of_service_settlements SET status='approved',journal_entry_id=?,
            approved_by=?,approved_at=CURRENT_TIMESTAMP,approval_status='approved'
            WHERE id=? AND status='draft' AND journal_entry_id IS NULL AND approval_status='pending'""",
            (journal,session["user_id"],settlement_id))
        if update_cursor.rowcount != 1: raise ValueError("تعذر تثبيت اعتماد التسوية.")
        _audit(c,"employee_end_of_service_approved",row["settlement_no"]+" journal="+str(journal))
        c.commit(); flash("تم اعتماد مكافأة نهاية الخدمة وترحيل قيد المصروف والاستحقاق والدمغة.","success")
    except Exception as exc:
        c.rollback(); flash(safe_error_message(exc),"error")
    c.close()
    return redirect(url_for("workforce.end_of_service"))


@workforce_bp.route("/hr/end-of-service/<int:settlement_id>/pay", methods=["POST"])
@_role("admin", "accountant")
def pay_end_of_service(settlement_id):
    c=_db()
    try:
        pay_date=date.fromisoformat(request.form.get("payment_date") or date.today().isoformat()).isoformat()
        amount=M(request.form.get("amount","0"))
        if amount<=0: raise ValueError("مبلغ الصرف يجب أن يكون أكبر من صفر.")
        cash_id=int(request.form["cash_account_id"])
        begin_write_transaction(c); _cash_or_bank_account(c,cash_id)
        row=execute_compat(c, "SELECT * FROM employee_end_of_service_settlements WHERE id=?",(settlement_id,)).fetchone()
        if not row or row["status"]!="approved" or not row["journal_entry_id"]:
            raise ValueError("يجب اعتماد مكافأة نهاية الخدمة وترحيل قيدها قبل الصرف.")
        if row["approved_by"] is not None and row["approved_by"] == session["user_id"]:
            raise ValueError("يجب أن ينفذ صرف مكافأة نهاية الخدمة مستخدم مختلف عن معتمدها.")
        paid=M(sum((D(x["amount"]) for x in execute_compat(c, 
            "SELECT amount FROM employee_end_of_service_payments WHERE settlement_id=?",(settlement_id,)).fetchall()),Decimal("0")))
        outstanding=M(max(Decimal("0"),D(row["net_payable"])-paid))
        if outstanding<=0 or amount>outstanding: raise ValueError("مبلغ الصرف يتجاوز صافي مكافأة نهاية الخدمة المتبقي.")
        payable=execute_compat(c, """SELECT account_id FROM journal_lines WHERE entry_id=?
            AND memo='صافي مكافأة نهاية الخدمة المستحقة' AND CAST(credit AS NUMERIC)>0 ORDER BY id LIMIT 1""",
            (row["journal_entry_id"],)).fetchone()
        if not payable: raise ValueError("لم يتم العثور على حساب المكافأة المستحقة في القيد الأصلي.")
        reference="EOS-PAY-"+str(settlement_id)+"-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
        journal=post_journal(c,pay_date,reference,"صرف مكافأة نهاية الخدمة رقم "+row["settlement_no"],
            [(int(payable["account_id"]),amount,0,"تخفيض مكافأة نهاية الخدمة المستحقة"),
             (cash_id,0,amount,"صرف مكافأة نهاية الخدمة من البنك/الصندوق")],session["user_id"])
        execute_compat(c, """INSERT INTO employee_end_of_service_payments
            (settlement_id,payment_date,amount,cash_account_id,journal_entry_id,reference,notes,created_by)
            VALUES(?,?,?,?,?,?,?,?)""",
            (settlement_id,pay_date,str(amount),cash_id,journal,reference,request.form.get("notes","").strip()[:250],session["user_id"]))
        new_status="paid" if M(paid+amount)>=D(row["net_payable"]) else "approved"
        execute_compat(c, """UPDATE employee_end_of_service_settlements SET status=?,
            paid_at=CASE WHEN ?='paid' THEN CURRENT_TIMESTAMP ELSE paid_at END
            WHERE id=? AND status='approved'""",(new_status,new_status,settlement_id))
        _audit(c,"employee_end_of_service_payment",row["settlement_no"]+" amount="+str(amount)+" journal="+str(journal))
        c.commit(); flash("تم صرف مكافأة نهاية الخدمة بالكامل وترحيل قيد السداد." if new_status=="paid"
            else "تم تسجيل دفعة جزئية لمكافأة نهاية الخدمة وترحيل قيدها.","success")
    except Exception as exc:
        c.rollback(); flash(safe_error_message(exc),"error")
    c.close()
    return redirect(url_for("workforce.end_of_service"))


@workforce_bp.route("/hr/end-of-service/<int:settlement_id>/print", methods=["GET"])
@_role("admin", "accountant", "viewer")
def print_end_of_service(settlement_id):
    c=_db()
    settlement=execute_compat(c, """SELECT s.*,e.employee_code,e.full_name,e.job_title,e.department
        FROM employee_end_of_service_settlements s JOIN employees e ON e.id=s.employee_id WHERE s.id=?""",
        (settlement_id,)).fetchone()
    if not settlement:
        c.close(); flash("تسوية نهاية الخدمة غير موجودة.","error")
        return redirect(url_for("workforce.end_of_service"))
    payments=execute_compat(c, """SELECT p.*,a.code AS account_code,a.name_ar AS account_name
        FROM employee_end_of_service_payments p JOIN accounts a ON a.id=p.cash_account_id
        WHERE p.settlement_id=? ORDER BY p.payment_date,p.id""",(settlement_id,)).fetchall()
    c.close()
    paid=M(sum((D(x["amount"]) for x in payments),Decimal("0")))
    return render_template("end_of_service_print.html",settlement=settlement,payments=payments,
        paid_amount=paid,remaining_amount=M(max(Decimal("0"),D(settlement["net_payable"])-paid)))
