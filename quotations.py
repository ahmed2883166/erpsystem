"""Customer price lists and quotation workflow."""
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from datetime import date,datetime
from pathlib import Path
from functools import wraps
from flask import Blueprint,flash,redirect,render_template,request,session,url_for
from sales import post_invoice

quotes_bp=Blueprint("quotes",__name__,url_prefix="/sales/quotations")
DB_PATH=Path(__file__).resolve().parent/"data"/"nexus.db"
M=Decimal("0.01")

def db():
 c=sqlite3.connect(DB_PATH,timeout=20,isolation_level=None); c.row_factory=sqlite3.Row
 c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=20000"); return c

def initialize_quotes_schema():
 c=db(); c.executescript("""
 CREATE TABLE IF NOT EXISTS customer_price_rules(
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  product_id INTEGER NOT NULL REFERENCES stock_products(id), unit_price TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT, min_qty TEXT NOT NULL DEFAULT '1',
  active INTEGER NOT NULL DEFAULT 1, UNIQUE(customer_id,product_id,valid_from));
 CREATE TABLE IF NOT EXISTS quotations(
  id INTEGER PRIMARY KEY, quote_no TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL REFERENCES customers(id),
  quote_date TEXT NOT NULL, valid_until TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
  subtotal TEXT NOT NULL, tax_rate TEXT NOT NULL, tax_amount TEXT NOT NULL, total_amount TEXT NOT NULL,
  sales_invoice_id INTEGER, notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
 CREATE TABLE IF NOT EXISTS quotation_lines(
  id INTEGER PRIMARY KEY, quotation_id INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
  product_id INTEGER REFERENCES stock_products(id), description TEXT NOT NULL,
  quantity TEXT NOT NULL, unit_price TEXT NOT NULL, line_total TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS idx_quotes_customer_dates ON quotations(customer_id,quote_date,valid_until);
 """); c.close()

def require(write=False):
 def deco(fn):
  @wraps(fn)
  def wrapped(*a,**kw):
   if not session.get("user_id"): return redirect(url_for("login"))
   if write and session.get("role") not in {"admin","accountant"}:
    flash("لا تملك صلاحية إنشاء أو تعديل عروض الأسعار.","error"); return redirect(url_for("quotes.index"))
   return fn(*a,**kw)
  return wrapped
 return deco

