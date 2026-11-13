from app_paths import DB_PATH
from database import connect_database
"""Customer and supplier receivables/payables aging reports."""
from datetime import date
from decimal import Decimal, InvalidOperation
from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from functools import wraps

aging_bp=Blueprint("aging",__name__,url_prefix="/reports/aging")

def db():
 return connect_database(DB_PATH)

def initialize_aging_schema():
 c=db()
 for table in ("sales_invoices","supplier_bills"):
  cols={r["name"] for r in c.execute("PRAGMA table_info("+table+")")}
  if "due_date" not in cols: c.execute("ALTER TABLE "+table+" ADD COLUMN due_date TEXT")
 c.execute("UPDATE sales_invoices SET due_date=invoice_date WHERE due_date IS NULL OR due_date=''")
 c.execute("UPDATE supplier_bills SET due_date=bill_date WHERE due_date IS NULL OR due_date=''")
 c.commit(); c.close()

def auth(fn):
 @wraps(fn)
 def wrapped(*a,**kw):
  if not session.get("user_id"): return redirect(url_for("login"))
  kind=request.args.get("kind","customers")
  permission="purchases.view" if kind=="suppliers" else "sales.view"
  allowed=(permission in set(session["permissions"])) if "permissions" in session else (session.get("role") in {"admin","accountant","viewer"})
  if not allowed:
   flash("لا تملك صلاحية عرض تقرير أعمار الديون.","error")
   return redirect(url_for("dashboard"))
  return fn(*a,**kw)
 return wrapped

def _amount(value):
 try:
  amount=Decimal(str(value or "0"))
 except (InvalidOperation,ValueError,TypeError):
  raise ValueError("يوجد مبلغ غير صالح في مستندات الأعمار.")
 if not amount.is_finite():
  raise ValueError("يوجد مبلغ غير محدود في مستندات الأعمار.")
 return amount


def _table_exists(conn, name):
 return conn.execute(
  "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
 ).fetchone() is not None


def _customer_aging_movements(conn, as_of):
 # paid_amount and credited_amount are running totals. They cannot be used
 # for a historical aging date because they include later receipts/credits.
 # Rebuild the balances from dated subledger transactions when those tables
 # are available; retain the legacy totals only for older installations.
 required=("customer_receipts","customer_receipt_batches",
           "customer_receipt_allocations","customer_advance_allocations")
 has_detail=all(_table_exists(conn, table) for table in required)
 paid_as_of={}
 paid_all={}
 if has_detail:
  sources=(
   ("SELECT invoice_id,amount FROM customer_receipts WHERE receipt_date<=?",(as_of,)),
   ("""SELECT a.invoice_id,a.amount FROM customer_receipt_allocations a
       JOIN customer_receipt_batches b ON b.id=a.batch_id WHERE b.receipt_date<=?""",(as_of,)),
   ("SELECT invoice_id,amount FROM customer_advance_allocations WHERE allocation_date<=?",(as_of,)),
  )
  all_sources=(
   ("SELECT invoice_id,amount FROM customer_receipts",()),
   ("SELECT invoice_id,amount FROM customer_receipt_allocations",()),
   ("SELECT invoice_id,amount FROM customer_advance_allocations",()),
  )
  for sql,params in sources:
   for invoice_id,amount in conn.execute(sql,params).fetchall():
    paid_as_of[invoice_id]=paid_as_of.get(invoice_id,Decimal("0"))+_amount(amount)
  for sql,params in all_sources:
   for invoice_id,amount in conn.execute(sql,params).fetchall():
    paid_all[invoice_id]=paid_all.get(invoice_id,Decimal("0"))+_amount(amount)
 credited_as_of={}
 credited_all={}
 if _table_exists(conn,"sales_credit_notes"):
  columns={row["name"] for row in conn.execute("PRAGMA table_info(sales_credit_notes)")}
  if "cancel_journal_entry_id" in columns and _table_exists(conn,"journal_entries"):
   sql="""SELECT cn.invoice_id,cn.total_amount amount
          FROM sales_credit_notes cn
          LEFT JOIN journal_entries reversal ON reversal.id=cn.cancel_journal_entry_id
          WHERE cn.credit_date<=?
            AND (cn.status='posted' OR
                 (cn.status='cancelled' AND reversal.entry_date>?))"""
  else:
   sql="""SELECT invoice_id,total_amount amount FROM sales_credit_notes
          WHERE status='posted' AND credit_date<=?"""
  params=(as_of,as_of) if "reversal" in sql else (as_of,)
  for invoice_id,amount in conn.execute(
   """SELECT invoice_id,total_amount amount FROM sales_credit_notes WHERE status='posted'""",()
  ).fetchall():
   credited_all[invoice_id]=credited_all.get(invoice_id,Decimal("0"))+_amount(amount)
  for invoice_id,amount in conn.execute(sql,params).fetchall():
   credited_as_of[invoice_id]=credited_as_of.get(invoice_id,Decimal("0"))+_amount(amount)
 refunds_as_of={}
 if _table_exists(conn,"customer_refunds") and _table_exists(conn,"sales_credit_notes"):
  for invoice_id,amount in conn.execute(
   """SELECT cn.invoice_id,rf.amount FROM customer_refunds rf
      JOIN sales_credit_notes cn ON cn.id=rf.credit_note_id
      WHERE cn.credit_date<=? AND rf.refund_date<=?""",(as_of,as_of)
  ).fetchall():
   refunds_as_of[invoice_id]=refunds_as_of.get(invoice_id,Decimal("0"))+_amount(amount)
 return has_detail,paid_as_of,paid_all,credited_as_of,credited_all,refunds_as_of


