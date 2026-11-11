import unittest
from pathlib import Path

from flask import Flask, Blueprint, render_template


ROOT = Path(__file__).resolve().parents[1] / "templates"


class RealTemplatePageTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(ROOT))
        self.app.secret_key = "test"
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        self.app.jinja_env.filters["money"] = lambda value: f"{float(value or 0):,.2f}"
        self.app.context_processor(lambda: {
            "permissions": set(),
            "license_info": {"status": "demo"},
        })
        self.app.add_url_rule("/change-password", endpoint="change_password", view_func=lambda: "")
        self.app.add_url_rule("/logout", endpoint="logout", view_func=lambda: "")
        sales = Blueprint("sales", __name__)
        purchases = Blueprint("purchase", __name__)
        sales.add_url_rule("/", endpoint="index", view_func=lambda: "")
        purchases.add_url_rule("/", endpoint="index", view_func=lambda: "")
        self.app.register_blueprint(sales, url_prefix="/sales")
        self.app.register_blueprint(purchases, url_prefix="/purchases")

        @self.app.get("/test/sales-invoice")
        def sales_invoice():
            return render_template("sales_invoice_print.html",
                invoice={
                    "invoice_no": "SI-TEST", "invoice_date": "2026-11-10",
                    "due_date": "2026-11-20", "customer_name": "عميل اختبار",
                    "customer_code": "C-1", "customer_tax_number": "",
                    "subtotal": "100", "tax_amount": "14", "total_amount": "114",
                    "paid_amount": "20", "credited_amount": "0",
                    "journal_entry_id": 1, "status": "posted",
                },
                lines=[{"sku": "P-1", "product_name": "صنف اختبار",
                        "description": "صنف اختبار", "quantity": "2",
                        "unit_price": "50", "line_total": "100"}],
                settings={"company_name": "NEXUS ERP", "base_currency": "EGP"})

        @self.app.get("/test/purchase-bill")
        def purchase_bill():
            return render_template("purchase_bill_print.html",
                bill={
                    "bill_no": "PB-TEST", "bill_date": "2026-11-10",
                    "due_date": "2026-11-20", "supplier_name": "مورد اختبار",
                    "supplier_code": "S-1", "supplier_tax_number": "",
                    "supplier_invoice_no": "SUP-100", "subtotal": "100",
                    "tax_amount": "14", "total_amount": "114", "paid_amount": "0",
                    "credited_amount": "0", "journal_entry_id": 2, "status": "posted",
                },
                receipts=[], journal_lines=[],
                settings={"company_name": "NEXUS ERP", "base_currency": "EGP"})

        @self.app.get("/test/users")
        def users_page():
            return render_template("users.html", users=[],
                role_permissions={"admin": ["users.manage"]},
                default_permissions=["users.manage"],
                all_permissions=["users.manage"],
                permission_labels={"users.manage": "إدارة المستخدمين"})

        self.client = self.app.test_client()

    def _login(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["full_name"] = "مدير الاختبار"
            session["role"] = "admin"

    def test_sales_invoice_print_template_renders(self):
        self._login()
        response = self.client.get("/test/sales-invoice")
        self.assertEqual(response.status_code, 200)
        self.assertIn("SI-TEST", response.get_data(as_text=True))
        self.assertIn("114.00", response.get_data(as_text=True))

    def test_purchase_bill_print_template_renders(self):
        self._login()
        response = self.client.get("/test/purchase-bill")
        self.assertEqual(response.status_code, 200)
        self.assertIn("PB-TEST", response.get_data(as_text=True))
        self.assertIn("مورد اختبار", response.get_data(as_text=True))

    def test_user_management_template_renders(self):
        self._login()
        response = self.client.get("/test/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn("المستخدمون والصلاحيات", response.get_data(as_text=True))
        self.assertIn("حفظ المستخدم والصلاحيات", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