@quotes_bp.route("/",methods=["GET","POST"])
@require(write=True)
def index():
 c=db()
 if request.method=="POST":
  try:
   customer=int(request.form["customer_id"]); qdate=request.form["quote_date"]; valid=request.form["valid_until"]
   if valid<qdate: raise ValueError("تاريخ انتهاء العرض يجب أن يكون بعد تاريخ العرض.")
   lines=[]; subtotal=Decimal("0")
   pids=request.form.getlist("product_id"); descs=request.form.getlist("description")
   qtys=request.form.getlist("quantity"); prices=request.form.getlist("unit_price")
   for pid,desc,qty,price in zip(pids,descs,qtys,prices):
    if not pid and not desc.strip(): continue
    q=Decimal(qty); p=Decimal(price)
    if q<=0 or p<0: raise ValueError("الكمية والسعر غير صحيحين.")
    total=(q*p).quantize(M,rounding=ROUND_HALF_UP); subtotal+=total
    lines.append((int(pid) if pid else None,desc.strip() or "خدمة",str(q),str(p),str(total)))
   if not lines: raise ValueError("أضف بندًا واحدًا على الأقل.")
   rate=Decimal(request.form.get("tax_rate","0")); tax=(subtotal*rate/100).quantize(M,rounding=ROUND_HALF_UP)
   no="QT-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
   cur=c.execute("INSERT INTO quotations(quote_no,customer_id,quote_date,valid_until,subtotal,tax_rate,tax_amount,total_amount,notes,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
    (no,customer,qdate,valid, str(subtotal),str(rate),str(tax),str(subtotal+tax),request.form.get("notes",""),session["user_id"]))
   qid=cur.lastrowid
   c.executemany("INSERT INTO quotation_lines(quotation_id,product_id,description,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?)",[(qid,*x) for x in lines])
   c.commit(); flash("تم حفظ عرض السعر "+no,"success"); return redirect(url_for("quotes.view",quote_id=qid))
  except (ValueError,KeyError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
 customers=c.execute("SELECT id,code,name FROM customers WHERE active=1 ORDER BY name").fetchall()
 products=c.execute("SELECT id,sku,name_ar FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
 settings={r["setting_key"]:r["setting_value"] for r in c.execute("SELECT setting_key,setting_value FROM company_settings WHERE setting_key='default_vat_rate'")}
 quotes=c.execute("SELECT q.*,cu.name customer_name FROM quotations q JOIN customers cu ON cu.id=q.customer_id ORDER BY q.id DESC").fetchall()
 c.close(); return render_template("quotations.html",customers=customers,products=products,quotes=quotes,today=date.today().isoformat(),vat=settings.get("default_vat_rate","0"))

@quotes_bp.route("/<int:quote_id>")
@require()
def view(quote_id):
 c=db(); q=c.execute("SELECT q.*,cu.name customer_name,cu.code customer_code FROM quotations q JOIN customers cu ON cu.id=q.customer_id WHERE q.id=?",(quote_id,)).fetchone()
 if not q: c.close(); flash("عرض السعر غير موجود.","error"); return redirect(url_for("quotes.index"))
 lines=c.execute("SELECT l.*,p.sku FROM quotation_lines l LEFT JOIN stock_products p ON p.id=l.product_id WHERE l.quotation_id=?",(quote_id,)).fetchall()
 c.close(); return render_template("quotation_view.html",q=q,lines=lines,today=date.today().isoformat())

@quotes_bp.route("/<int:quote_id>/status",methods=["POST"])
@require(write=True)
def status(quote_id):
 new=request.form.get("status")
 if new not in {"sent","accepted","rejected"}: flash("حالة العرض غير صحيحة.","error")
 else:
  c=db(); q=c.execute("SELECT * FROM quotations WHERE id=?",(quote_id,)).fetchone()
  if q and q["status"] in {"draft","sent"}:
   c.execute("UPDATE quotations SET status=? WHERE id=?",(new,quote_id)); c.commit(); flash("تم تحديث حالة العرض.","success")
  c.close()
 return redirect(url_for("quotes.view",quote_id=quote_id))

@quotes_bp.route("/<int:quote_id>/convert",methods=["POST"])
@require(write=True)
def convert(quote_id):
 c=db()
 try:
  q=c.execute("SELECT * FROM quotations WHERE id=?",(quote_id,)).fetchone()
  if not q: raise ValueError("عرض السعر غير موجود.")
  if q["sales_invoice_id"]: raise ValueError("تم تحويل العرض إلى فاتورة بالفعل.")
  if q["status"] not in {"accepted","sent"}: raise ValueError("يجب قبول العرض أو إرساله قبل التحويل.")
  if q["valid_until"]<date.today().isoformat(): raise ValueError("انتهت صلاحية عرض السعر.")
  lines=c.execute("SELECT * FROM quotation_lines WHERE quotation_id=?",(quote_id,)).fetchall()
  items=[{"product_id":x["product_id"],"description":x["description"],"quantity":x["quantity"],"unit_price":x["unit_price"]} for x in lines]
  c.execute("BEGIN IMMEDIATE")
  result=post_invoice(c,q["customer_id"],date.today().isoformat(),None,items,q["tax_rate"],session["user_id"])
  c.execute("UPDATE quotations SET status='converted',sales_invoice_id=? WHERE id=?",(result["invoice_id"],quote_id))
  c.commit(); flash("تم تحويل العرض إلى فاتورة بيع "+result["invoice_no"],"success")
 except (ValueError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
 finally: c.close()
 return redirect(url_for("quotes.view",quote_id=quote_id))

@quotes_bp.route("/price-rules",methods=["GET","POST"])
@require(write=True)
def price_rules():
 c=db()
 if request.method=="POST":
  try:
   price=Decimal(request.form["unit_price"]); qty=Decimal(request.form.get("min_qty","1"))
   if price<0 or qty<=0 or request.form.get("valid_to") and request.form["valid_to"]<request.form["valid_from"]: raise ValueError("راجع السعر والكمية وفترة الصلاحية.")
   c.execute("INSERT INTO customer_price_rules(customer_id,product_id,unit_price,valid_from,valid_to,min_qty) VALUES(?,?,?,?,?,?)",
    (int(request.form["customer_id"]),int(request.form["product_id"]),str(price),request.form["valid_from"],request.form.get("valid_to") or None,str(qty)))
   c.commit(); flash("تم حفظ السعر الخاص بالعميل.","success")
  except (ValueError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
 customers=c.execute("SELECT id,code,name FROM customers WHERE active=1 ORDER BY name").fetchall()
 products=c.execute("SELECT id,sku,name_ar FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
 rules=c.execute("SELECT r.*,cu.name customer_name,p.name_ar product_name,p.sku FROM customer_price_rules r JOIN customers cu ON cu.id=r.customer_id JOIN stock_products p ON p.id=r.product_id ORDER BY r.valid_from DESC").fetchall()
 c.close(); return render_template("customer_price_rules.html",customers=customers,products=products,rules=rules,today=date.today().isoformat())
