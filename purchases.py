"""NEXUS ERP purchasing v1: suppliers, purchase orders, GRNI receipts, bills and payments.
All posting is transactional and idempotent. Tax is posted only when an explicit input-tax account exists.
"""
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from datetime import date, datetime
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from accounting_core import post_journal

purchase_bp = Blueprint("purchase", __name__, url_prefix="/purchases")
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"
M = Decimal("0.01")

def db():
    c=sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=20000")
    return c

def money(v):
    return Decimal(str(v)).quantize(M, rounding=ROUND_HALF_UP)

def init_purchase_schema():
    c=db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS suppliers(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      tax_number TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
      email TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS purchase_orders(
      id INTEGER PRIMARY KEY, po_no TEXT NOT NULL UNIQUE, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
      order_date TEXT NOT NULL, expected_date TEXT, status TEXT NOT NULL DEFAULT 'draft',
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS purchase_order_lines(
      id INTEGER PRIMARY KEY, po_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES stock_products(id), quantity TEXT NOT NULL,
      unit_price TEXT NOT NULL, received_qty TEXT NOT NULL DEFAULT '0'
    );
    CREATE TABLE IF NOT EXISTS goods_receipts(
      id INTEGER PRIMARY KEY, receipt_no TEXT NOT NULL UNIQUE, po_id INTEGER REFERENCES purchase_orders(id),
      supplier_id INTEGER NOT NULL REFERENCES suppliers(id), warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      receipt_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'posted',
      journal_entry_id INTEGER UNIQUE, total_value TEXT NOT NULL DEFAULT '0',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS goods_receipt_lines(
      id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
      po_line_id INTEGER REFERENCES purchase_order_lines(id), product_id INTEGER NOT NULL REFERENCES stock_products(id),
      quantity TEXT NOT NULL, unit_cost TEXT NOT NULL, total_cost TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS supplier_bills(
      id INTEGER PRIMARY KEY, bill_no TEXT NOT NULL UNIQUE, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
      bill_date TEXT NOT NULL, supplier_invoice_no TEXT NOT NULL, subtotal TEXT NOT NULL,
      tax_rate TEXT NOT NULL DEFAULT '0', tax_amount TEXT NOT NULL DEFAULT '0',
      total_amount TEXT NOT NULL, paid_amount TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'posted', journal_entry_id INTEGER UNIQUE,
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS supplier_bill_receipts(
      bill_id INTEGER NOT NULL REFERENCES supplier_bills(id), receipt_id INTEGER NOT NULL REFERENCES goods_receipts(id),
      matched_amount TEXT NOT NULL, PRIMARY KEY(bill_id,receipt_id)
    );
    CREATE TABLE IF NOT EXISTS supplier_payments(
      id INTEGER PRIMARY KEY, payment_no TEXT NOT NULL UNIQUE, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
      payment_date TEXT NOT NULL, amount TEXT NOT NULL, cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER UNIQUE, reference TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS supplier_payment_allocations(
      payment_id INTEGER NOT NULL REFERENCES supplier_payments(id), bill_id INTEGER NOT NULL REFERENCES supplier_bills(id),
      amount TEXT NOT NULL, PRIMARY KEY(payment_id,bill_id)
    );
    CREATE INDEX IF NOT EXISTS idx_purchase_bills_supplier ON supplier_bills(supplier_id,bill_date);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_bill_invoice_no
      ON supplier_bills(supplier_id, lower(trim(supplier_invoice_no)));
    CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_bill_receipt_once
      ON supplier_bill_receipts(receipt_id);
    CREATE INDEX IF NOT EXISTS idx_purchase_receipts_supplier ON goods_receipts(supplier_id,receipt_date);
    """)
    c.close()

def require(permission):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a,**kw):
            if "user_id" not in session: return redirect(url_for("login"))
            allowed={"purchases.manage":{"admin","accountant"},"purchases.view":{"admin","accountant","viewer"}}
            if session.get("role") not in allowed.get(permission,set()):
                flash("لا تملك الصلاحية المطلوبة.","error"); return redirect(url_for("dashboard"))
            return fn(*a,**kw)
        return wrapped
    return deco

def account(c, code, typ=None):
    row=c.execute("SELECT id,is_group,active,account_type FROM accounts WHERE code=?",(code,)).fetchone()
    if not row or row["is_group"] or not row["active"] or (typ and row["account_type"]!=typ):
        raise ValueError(f"حساب الربط {code} غير موجود أو غير نشط/تفصيلي. راجع دليل الحسابات.")
    return row["id"]

def create_journal(c, d, ref, desc, lines, user):
    return post_journal(c,d,ref,desc,lines,user)

def ensure_detail_account(c, code, name, typ, preferred_parent):
    found=c.execute("SELECT id,is_group,active,account_type FROM accounts WHERE code=?",(code,)).fetchone()
    if found:
        if found["is_group"] or not found["active"] or found["account_type"]!=typ:
            raise ValueError("حساب الربط موجود لكنه غير صالح للترحيل: "+code)
        return found["id"]
    parent=c.execute("SELECT id,is_group,active,account_type FROM accounts WHERE code=?",(preferred_parent,)).fetchone()
    if not parent or not parent["is_group"] or not parent["active"] or parent["account_type"]!=typ:
        parent=c.execute("SELECT id FROM accounts WHERE account_type=? AND is_group=1 AND active=1 ORDER BY code LIMIT 1",(typ,)).fetchone()
    parent_id=parent["id"] if parent else None
    cur=c.execute("INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group,active) VALUES(?,?,?,?,0,1)",
                  (code,name,typ,parent_id))
    return cur.lastrowid

def supplier_ap_account(c):
    row=c.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
    activity=row["setting_value"] if row else "commercial"
    if activity=="medical":
        return ensure_detail_account(c,"2150","الموردون - مستلزمات طبية","liability","2100")
    if activity=="commercial":
        return account(c,"2110","liability")
    if activity=="service":
        return account(c,"2100","liability")
    if activity in {"restaurant","industrial"}:
        return account(c,"2100","liability")
    raise ValueError("لا يوجد حساب موردين مضبوط للنشاط الحالي.")

def activity_inventory_code(c):
    row=c.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()
    activity=row[0] if row else "commercial"
    return {"commercial":"1150","industrial":"1130","medical":"1150","restaurant":"1140"}.get(activity)

@purchase_bp.route("/")
@require("purchases.view")
def index():
    c=db()
    suppliers=c.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    orders=c.execute("""SELECT p.*,s.name supplier_name FROM purchase_orders p JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC LIMIT 20""").fetchall()
    receipts=c.execute("""SELECT r.*,s.name supplier_name FROM goods_receipts r JOIN suppliers s ON s.id=r.supplier_id ORDER BY r.id DESC LIMIT 20""").fetchall()
    bills=c.execute("""SELECT b.*,s.name supplier_name FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id ORDER BY b.id DESC LIMIT 20""").fetchall()
    payments=c.execute("""SELECT p.*,s.name supplier_name FROM supplier_payments p JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC LIMIT 20""").fetchall()
    c.close()
    return render_template("purchases.html",suppliers=suppliers,orders=orders,receipts=receipts,bills=bills,payments=payments,today=date.today().isoformat())

@purchase_bp.route("/suppliers",methods=["GET","POST"])
@require("purchases.manage")
def suppliers():
    c=db()
    if request.method=="POST":
        try:
            c.execute("INSERT INTO suppliers(code,name,tax_number,phone,email,address) VALUES(?,?,?,?,?,?)",
              (request.form["code"].strip(),request.form["name"].strip(),request.form.get("tax_number","").strip(),request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("address","").strip()))
            c.commit(); flash("تم حفظ المورد.","success")
        except sqlite3.IntegrityError: c.rollback(); flash("كود المورد مستخدم بالفعل.","error")
    rows=c.execute("""SELECT s.*,
        COALESCE((SELECT SUM(CAST(b.total_amount AS REAL)-CAST(b.paid_amount AS REAL))
                  FROM supplier_bills b WHERE b.supplier_id=s.id AND b.status='posted'),0) outstanding
        FROM suppliers s ORDER BY s.name""").fetchall(); c.close()
    return render_template("purchase_suppliers.html",rows=rows)


@purchase_bp.route("/suppliers/<int:supplier_id>/edit",methods=["GET","POST"])
@require("purchases.manage")
def edit_supplier(supplier_id):
    c=db(); supplier=c.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
    if not supplier:
        c.close(); flash("المورد غير موجود.","error"); return redirect(url_for("purchase.suppliers"))
    used=any(c.execute(q,(supplier_id,)).fetchone() for q in [
      "SELECT 1 FROM purchase_orders WHERE supplier_id=? LIMIT 1",
      "SELECT 1 FROM goods_receipts WHERE supplier_id=? LIMIT 1",
      "SELECT 1 FROM supplier_bills WHERE supplier_id=? LIMIT 1",
      "SELECT 1 FROM supplier_payments WHERE supplier_id=? LIMIT 1"])
    if request.method=="POST":
        try:
            c.execute("UPDATE suppliers SET code=?,name=?,tax_number=?,phone=?,email=?,address=? WHERE id=?",
              (request.form["code"].strip(),request.form["name"].strip(),request.form.get("tax_number","").strip(),request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("address","").strip(),supplier_id))
            c.commit(); c.close(); flash("تم تعديل بيانات المورد.","success"); return redirect(url_for("purchase.suppliers"))
        except sqlite3.IntegrityError: c.rollback(); flash("كود المورد مستخدم بالفعل.","error")
    c.close(); return render_template("purchase_supplier_edit.html",supplier=supplier,used=used)


@purchase_bp.route("/suppliers/<int:supplier_id>/delete",methods=["POST"])
@require("purchases.manage")
def delete_supplier(supplier_id):
    c=db(); supplier=c.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
    if not supplier: flash("المورد غير موجود.","error")
    else:
        checks=[
          ("SELECT 1 FROM purchase_orders WHERE supplier_id=? LIMIT 1","أوامر الشراء"),
          ("SELECT 1 FROM goods_receipts WHERE supplier_id=? LIMIT 1","استلامات المشتريات"),
          ("SELECT 1 FROM supplier_bills WHERE supplier_id=? LIMIT 1","فواتير الموردين"),
          ("SELECT 1 FROM supplier_payments WHERE supplier_id=? LIMIT 1","مدفوعات الموردين")]
        used=next((label for q,label in checks if c.execute(q,(supplier_id,)).fetchone()),None)
        if used: flash("لا يمكن حذف المورد لوجود "+used+" مرتبطة به.","error")
        else:
            try: c.execute("DELETE FROM suppliers WHERE id=?",(supplier_id,)); c.commit(); flash("تم حذف المورد.","success")
            except sqlite3.IntegrityError: c.rollback(); flash("لا يمكن حذف المورد لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("purchase.suppliers"))

@purchase_bp.route("/orders/new",methods=["GET","POST"])
@require("purchases.manage")
def new_order():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); product=int(request.form["product_id"])
            qty=Decimal(request.form["quantity"]); price=money(request.form["unit_price"])
            if qty<=0 or price<0: raise ValueError("الكمية والسعر غير صحيحين.")
            c.execute("BEGIN IMMEDIATE")
            no="PO-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            cur=c.execute("INSERT INTO purchase_orders(po_no,supplier_id,order_date,expected_date,notes,created_by) VALUES(?,?,?,?,?,?)",
              (no,supplier,request.form["order_date"],request.form.get("expected_date") or None,request.form.get("notes",""),session["user_id"]))
            c.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",(cur.lastrowid,product,str(qty),str(price)))
            c.commit(); flash(f"تم إنشاء أمر الشراء {no}.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    products=c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    c.close(); return render_template("purchase_order_new.html",suppliers=su,products=products,today=date.today().isoformat())


@purchase_bp.route("/orders/<int:order_id>/edit",methods=["GET","POST"])
@require("purchases.manage")
def edit_order(order_id):
    c=db(); order=c.execute("""SELECT p.*,l.id line_id,l.product_id,l.quantity,l.unit_price
      FROM purchase_orders p LEFT JOIN purchase_order_lines l ON l.po_id=p.id WHERE p.id=?""",(order_id,)).fetchone()
    if not order:
        c.close(); flash("أمر الشراء غير موجود.","error"); return redirect(url_for("purchase.index"))
    received=c.execute("SELECT 1 FROM goods_receipts WHERE po_id=? LIMIT 1",(order_id,)).fetchone() is not None
    if received or order["status"]!="draft":
        c.close(); flash("لا يمكن تعديل أمر شراء تم استلامه أو اعتماده.","error"); return redirect(url_for("purchase.index"))
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); product=int(request.form["product_id"])
            qty=Decimal(request.form["quantity"]); price=money(request.form["unit_price"])
            if qty<=0 or price<0: raise ValueError("الكمية والسعر غير صحيحين.")
            c.execute("UPDATE purchase_orders SET supplier_id=?,order_date=?,expected_date=?,notes=? WHERE id=?",
              (supplier,request.form["order_date"],request.form.get("expected_date") or None,request.form.get("notes",""),order_id))
            c.execute("UPDATE purchase_order_lines SET product_id=?,quantity=?,unit_price=? WHERE id=?",
              (product,str(qty),str(price),order["line_id"]))
            c.commit(); c.close(); flash("تم تعديل أمر الشراء.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
    suppliers=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    products=c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    c.close(); return render_template("purchase_order_edit.html",order=order,suppliers=suppliers,products=products)


@purchase_bp.route("/orders/<int:order_id>/delete",methods=["POST"])
@require("purchases.manage")
def delete_order(order_id):
    c=db(); order=c.execute("SELECT * FROM purchase_orders WHERE id=?",(order_id,)).fetchone()
    if not order: flash("أمر الشراء غير موجود.","error")
    elif order["status"]!="draft": flash("لا يمكن حذف أمر شراء معتمد.","error")
    elif c.execute("SELECT 1 FROM goods_receipts WHERE po_id=? LIMIT 1",(order_id,)).fetchone():
        flash("لا يمكن حذف أمر الشراء لوجود استلام مرتبط به.","error")
    else:
        try: c.execute("DELETE FROM purchase_orders WHERE id=?",(order_id,)); c.commit(); flash("تم حذف أمر الشراء غير المستلم.","success")
        except sqlite3.IntegrityError: c.rollback(); flash("لا يمكن حذف أمر الشراء لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("purchase.index"))

@purchase_bp.route("/receipts/new",methods=["GET","POST"])
@require("purchases.manage")
def new_receipt():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); product=int(request.form["product_id"]); wh=int(request.form["warehouse_id"])
            po_id=int(request.form["po_id"]) if request.form.get("po_id") else None
            qty=Decimal(request.form["quantity"]); unit=money(request.form["unit_cost"]); total=money(qty*unit)
            if qty<=0 or unit<0: raise ValueError("الكمية أو التكلفة غير صحيحة.")
            c.execute("BEGIN IMMEDIATE")
            po_line_id=None
            if po_id:
                po=c.execute("SELECT * FROM purchase_orders WHERE id=? AND supplier_id=? AND status IN ('draft','partially_received')",
                             (po_id,supplier)).fetchone()
                if not po: raise ValueError("أمر الشراء غير موجود أو لا يتبع المورد أو مغلق.")
                po_lines=c.execute("SELECT * FROM purchase_order_lines WHERE po_id=? AND product_id=? ORDER BY id",
                                   (po_id,product)).fetchall()
                if len(po_lines)!=1: raise ValueError("الصنف يجب أن يقابل سطرًا واحدًا فقط في أمر الشراء.")
                po_line=po_lines[0]
                outstanding=Decimal(po_line["quantity"])-Decimal(po_line["received_qty"])
                if qty>outstanding: raise ValueError("كمية الاستلام أكبر من الكمية المتبقية في أمر الشراء.")
                po_line_id=po_line["id"]
            invcode=activity_inventory_code(c)
            if not invcode: raise ValueError("النشاط الحالي لا يملك حساب مخزون تلقائيًا؛ اضبط الربط أولًا.")
            inv=account(c,invcode,"asset")
            # GRNI must be explicitly configured as a detailed liability account.
            grni=ensure_detail_account(c,"2190","بضاعة مستلمة غير مفوترة GRNI","liability","2000")
            no="GRN-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            cur=c.execute("INSERT INTO goods_receipts(receipt_no,po_id,supplier_id,warehouse_id,receipt_date,total_value,created_by) VALUES(?,?,?,?,?,?,?)",
              (no,po_id,supplier,wh,request.form["receipt_date"],str(total),session["user_id"]))
            rid=cur.lastrowid
            c.execute("INSERT INTO goods_receipt_lines(receipt_id,po_line_id,product_id,quantity,unit_cost,total_cost) VALUES(?,?,?,?,?,?)",
                      (rid,po_line_id,product,str(qty),str(unit),str(total)))
            if po_id:
                received_total=Decimal(po_line["received_qty"])+qty
                new_status="received" if received_total==Decimal(po_line["quantity"]) else "partially_received"
                c.execute("UPDATE purchase_order_lines SET received_qty=? WHERE id=?",(str(received_total),po_line_id))
                c.execute("UPDATE purchase_orders SET status=? WHERE id=?",(new_status,po_id))
            # Stock ledger + valuation layer are updated in the same DB transaction.
            move_no="PUR-"+no
            movement=c.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,accounting_status)
              VALUES(?,'receipt',?,?,?,?,?,?,?,?, 'pending_document')""",
              (move_no,product,wh,str(qty),str(unit),str(total),request.form["receipt_date"],"استلام مشتريات "+no,session["user_id"]))
            mid=movement.lastrowid
            c.execute("INSERT INTO stock_valuation_layers(product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,unit_cost) VALUES(?,?,?,?,?,?)",
              (product,wh,mid,str(qty),str(qty),str(unit)))
            eid=create_journal(c,request.form["receipt_date"],no,"استلام مشتريات غير مفوتر "+no,[(inv,total,0,"إثبات مخزون مستلم"),(grni,0,total,"بضاعة مستلمة غير مفوترة")],session["user_id"])
            c.execute("UPDATE goods_receipts SET journal_entry_id=? WHERE id=?",(eid,rid))
            c.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?",(eid,mid))
            c.commit(); flash(f"تم الاستلام وترحيل قيد GRNI رقم {no}.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    wh=c.execute("SELECT * FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    pr=c.execute("SELECT * FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    orders=c.execute("SELECT id,po_no,supplier_id,status FROM purchase_orders WHERE status IN ('draft','partially_received') ORDER BY id DESC").fetchall()
    c.close(); return render_template("purchase_receipt_new.html",suppliers=su,warehouses=wh,products=pr,orders=orders,today=date.today().isoformat())

@purchase_bp.route("/bills/new",methods=["GET","POST"])
@require("purchases.manage")
def new_bill():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); subtotal=money(request.form["subtotal"]); rate=Decimal(request.form.get("tax_rate","0"))
            if subtotal<=0 or rate<0 or rate>100: raise ValueError("قيمة الفاتورة أو الضريبة غير صحيحة.")
            tax=money(subtotal*rate/Decimal("100")); total=subtotal+tax
            receipts=[int(x) for x in request.form.getlist("receipt_id")]
            supplier_invoice_no=request.form["supplier_invoice_no"].strip()
            if not supplier_invoice_no:
                raise ValueError("رقم فاتورة المورد مطلوب.")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM supplier_bills WHERE supplier_id=? AND lower(trim(supplier_invoice_no))=lower(trim(?)) LIMIT 1",
                         (supplier,supplier_invoice_no)).fetchone():
                raise ValueError("رقم فاتورة المورد مسجل بالفعل لهذا المورد.")
            invcode=activity_inventory_code(c); inv=account(c,invcode,"asset")
            ap=supplier_ap_account(c)
            lines=[]; remaining=subtotal
            for rid in receipts:
                r=c.execute("SELECT * FROM goods_receipts WHERE id=? AND supplier_id=?",(rid,supplier)).fetchone()
                if not r: raise ValueError("إيصال الاستلام غير تابع لهذا المورد.")
                if c.execute("SELECT 1 FROM supplier_bill_receipts WHERE receipt_id=? LIMIT 1",(rid,)).fetchone():
                    raise ValueError("إيصال الاستلام مرتبط بفاتورة مورد أخرى بالفعل.")
                allocated=money(r["total_value"])
                if allocated>remaining: raise ValueError("إجمالي إيصالات الاستلام أكبر من قيمة الفاتورة.")
                lines.append((rid,allocated)); remaining-=allocated
            if receipts and remaining!=0: raise ValueError("قيمة الفاتورة يجب أن تطابق إجمالي إيصالات GRNI المحددة.")
            if tax:
                # No guessed tax posting: explicit input VAT account is mandatory.
                vat=ensure_detail_account(c,"1165","ضريبة مدخلات قابلة للخصم","asset","1100")
                lines.append(("VAT",tax))
            no="BILL-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            cur=c.execute("INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,supplier_invoice_no,subtotal,tax_rate,tax_amount,total_amount,created_by) VALUES(?,?,?,?,?,?,?,?,?)",
              (no,supplier,request.form["bill_date"],supplier_invoice_no,str(subtotal),str(rate),str(tax),str(total),session["user_id"]))
            bid=cur.lastrowid
            due_date=request.form.get("due_date") or request.form["bill_date"]
            if due_date < request.form["bill_date"]: raise ValueError("تاريخ استحقاق المورد لا يمكن أن يسبق تاريخ الفاتورة.")
            c.execute("UPDATE supplier_bills SET due_date=? WHERE id=?",(due_date,bid))
            jlines=[]
            if receipts:
                for rid,amt in lines:
                    if rid=="VAT": continue
                    grni=account(c,"2190","liability")
                    jlines.extend([(grni,amt,0,"تسوية GRNI"),(ap,0,amt,"إثبات المورد")])
                    c.execute("INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",(bid,rid,str(amt)))
                if tax: jlines.append((vat,tax,0,"ضريبة مدخلات حسب الفاتورة"))
                jlines.append((ap,0,tax,"ضريبة ضمن إجمالي المورد"))
            else:
                raise ValueError("لا يمكن ترحيل فاتورة مشتريات مخزنية دون إيصال استلام. سجّل الاستلام أولًا من شاشة استلام المشتريات ثم اربطه بالفاتورة.")
            eid=create_journal(c,request.form["bill_date"],no,"فاتورة مورد "+request.form["supplier_invoice_no"],jlines,session["user_id"])
            c.execute("UPDATE supplier_bills SET journal_entry_id=? WHERE id=?",(eid,bid))
            c.commit(); flash(f"تم ترحيل فاتورة المورد {no}.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    receipts=c.execute("""SELECT r.*,s.name supplier_name FROM goods_receipts r JOIN suppliers s ON s.id=r.supplier_id
      WHERE r.status='posted' AND NOT EXISTS(SELECT 1 FROM supplier_bill_receipts x WHERE x.receipt_id=r.id) ORDER BY r.id""").fetchall()
    c.close(); return render_template("purchase_bill_new.html",suppliers=su,receipts=receipts,today=date.today().isoformat())

@purchase_bp.route("/payments/new",methods=["GET","POST"])
@require("purchases.manage")
def new_payment():
    c=db()
    if request.method=="POST":
        try:
            supplier=int(request.form["supplier_id"]); amount=money(request.form["amount"]); cash=int(request.form["cash_account_id"])
            if amount<=0: raise ValueError("قيمة السداد يجب أن تكون أكبر من صفر.")
            cash_row=c.execute("SELECT code,name_ar,account_type,is_group,active FROM accounts WHERE id=?",(cash,)).fetchone()
            cash_name=(cash_row["name_ar"] or "").lower() if cash_row else ""
            cash_codes={"1000","1110","1120","1121","1122"}
            cash_words=("صندوق","بنك","بنوك","مصرف","نقد","cash","bank")
            if (not cash_row or cash_row["account_type"]!="asset" or cash_row["is_group"] or not cash_row["active"]
                or (cash_row["code"] not in cash_codes and not any(word in cash_name for word in cash_words))):
                raise ValueError("حساب السداد يجب أن يكون حساب صندوق أو بنك تفصيليًا ونشطًا، وليس حساب إيراد أو مصروف أو أصل آخر.")
            ap=supplier_ap_account(c); c.execute("BEGIN IMMEDIATE")
            no="PAY-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
            eid=create_journal(c,request.form["payment_date"],no,"سداد مورد",[(ap,amount,0,"تخفيض مديونية المورد"),(cash,0,amount,"سداد نقدي/بنكي")],session["user_id"])
            cur=c.execute("INSERT INTO supplier_payments(payment_no,supplier_id,payment_date,amount,cash_account_id,journal_entry_id,reference,created_by) VALUES(?,?,?,?,?,?,?,?)",
              (no,supplier,request.form["payment_date"],str(amount),cash,eid,request.form.get("reference",""),session["user_id"]))
            payment_id=cur.lastrowid; remaining=amount
            bills=c.execute("""SELECT * FROM supplier_bills WHERE supplier_id=? AND status='posted' AND CAST(total_amount AS REAL)>CAST(paid_amount AS REAL) ORDER BY bill_date,id""",(supplier,)).fetchall()
            for b in bills:
                due=money(Decimal(b["total_amount"])-Decimal(b["paid_amount"])); alloc=min(due,remaining)
                if alloc>0:
                    c.execute("INSERT INTO supplier_payment_allocations(payment_id,bill_id,amount) VALUES(?,?,?)",(payment_id,b["id"],str(alloc)))
                    c.execute("UPDATE supplier_bills SET paid_amount=? WHERE id=?",(str(money(Decimal(b["paid_amount"])+alloc)),b["id"]))
                    remaining-=alloc
                if remaining<=0: break
            if remaining>0: raise ValueError("المبلغ أكبر من مديونية الفواتير المفتوحة؛ لا يتم تسجيل دفعة مقدمة تلقائيًا.")
            c.commit(); flash(f"تم تسجيل السداد {no} وتوزيعه على أقدم الفواتير.","success"); return redirect(url_for("purchase.index"))
        except (ValueError,InvalidOperation,KeyError,sqlite3.Error) as e: c.rollback(); flash(str(e),"error")
    su=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    cash=c.execute("""SELECT * FROM accounts WHERE active=1 AND is_group=0 AND account_type='asset'
      AND (lower(name_ar) LIKE '%صندوق%' OR lower(name_ar) LIKE '%بنك%' OR lower(name_ar) LIKE '%نقد%' OR lower(name_ar) LIKE '%نقاط البيع%'
           OR code IN ('1000','1110','1120')) ORDER BY code""").fetchall()
    c.close(); return render_template("purchase_payment_new.html",suppliers=su,cash_accounts=cash,today=date.today().isoformat())
