"""Customer and supplier receivables/payables aging reports."""
import sqlite3
from datetime import date
from pathlib import Path
from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from functools import wraps

aging_bp=Blueprint("aging",__name__,url_prefix="/reports/aging")
DB_PATH=Path(__file__).resolve().parent/"data"/"nexus.db"

def db():
 c=sqlite3.connect(DB_PATH,timeout=20); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c

def initialize_aging_schema():
 c=db()
 for table in ("sales_invoices","supplier_bills"):
  cols={r["name"] for r in c.execute("PRAGMA table_info("+table+")")}
  if "due_date" not in cols: c.execute("ALTER TABLE "+table+" ADD COLUMN due_date TEXT")
 c.execute("UPDATE sales_invoices SET due_date=invoice_date WHERE due_date IS NULL OR due_date=''")
 c.execute("UPDATE supplier_bills SET due_date=bill_date WHERE due_date IS NULL OR due_date=''")
 c.commit(); c.close()

def auth(fn):
 def wrapped(*a,**kw):
  if not session.get("user_id"): return redirect(url_for("login"))
  if "reports.view" not in {"reports.view"}: return redirect(url_for("dashboard"))
  return fn(*a,**kw)
 return wrapped

@aging_bp.route("/")
@auth
def index():
 kind=request.args.get("kind","customers")
 if kind not in {"customers","suppliers"}: kind="customers"
 as_of=request.args.get("as_of") or date.today().isoformat()
 c=db()
 if kind=="customers":
  rows=c.execute("""SELECT i.invoice_no doc_no,cu.code party_code,cu.name party_name,i.invoice_date doc_date,
    COALESCE(i.due_date,i.invoice_date) due_date,CAST(i.total_amount AS REAL)-CAST(i.paid_amount AS REAL) balance
    FROM sales_invoices i JOIN customers cu ON cu.id=i.customer_id
    WHERE i.status='posted' AND CAST(i.total_amount AS REAL)>CAST(i.paid_amount AS REAL) AND i.invoice_date<=?
    ORDER BY due_date,cu.name""",(as_of,)).fetchall()
 else:
  rows=c.execute("""SELECT b.bill_no doc_no,s.code party_code,s.name party_name,b.bill_date doc_date,
    COALESCE(b.due_date,b.bill_date) due_date,CAST(b.total_amount AS REAL)-CAST(b.paid_amount AS REAL) balance
    FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
    WHERE b.status='posted' AND CAST(b.total_amount AS REAL)>CAST(b.paid_amount AS REAL) AND b.bill_date<=?
    ORDER BY due_date,s.name""",(as_of,)).fetchall()
 c.close(); today=date.fromisoformat(as_of); result=[]; totals=[0.0]*5
 for r in rows:
  days=(today-date.fromisoformat(r["due_date"])).days
  bucket=0 if days<=0 else 1 if days<=30 else 2 if days<=60 else 3 if days<=90 else 4
  vals=[0.0]*5; vals[bucket]=float(r["balance"])
  for i,v in enumerate(vals): totals[i]+=v
  result.append({**dict(r),"days":max(days,0),"buckets":vals})
 return render_template("aging_report.html",kind=kind,as_of=as_of,rows=result,totals=totals)