def _supplier_aging_movements(conn, as_of):
 has_detail=(_table_exists(conn,"supplier_payments") and
             _table_exists(conn,"supplier_payment_allocations"))
 paid_as_of={}
 paid_all={}
 if has_detail:
  for bill_id,amount in conn.execute(
   """SELECT a.bill_id,a.amount FROM supplier_payment_allocations a
      JOIN supplier_payments p ON p.id=a.payment_id
      WHERE p.payment_date<=?""",(as_of,)
  ).fetchall():
   paid_as_of[bill_id]=paid_as_of.get(bill_id,Decimal("0"))+_amount(amount)
  for bill_id,amount in conn.execute(
   "SELECT bill_id,amount FROM supplier_payment_allocations",()
  ).fetchall():
   paid_all[bill_id]=paid_all.get(bill_id,Decimal("0"))+_amount(amount)
 credited_as_of={}
 credited_all={}
 if _table_exists(conn,"supplier_credit_notes"):
  for bill_id,amount in conn.execute(
   "SELECT bill_id,total_amount FROM supplier_credit_notes WHERE status='posted'",()
  ).fetchall():
   credited_all[bill_id]=credited_all.get(bill_id,Decimal("0"))+_amount(amount)
  for bill_id,amount in conn.execute(
   """SELECT bill_id,total_amount FROM supplier_credit_notes
      WHERE status='posted' AND credit_date<=?""",(as_of,)
  ).fetchall():
   credited_as_of[bill_id]=credited_as_of.get(bill_id,Decimal("0"))+_amount(amount)
 return has_detail,paid_as_of,paid_all,credited_as_of,credited_all


@aging_bp.route("/")
@auth
def index():
 kind=request.args.get("kind","customers")
 if kind not in {"customers","suppliers"}: kind="customers"
 try:
  as_of=request.args.get("as_of") or date.today().isoformat()
  today=date.fromisoformat(as_of)
  as_of=today.isoformat()
 except ValueError:
  flash("تاريخ التقرير غير صحيح.","error")
  today=date.today(); as_of=today.isoformat()
 c=db()
 if kind=="customers":
  rows=c.execute("""SELECT i.id invoice_id,i.invoice_no doc_no,cu.code party_code,cu.name party_name,i.invoice_date doc_date,
    COALESCE(i.due_date,i.invoice_date) due_date,i.total_amount total_amount,i.paid_amount paid_amount,
    COALESCE(i.credited_amount,'0') credited_amount
    FROM sales_invoices i JOIN customers cu ON cu.id=i.customer_id
    WHERE i.status='posted' AND i.invoice_date<=?
    ORDER BY due_date,cu.name""",(as_of,)).fetchall()
 else:
  rows=c.execute("""SELECT b.id invoice_id,b.bill_no doc_no,s.code party_code,s.name party_name,b.bill_date doc_date,
    COALESCE(b.due_date,b.bill_date) due_date,b.total_amount total_amount,b.paid_amount paid_amount,
    COALESCE(b.credited_amount,'0') credited_amount
    FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
    WHERE b.status='posted' AND b.bill_date<=?
    ORDER BY due_date,s.name""",(as_of,)).fetchall()
 if kind=="customers":
  has_detail,paid_by_invoice,paid_all,credited_by_invoice,credited_all,refunds_by_invoice=_customer_aging_movements(c,as_of)
  credit_table_exists=_table_exists(c,"sales_credit_notes")
 else:
  has_detail,paid_by_invoice,paid_all,credited_by_invoice,credited_all=_supplier_aging_movements(c,as_of)
  refunds_by_invoice={}
  credit_table_exists=_table_exists(c,"supplier_credit_notes")
 c.close()
 result=[]; totals=[Decimal("0.00") for _ in range(5)]
 for r in rows:
  document_id=r["invoice_id"]
  stored_paid=_amount(r["paid_amount"])
  paid_amount=(paid_by_invoice.get(document_id,Decimal("0"))+
               max(Decimal("0"),stored_paid-paid_all.get(document_id,Decimal("0")))) if has_detail else stored_paid
  stored_credit=_amount(r["credited_amount"] if "credited_amount" in r.keys() else "0")
  credited_amount=(credited_by_invoice.get(document_id,Decimal("0"))+
                   max(Decimal("0"),stored_credit-credited_all.get(document_id,Decimal("0")))) if credit_table_exists else stored_credit
  refund_amount=refunds_by_invoice.get(document_id,Decimal("0"))
  balance=_amount(r["total_amount"])-paid_amount-credited_amount+refund_amount
  if balance<=0:
   continue
  try:
   due_date=date.fromisoformat(r["due_date"])
  except (TypeError,ValueError):
   due_date=today
  days=(today-due_date).days
  bucket=0 if days<=0 else 1 if days<=30 else 2 if days<=60 else 3 if days<=90 else 4
  vals=[Decimal("0.00") for _ in range(5)]; vals[bucket]=balance
  for i,v in enumerate(vals): totals[i]+=v
  result.append({**dict(r),"balance":balance,"days":max(days,0),"buckets":vals})
 return render_template("aging_report.html",kind=kind,as_of=as_of,rows=result,totals=totals)
