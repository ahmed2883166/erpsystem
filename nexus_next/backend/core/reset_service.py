from django.apps import apps
from django.db import transaction


# Deliberate allowlist: master data, users, permissions, company structure,
# account chart, tax setup, product catalog, warehouses, employees and audit
# history are intentionally excluded.
RESET_MODELS = (
    ("approvals", "SalesInvoiceApproval"),
    ("approvals", "FinancialApprovalRequest"),
    ("treasury", "BankStatementLine"),
    ("purchasing", "SupplierPayment"),
    ("sales", "CustomerReceipt"),
    ("purchasing", "PurchaseInvoiceLine"),
    ("sales", "SalesInvoiceLine"),
    ("purchasing", "PurchaseOrderReceiptLine"),
    ("inventory", "StockMovementJournalLink"),
    ("inventory", "StockCostLayer"),
    ("inventory", "StockBalance"),
    ("assets", "AssetDepreciation"),
    ("hr", "PayrollResultLine"),
    ("hr", "PayrollResult"),
    ("hr", "PayrollPosting"),
    ("hr", "LoanInstallment"),
    ("hr", "AttendanceRecord"),
    ("hr", "LeaveRequest"),
    ("hr", "EmployeeSalaryHistory"),
    ("hr", "EmployeeLoan"),
    ("hr", "PayrollCycle"),
    ("purchasing", "PurchaseInvoice"),
    ("sales", "SalesInvoice"),
    ("purchasing", "PurchaseReceipt"),
    ("sales", "SalesDelivery"),
    ("purchasing", "PurchaseOrderLine"),
    ("purchasing", "PurchaseOrder"),
    ("treasury", "BankReconciliation"),
    ("assets", "FixedAsset"),
    ("treasury", "TreasuryTransaction"),
    ("inventory", "StockMovementLine"),
    ("inventory", "StockMovement"),
    ("accounting", "JournalLine"),
    ("accounting", "JournalEntry"),
)


def reset_preview():
    """تجهيز معاينة البيانات التشغيلية التي ستتأثر بإعادة الضبط دون تعديلها."""
    return {
        f"{app}.{model}": apps.get_model(app, model).objects.count()
        for app, model in RESET_MODELS
    }


@transaction.atomic
def reset_operational_data():
    """تنفيذ إعادة ضبط البيانات التشغيلية ضمن معاملة وقواعد الحماية المعتمدة."""
    # Journal reversals are self-protected; clear links before removing all entries.
    JournalEntry = apps.get_model("accounting", "JournalEntry")
    JournalEntry.objects.exclude(reversal_of=None).update(reversal_of=None)

    deleted = {}
    for app, model in RESET_MODELS:
        Model = apps.get_model(app, model)
        count = Model.objects.count()
        if count:
            Model.objects.all().delete()
        deleted[f"{app}.{model}"] = count
    return deleted
