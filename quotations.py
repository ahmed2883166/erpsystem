from app_paths import DB_PATH
from database import connect_database
"""Customer price lists and quotation workflow."""
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date,datetime
from functools import wraps
from flask import Blueprint,flash,redirect,render_template,request,session,url_for
from sales import post_invoice
from stock import consume_layers, post_issue_journal, ensure_stock_date_open

quotes_bp=Blueprint("quotes",__name__,url_prefix="/sales/quotations")

M=Decimal("0.01")

def db():
 return connect_database(DB_PATH, isolation_level=None)

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
  sales_invoice_id INTEGER, sales_order_id INTEGER, notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
 CREATE TABLE IF NOT EXISTS quotation_lines(
  id INTEGER PRIMARY KEY, quotation_id INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
  product_id INTEGER REFERENCES stock_products(id), description TEXT NOT NULL,
  quantity TEXT NOT NULL, unit_price TEXT NOT NULL, line_total TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS idx_quotes_customer_dates ON quotations(customer_id,quote_date,valid_until);
 CREATE TABLE IF NOT EXISTS sales_orders(
  id INTEGER PRIMARY KEY, order_no TEXT NOT NULL UNIQUE,
  quotation_id INTEGER NOT NULL UNIQUE REFERENCES quotations(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  warehouse_id INTEGER REFERENCES stock_warehouses(id),
  order_date TEXT NOT NULL, expected_date TEXT,
  status TEXT NOT NULL DEFAULT 'pending_approval',
  created_by INTEGER, submitted_by INTEGER,
  approved_by INTEGER, approved_at TEXT,
  rejection_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
 );
 CREATE TABLE IF NOT EXISTS sales_order_lines(
  id INTEGER PRIMARY KEY, sales_order_id INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
  quotation_line_id INTEGER REFERENCES quotation_lines(id),
  product_id INTEGER REFERENCES stock_products(id),
  description TEXT NOT NULL, ordered_qty TEXT NOT NULL,
  delivered_qty TEXT NOT NULL DEFAULT '0', invoiced_qty TEXT NOT NULL DEFAULT '0',
  unit_price TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
 );
 CREATE INDEX IF NOT EXISTS idx_sales_order_lines_order ON sales_order_lines(sales_order_id,id);
 CREATE TABLE IF NOT EXISTS delivery_notes(
  id INTEGER PRIMARY KEY, delivery_no TEXT NOT NULL UNIQUE,
  sales_order_id INTEGER NOT NULL REFERENCES sales_orders(id),
  delivery_date TEXT NOT NULL, warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
  status TEXT NOT NULL DEFAULT 'draft', notes TEXT NOT NULL DEFAULT '',
  created_by INTEGER, posted_by INTEGER, posted_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
 );
 CREATE TABLE IF NOT EXISTS delivery_note_lines(
  id INTEGER PRIMARY KEY, delivery_note_id INTEGER NOT NULL REFERENCES delivery_notes(id) ON DELETE CASCADE,
  sales_order_line_id INTEGER NOT NULL REFERENCES sales_order_lines(id),
  product_id INTEGER NOT NULL REFERENCES stock_products(id),
  quantity TEXT NOT NULL, unit_cost TEXT NOT NULL DEFAULT '0',
  total_cost TEXT NOT NULL DEFAULT '0', invoiced_qty TEXT NOT NULL DEFAULT '0',
  invoiced_cost TEXT NOT NULL DEFAULT '0',
  stock_movement_id INTEGER REFERENCES stock_movements(id),
  UNIQUE(delivery_note_id,sales_order_line_id)
 );
 CREATE TABLE IF NOT EXISTS delivery_note_line_lots(
  id INTEGER PRIMARY KEY, delivery_note_line_id INTEGER NOT NULL REFERENCES delivery_note_lines(id),
  lot_id INTEGER REFERENCES stock_lots(id), quantity TEXT NOT NULL, cost_total TEXT NOT NULL,
  invoiced_qty TEXT NOT NULL DEFAULT '0', invoiced_cost TEXT NOT NULL DEFAULT '0'
 );
 CREATE INDEX IF NOT EXISTS idx_delivery_note_order ON delivery_notes(sales_order_id,status,delivery_date,id);
 CREATE INDEX IF NOT EXISTS idx_delivery_line_order_line ON delivery_note_lines(sales_order_line_id,delivery_note_id);

 """);
 qcols={r["name"] for r in c.execute("PRAGMA table_info(quotations)")}
 if "sales_order_id" not in qcols: c.execute("ALTER TABLE quotations ADD COLUMN sales_order_id INTEGER")
 dcols={r["name"] for r in c.execute("PRAGMA table_info(delivery_note_lines)")}
 if "invoiced_cost" not in dcols: c.execute("ALTER TABLE delivery_note_lines ADD COLUMN invoiced_cost TEXT NOT NULL DEFAULT '0'")
 lcols={r["name"] for r in c.execute("PRAGMA table_info(delivery_note_line_lots)")}
 if "invoiced_qty" not in lcols: c.execute("ALTER TABLE delivery_note_line_lots ADD COLUMN invoiced_qty TEXT NOT NULL DEFAULT '0'")
 if "invoiced_cost" not in lcols: c.execute("ALTER TABLE delivery_note_line_lots ADD COLUMN invoiced_cost TEXT NOT NULL DEFAULT '0'")
 c.close()

def sales_permission(permission, fallback_roles):
 def deco(fn):
  @wraps(fn)
  def wrapped(*args,**kwargs):
   if not session.get("user_id"): return redirect(url_for("login"))
   allowed=(permission in set(session.get("permissions",[])) or
            (permission=="sales.approve" and session.get("role")=="admin")) if "permissions" in session else session.get("role") in fallback_roles
   if not allowed:
    flash("لا تملك الصلاحية المطلوبة لإدارة أوامر البيع والتسليم.","error")
    return redirect(url_for("dashboard"))
   return fn(*args,**kwargs)
  return wrapped
 return deco

require_sales_view=sales_permission("sales.view",{"admin","accountant","viewer"})
require_sales_manage=sales_permission("sales.manage",{"admin","accountant"})
require_sales_approval=sales_permission("sales.approve",{"admin"})


def require(write=False):
 def deco(fn):
  @wraps(fn)
  def wrapped(*a,**kw):
   if not session.get("user_id"): return redirect(url_for("login"))
   required = "quotes.manage" if write or request.method == "POST" else "quotes.view"
   if (required not in set(session["permissions"])) if "permissions" in session else (write and session.get("role") not in {"admin","accountant"}):
    flash("لا تملك صلاحية إنشاء أو تعديل عروض الأسعار.","error"); return redirect(url_for("quotes.index"))
   return fn(*a,**kw)
  return wrapped
 return deco


def create_sales_order_from_quotation(conn, quote_id, created_by, warehouse_id=None, expected_date=None):
 q=conn.execute("SELECT * FROM quotations WHERE id=?",(quote_id,)).fetchone()
 if not q: raise ValueError("عرض السعر غير موجود.")
 if q["status"]!="accepted": raise ValueError("لا يمكن إنشاء أمر بيع إلا من عرض سعر مقبول من العميل.")
 if q["sales_order_id"] if "sales_order_id" in q.keys() else False:
  raise ValueError("تم تحويل عرض السعر إلى أمر بيع بالفعل.")
 if q["valid_until"]<date.today().isoformat(): raise ValueError("انتهت صلاحية عرض السعر.")
 if expected_date and expected_date<date.today().isoformat(): raise ValueError("تاريخ التسليم المتوقع لا يمكن أن يسبق تاريخ أمر البيع.")
 lines=conn.execute("SELECT * FROM quotation_lines WHERE quotation_id=? ORDER BY id",(quote_id,)).fetchall()
 if not lines: raise ValueError("عرض السعر لا يحتوي بنودًا.")
 activity=conn.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
 activity_code=activity["setting_value"] if activity else "commercial"
 if activity_code!="service" and not warehouse_id:
  raise ValueError("حدد مخزن التسليم عند إنشاء أمر البيع.")
 if activity_code=="service" and any(line["product_id"] for line in lines):
  raise ValueError("نشاط الخدمات لا يقبل بنود أصناف مخزنية في أمر البيع.")
 if warehouse_id and not conn.execute("SELECT 1 FROM stock_warehouses WHERE id=? AND active=1",(warehouse_id,)).fetchone():
  raise ValueError("مخزن التسليم غير موجود أو غير نشط.")
 for line in lines:
  try:
   qty=Decimal(str(line["quantity"])); price=Decimal(str(line["unit_price"]))
  except (InvalidOperation,ValueError,TypeError) as exc:
   raise ValueError("كمية أو سعر أحد بنود عرض السعر غير صحيح.") from exc
  if not qty.is_finite() or qty<=0 or not price.is_finite() or price<0:
   raise ValueError("كمية أو سعر أحد بنود عرض السعر غير صحيح.")
 order_no="SO-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
 cur=conn.execute(
  """INSERT INTO sales_orders(order_no,quotation_id,customer_id,warehouse_id,order_date,
     expected_date,status,created_by,submitted_by)
     VALUES(?,?,?,?,?,?,'pending_approval',?,?)""",
  (order_no,quote_id,q["customer_id"],warehouse_id,date.today().isoformat(),
   expected_date or None,created_by,created_by))
 order_id=cur.lastrowid
 conn.executemany(
  """INSERT INTO sales_order_lines(sales_order_id,quotation_line_id,product_id,description,
     ordered_qty,unit_price) VALUES(?,?,?,?,?,?)""",
  [(order_id,x["id"],x["product_id"],x["description"],x["quantity"],x["unit_price"]) for x in lines])
 conn.execute("UPDATE quotations SET sales_order_id=? WHERE id=?",(order_id,quote_id))
 return {"sales_order_id":order_id,"order_no":order_no}


def approve_sales_order(conn, order_id, approver_id):
 order=conn.execute("SELECT * FROM sales_orders WHERE id=?",(order_id,)).fetchone()
 if not order: raise ValueError("أمر البيع غير موجود.")
 if order["status"]!="pending_approval": raise ValueError("أمر البيع ليس في انتظار الاعتماد.")
 if order["created_by"]==approver_id: raise ValueError("لا يجوز اعتماد أمر البيع بواسطة نفس منشئه.")
 activity=conn.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
 activity_code=activity["setting_value"] if activity else "commercial"
 if order["warehouse_id"] is None and activity_code!="service":
  raise ValueError("حدد مخزن التسليم قبل اعتماد أمر البيع.")
 if order["warehouse_id"] and not conn.execute(
  "SELECT 1 FROM stock_warehouses WHERE id=? AND active=1",(order["warehouse_id"],)
 ).fetchone():
  raise ValueError("مخزن التسليم غير موجود أو غير نشط.")
 conn.execute(
  "UPDATE sales_orders SET status='approved',approved_by=?,approved_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending_approval'",
  (approver_id,order_id))
 if conn.execute("SELECT changes()").fetchone()[0]!=1:
  raise ValueError("تم تغيير حالة أمر البيع؛ أعد تحميل الصفحة.")
 return True


def create_delivery_note(conn, order_id, delivery_date, lines, created_by, notes=""):
 order=conn.execute("SELECT * FROM sales_orders WHERE id=?",(order_id,)).fetchone()
 if not order: raise ValueError("أمر البيع غير موجود.")
 if order["status"] not in {"approved","partially_delivered","partially_invoiced"}:
  raise ValueError("لا يمكن إنشاء إذن تسليم قبل اعتماد أمر البيع أو بعد اكتمال التسليم.")
 ensure_stock_date_open(conn,delivery_date,"تاريخ إذن التسليم")
 if delivery_date<order["order_date"]:
  raise ValueError("تاريخ إذن التسليم لا يمكن أن يسبق تاريخ أمر البيع.")
 if not lines: raise ValueError("أضف بندًا واحدًا على الأقل لإذن التسليم.")
 delivery_no="DN-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
 cur=conn.execute(
  """INSERT INTO delivery_notes(delivery_no,sales_order_id,delivery_date,warehouse_id,
     status,notes,created_by) VALUES(?,?,?,?,'draft',?,?)""",
  (delivery_no,order_id,delivery_date,order["warehouse_id"],notes,created_by))
 note_id=cur.lastrowid
 seen=set()
 for item in lines:
  line_id=int(item["sales_order_line_id"])
  if line_id in seen: raise ValueError("لا تكرر نفس بند أمر البيع في إذن التسليم.")
  seen.add(line_id)
  line=conn.execute("SELECT * FROM sales_order_lines WHERE id=? AND sales_order_id=?",(line_id,order_id)).fetchone()
  if not line: raise ValueError("بند إذن التسليم لا يتبع أمر البيع المحدد.")
  try:
   qty=Decimal(str(item["quantity"]))
  except (InvalidOperation,ValueError,TypeError) as exc:
   raise ValueError("كمية التسليم غير صحيحة.") from exc
  remaining=Decimal(str(line["ordered_qty"]))-Decimal(str(line["delivered_qty"]))
  if not qty.is_finite() or qty<=0: raise ValueError("كمية التسليم يجب أن تكون أكبر من صفر.")
  if qty>remaining: raise ValueError("كمية التسليم تتجاوز الكمية المتبقية من أمر البيع.")
  if not line["product_id"]: raise ValueError("بنود الخدمات لا تحتاج إذن تسليم مخزني.")
  conn.execute(
   """INSERT INTO delivery_note_lines(delivery_note_id,sales_order_line_id,product_id,quantity)
      VALUES(?,?,?,?)""",(note_id,line_id,line["product_id"],str(qty)))
 return {"delivery_note_id":note_id,"delivery_no":delivery_no}


def post_delivery_note(conn, note_id, posted_by):
 note=conn.execute("SELECT * FROM delivery_notes WHERE id=?",(note_id,)).fetchone()
 if not note: raise ValueError("إذن التسليم غير موجود.")
 if note["status"]!="draft": raise ValueError("إذن التسليم تم ترحيله أو إلغاؤه بالفعل.")
 order=conn.execute("SELECT * FROM sales_orders WHERE id=?",(note["sales_order_id"],)).fetchone()
 if order["status"] not in {"approved","partially_delivered","partially_invoiced"}: raise ValueError("أمر البيع غير معتمد للتسليم.")
 ensure_stock_date_open(conn,note["delivery_date"],"تاريخ إذن التسليم")
 if note["delivery_date"]<order["order_date"]:
  raise ValueError("تاريخ إذن التسليم لا يمكن أن يسبق تاريخ أمر البيع.")
 if not conn.execute("SELECT 1 FROM stock_warehouses WHERE id=? AND active=1",(note["warehouse_id"],)).fetchone():
  raise ValueError("مخزن إذن التسليم غير موجود أو غير نشط.")
 rows=conn.execute("SELECT * FROM delivery_note_lines WHERE delivery_note_id=? ORDER BY id",(note_id,)).fetchall()
 if not rows: raise ValueError("إذن التسليم لا يحتوي بنودًا.")
 method_row=conn.execute("SELECT setting_value FROM company_settings WHERE setting_key='inventory_cost_method'").fetchone()
 method=method_row["setting_value"] if method_row else "average"
 total_cost=Decimal("0")
 for row in rows:
  order_line=conn.execute("SELECT * FROM sales_order_lines WHERE id=?",(row["sales_order_line_id"],)).fetchone()
  qty=Decimal(str(row["quantity"]))
  remaining=Decimal(str(order_line["ordered_qty"]))-Decimal(str(order_line["delivered_qty"]))
  if qty>remaining: raise ValueError("كمية التسليم تجاوزت المتبقي بسبب عملية تسليم أخرى.")
  product=conn.execute("SELECT * FROM stock_products WHERE id=? AND active=1",(row["product_id"],)).fetchone()
  if not product: raise ValueError("الصنف غير موجود أو غير نشط.")
  cost,allocations=consume_layers(conn,row["product_id"],note["warehouse_id"],qty,method)
  unit_cost=(cost/qty).quantize(M,rounding=ROUND_HALF_UP)
  movement_no=note["delivery_no"]+"-"+str(row["id"])
  cur=conn.execute(
   """INSERT INTO stock_movements(movement_no,movement_type,product_id,source_warehouse_id,
      quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status,issue_purpose)
      VALUES(?,'issue',?,?,?,?,?,?,?,?, 'pending','sale')""",
   (movement_no,row["product_id"],note["warehouse_id"],str(qty),str(unit_cost),str(cost),
    note["delivery_date"],"صرف إذن تسليم "+note["delivery_no"],posted_by))
  movement_id=cur.lastrowid
  post_issue_journal(conn,movement_id,movement_no,product,note["delivery_date"],cost,posted_by,"sale")
  conn.execute(
   "UPDATE delivery_note_lines SET unit_cost=?,total_cost=?,stock_movement_id=? WHERE id=?",
   (str(unit_cost),str(cost),movement_id,row["id"]))
  for lot_id,lot_qty,lot_unit_cost,lot_cost in allocations:
   conn.execute(
    "INSERT INTO delivery_note_line_lots(delivery_note_line_id,lot_id,quantity,cost_total) VALUES(?,?,?,?)",
    (row["id"],lot_id,str(lot_qty),str(lot_cost)))
  conn.execute("UPDATE sales_order_lines SET delivered_qty=? WHERE id=?",
   (str(Decimal(str(order_line["delivered_qty"]))+qty),order_line["id"]))
  total_cost+=cost
 remaining=conn.execute(
  "SELECT ordered_qty,delivered_qty FROM sales_order_lines WHERE sales_order_id=?",(order["id"],)).fetchall()
 all_delivered=all(Decimal(str(x["delivered_qty"]))>=Decimal(str(x["ordered_qty"])) for x in remaining)
 conn.execute("UPDATE delivery_notes SET status='posted',posted_by=?,posted_at=CURRENT_TIMESTAMP WHERE id=?",
  (posted_by,note_id))
 conn.execute("UPDATE sales_orders SET status=? WHERE id=?",
  ("delivered" if all_delivered else "partially_delivered",order["id"]))
 return {"delivery_note_id":note_id,"total_cost":total_cost,"fully_delivered":all_delivered}


def invoice_sales_order(conn, order_id, invoice_date, due_date, created_by, dimensions=None):
 order=conn.execute("SELECT * FROM sales_orders WHERE id=?",(order_id,)).fetchone()
 if not order: raise ValueError("أمر البيع غير موجود.")
 if order["status"] not in {"approved","partially_delivered","delivered","partially_invoiced"}:
  raise ValueError("أمر البيع غير معتمد أو لا توجد كميات جاهزة للفوترة.")
 try:
  invoice_day=date.fromisoformat(invoice_date)
  due_day=date.fromisoformat(due_date or invoice_date)
 except (TypeError,ValueError) as exc:
  raise ValueError("تاريخ الفاتورة أو الاستحقاق غير صحيح.") from exc
 if due_day<invoice_day: raise ValueError("تاريخ الاستحقاق لا يمكن أن يسبق تاريخ الفاتورة.")
 if invoice_day<date.fromisoformat(order["order_date"]): raise ValueError("تاريخ الفاتورة لا يمكن أن يسبق تاريخ أمر البيع.")
 quotation=conn.execute("SELECT * FROM quotations WHERE id=?",(order["quotation_id"],)).fetchone()
 order_lines=conn.execute("SELECT * FROM sales_order_lines WHERE sales_order_id=? ORDER BY id",(order_id,)).fetchall()
 items=[]; fulfillment=[]; consumption_plan=[]
 for line in order_lines:
  if line["product_id"]:
   ready=Decimal(str(line["delivered_qty"]))-Decimal(str(line["invoiced_qty"]))
  else:
   ready=Decimal(str(line["ordered_qty"]))-Decimal(str(line["invoiced_qty"]))
  if ready<=0: continue
  left=ready; cost_total=Decimal("0"); allocations=[]
  delivery_rows=conn.execute(
   """SELECT dl.*,dn.delivery_no,dn.delivery_date FROM delivery_note_lines dl
      JOIN delivery_notes dn ON dn.id=dl.delivery_note_id
      WHERE dl.sales_order_line_id=? AND dn.status='posted'
      ORDER BY dn.delivery_date,dn.id,dl.id""",(line["id"],)).fetchall() if line["product_id"] else []
  if delivery_rows and invoice_day<max(date.fromisoformat(dl["delivery_date"]) for dl in delivery_rows):
   raise ValueError("تاريخ الفاتورة لا يمكن أن يسبق تاريخ إذن التسليم.")
  for dl in delivery_rows:
   available=Decimal(str(dl["quantity"]))-Decimal(str(dl["invoiced_qty"]))
   if available<=0 or left<=0: continue
   take=min(available,left)
   line_qty=Decimal(str(dl["quantity"]))
   line_cost=Decimal(str(dl["total_cost"]))
   already_cost=Decimal(str(dl["invoiced_cost"] or "0"))
   remaining_cost=line_cost-already_cost
   take_cost=remaining_cost if take==available else (line_cost*take/line_qty).quantize(M,rounding=ROUND_HALF_UP)
   cost_total+=take_cost
   lot_plan=[]
   lot_rows=conn.execute("SELECT * FROM delivery_note_line_lots WHERE delivery_note_line_id=?",(dl["id"],)).fetchall()
   for lot in lot_rows:
    lot_qty=Decimal(str(lot["quantity"]))
    lot_cost=Decimal(str(lot["cost_total"]))
    lot_already_qty=Decimal(str(lot["invoiced_qty"] or "0"))
    lot_already_cost=Decimal(str(lot["invoiced_cost"] or "0"))
    lot_available=lot_qty-lot_already_qty
    allocated_qty=lot_available if take==available else (lot_qty*take/line_qty)
    allocated_cost=(lot_cost-lot_already_cost) if take==available else (lot_cost*take/line_qty).quantize(M,rounding=ROUND_HALF_UP)
    allocations.append((lot["lot_id"],allocated_qty,allocated_cost))
    lot_plan.append((lot["id"],allocated_qty,allocated_cost))
   consumption_plan.append((dl["id"],take,take_cost,lot_plan))
   left-=take
  if line["product_id"] and left>0:
   raise ValueError("الكميات المسلمة غير مرتبطة بطبقات تكلفة كافية للفوترة.")
  items.append({"product_id":line["product_id"],"description":line["description"],
   "quantity":str(ready),"unit_price":line["unit_price"],"sales_order_line_id":line["id"]})
  fulfillment.append({"cost_total":str(cost_total),"allocations":allocations})
 if not items: raise ValueError("لا توجد كميات مسلمة غير مفوترة.")
 result=post_invoice(conn,order["customer_id"],invoice_date,order["warehouse_id"],items,
  quotation["tax_rate"],created_by,dimensions=dimensions,preissued_costs=fulfillment,
  sales_order_id=order_id)
 conn.execute("UPDATE sales_invoices SET due_date=? WHERE id=?",(due_date or invoice_date,result["invoice_id"]))
 for delivery_line_id,qty,allocated_cost,lot_plan in consumption_plan:
  current=conn.execute("SELECT invoiced_qty,invoiced_cost FROM delivery_note_lines WHERE id=?",(delivery_line_id,)).fetchone()
  conn.execute("UPDATE delivery_note_lines SET invoiced_qty=?,invoiced_cost=? WHERE id=?",
   (str(Decimal(str(current["invoiced_qty"]))+qty),
    str(Decimal(str(current["invoiced_cost"]))+allocated_cost),delivery_line_id))
  for lot_id,lot_qty,lot_cost in lot_plan:
   lot_current=conn.execute("SELECT invoiced_qty,invoiced_cost FROM delivery_note_line_lots WHERE id=?",(lot_id,)).fetchone()
   conn.execute("UPDATE delivery_note_line_lots SET invoiced_qty=?,invoiced_cost=? WHERE id=?",
    (str(Decimal(str(lot_current["invoiced_qty"]))+lot_qty),
     str(Decimal(str(lot_current["invoiced_cost"]))+lot_cost),lot_id))
 remaining=conn.execute("SELECT ordered_qty,invoiced_qty FROM sales_order_lines WHERE sales_order_id=?",(order_id,)).fetchall()
 all_invoiced=all(Decimal(str(x["invoiced_qty"]))>=Decimal(str(x["ordered_qty"])) for x in remaining)
 conn.execute("UPDATE sales_orders SET status=? WHERE id=?",
  ("invoiced" if all_invoiced else ("partially_invoiced" if any(Decimal(str(x["invoiced_qty"]))>0 for x in remaining) else order["status"]),order_id))
 return result


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
    if pid:
     rule=c.execute("""SELECT unit_price FROM customer_price_rules WHERE customer_id=? AND product_id=?
       AND active=1 AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)
       AND CAST(min_qty AS REAL)<=CAST(? AS REAL) ORDER BY valid_from DESC,id DESC LIMIT 1""",
       (customer,int(pid),qdate,qdate,qty)).fetchone()
     if rule: p=Decimal(rule["unit_price"])
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
 warehouses=c.execute("SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
 c.close(); return render_template("quotation_view.html",q=q,lines=lines,warehouses=warehouses,today=date.today().isoformat())

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
@require_sales_manage
def convert(quote_id):
 c=db()
 try:
  c.execute("BEGIN IMMEDIATE")
  result=create_sales_order_from_quotation(
   c,quote_id,session["user_id"],request.form.get("warehouse_id",type=int),
   request.form.get("expected_date") or None)
  c.execute("UPDATE quotations SET status='converted_to_order' WHERE id=?",(quote_id,))
  c.commit()
  flash("تم إنشاء أمر البيع "+result["order_no"]+" وإرساله للاعتماد الداخلي.","success")
  return redirect(url_for("quotes.sales_order_view",order_id=result["sales_order_id"]))
 except (ValueError,InvalidOperation,sqlite3.Error) as e:
  c.rollback(); flash(str(e),"error")
  return redirect(url_for("quotes.view",quote_id=quote_id))
 finally: c.close()


@quotes_bp.route("/orders/<int:order_id>")
@require_sales_view
def sales_order_view(order_id):
 c=db()
 order=c.execute(
  """SELECT so.*,cu.name customer_name,cu.code customer_code,q.quote_no,q.tax_rate
     FROM sales_orders so JOIN customers cu ON cu.id=so.customer_id
     JOIN quotations q ON q.id=so.quotation_id WHERE so.id=?""",(order_id,)).fetchone()
 if not order:
  c.close(); flash("أمر البيع غير موجود.","error"); return redirect(url_for("quotes.index"))
 lines=[]
 for row in c.execute("SELECT * FROM sales_order_lines WHERE sales_order_id=? ORDER BY id",(order_id,)).fetchall():
  item=dict(row)
  item["remaining_delivery"]=max(Decimal("0"),Decimal(row["ordered_qty"])-Decimal(row["delivered_qty"]))
  item["ready_to_invoice"]=max(Decimal("0"),Decimal(row["delivered_qty"])-Decimal(row["invoiced_qty"]))
  lines.append(item)
 notes=c.execute("SELECT * FROM delivery_notes WHERE sales_order_id=? ORDER BY id DESC",(order_id,)).fetchall()
 invoices=c.execute("SELECT id,invoice_no,invoice_date,total_amount FROM sales_invoices WHERE sales_order_id=? ORDER BY id",(order_id,)).fetchall()
 warehouses=c.execute("SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
 c.close()
 return render_template("sales_order_view.html",order=order,lines=lines,notes=notes,
  invoices=invoices,warehouses=warehouses,today=date.today().isoformat())


@quotes_bp.route("/orders/<int:order_id>/approve",methods=["POST"])
@require_sales_approval
def approve_order_route(order_id):
 c=db()
 try:
  c.execute("BEGIN IMMEDIATE")
  approve_sales_order(c,order_id,session["user_id"])
  c.commit(); flash("تم اعتماد أمر البيع. يمكن الآن إنشاء أذون التسليم.","success")
 except (ValueError,sqlite3.Error) as e:
  c.rollback(); flash(str(e),"error")
 finally: c.close()
 return redirect(url_for("quotes.sales_order_view",order_id=order_id))


@quotes_bp.route("/orders/<int:order_id>/delivery-notes",methods=["POST"])
@require_sales_manage
def create_delivery_note_route(order_id):
 c=db()
 try:
  ids=request.form.getlist("sales_order_line_id")
  quantities=request.form.getlist("quantity")
  lines=[{"sales_order_line_id":line_id,"quantity":qty}
         for line_id,qty in zip(ids,quantities) if str(qty).strip()]
  c.execute("BEGIN IMMEDIATE")
  result=create_delivery_note(c,order_id,request.form.get("delivery_date",""),
   lines,session["user_id"],request.form.get("notes",""))
  c.commit(); flash("تم إنشاء إذن التسليم "+result["delivery_no"]+" كمسودة.","success")
 except (ValueError,sqlite3.Error) as e:
  c.rollback(); flash(str(e),"error")
 finally: c.close()
 return redirect(url_for("quotes.sales_order_view",order_id=order_id))


@quotes_bp.route("/delivery-notes/<int:note_id>/post",methods=["POST"])
@require_sales_manage
def post_delivery_note_route(note_id):
 c=db()
 order_id=None
 try:
  c.execute("BEGIN IMMEDIATE")
  note=c.execute("SELECT sales_order_id FROM delivery_notes WHERE id=?",(note_id,)).fetchone()
  if not note: raise ValueError("إذن التسليم غير موجود.")
  order_id=note["sales_order_id"]
  result=post_delivery_note(c,note_id,session["user_id"])
  c.commit(); flash("تم ترحيل إذن التسليم وصرف المخزون. تكلفة الصرف: "+str(result["total_cost"]),"success")
 except (ValueError,sqlite3.Error) as e:
  c.rollback(); flash(str(e),"error")
 finally: c.close()
 return redirect(url_for("quotes.sales_order_view",order_id=order_id)) if order_id else redirect(url_for("quotes.index"))


@quotes_bp.route("/orders/<int:order_id>/invoice",methods=["POST"])
@require_sales_manage
def invoice_order_route(order_id):
 c=db()
 try:
  c.execute("BEGIN IMMEDIATE")
  result=invoice_sales_order(c,order_id,request.form.get("invoice_date") or date.today().isoformat(),
   request.form.get("due_date") or date.today().isoformat(),session["user_id"],dimensions=request.form)
  c.commit(); flash("تم إصدار فاتورة الكميات المسلمة فقط: "+result["invoice_no"],"success")
  return redirect(url_for("quotes.sales_order_view",order_id=order_id))
 except (ValueError,sqlite3.Error) as e:
  c.rollback(); flash(str(e),"error")
  return redirect(url_for("quotes.sales_order_view",order_id=order_id))
 finally: c.close()

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
