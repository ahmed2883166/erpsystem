"""Shared SQLite fixtures for party statements and aging report tests."""


def initialize_party_reporting_schema(conn):
    """Create the common customer/supplier document schema used by report tests."""
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS customers(
        id INTEGER PRIMARY KEY, code TEXT, name TEXT, active INTEGER DEFAULT 1
      );
      CREATE TABLE IF NOT EXISTS suppliers(
        id INTEGER PRIMARY KEY, code TEXT, name TEXT, active INTEGER DEFAULT 1
      );
      CREATE TABLE IF NOT EXISTS sales_invoices(
        id INTEGER PRIMARY KEY, customer_id INTEGER, invoice_no TEXT,
        invoice_date TEXT, due_date TEXT, total_amount TEXT,
        paid_amount TEXT DEFAULT '0', credited_amount TEXT DEFAULT '0', status TEXT
      );
      CREATE TABLE IF NOT EXISTS sales_credit_notes(
        id INTEGER PRIMARY KEY, invoice_id INTEGER, credit_note_no TEXT,
        credit_date TEXT, reason TEXT, total_amount TEXT, status TEXT
      );
      CREATE TABLE IF NOT EXISTS customer_refunds(
        id INTEGER PRIMARY KEY, credit_note_id INTEGER, refund_no TEXT,
        refund_date TEXT, amount TEXT
      );
      CREATE TABLE IF NOT EXISTS customer_receipts(
        id INTEGER PRIMARY KEY, invoice_id INTEGER, receipt_no TEXT,
        receipt_date TEXT, amount TEXT, reference TEXT
      );
      CREATE TABLE IF NOT EXISTS supplier_bills(
        id INTEGER PRIMARY KEY, supplier_id INTEGER, bill_no TEXT,
        bill_date TEXT, due_date TEXT, total_amount TEXT,
        paid_amount TEXT DEFAULT '0', credited_amount TEXT DEFAULT '0', status TEXT
      );
      CREATE TABLE IF NOT EXISTS supplier_credit_notes(
        id INTEGER PRIMARY KEY, credit_no TEXT, supplier_id INTEGER, bill_id INTEGER,
        credit_date TEXT, reason TEXT, total_amount TEXT, status TEXT
      );
      CREATE TABLE IF NOT EXISTS supplier_payments(
        id INTEGER PRIMARY KEY, supplier_id INTEGER, payment_no TEXT,
        payment_date TEXT, amount TEXT, reference TEXT
      );
    """)
