
def invoice_discount_snapshot(invoice):
    """Return a deterministic, JSON-safe snapshot of every invoiced line."""
    prefetched = getattr(invoice, "_prefetched_objects_cache", {}).get("lines")
    if prefetched is not None:
        rows = sorted(prefetched, key=lambda line: (line.delivery_line_id, line.pk))
    else:
        rows = invoice.lines.select_related(
            "delivery_line__product", "tax_code"
        ).order_by("delivery_line_id", "pk")
    snapshot = []
    for line in rows:
        product = line.delivery_line.product
        tax_code = line.tax_code
        snapshot.append({
            "delivery_line_id": line.delivery_line_id,
            "product_id": product.pk,
            "product_code": product.code,
            "product_name": product.name_ar,
            "quantity": str(line.delivery_line.quantity),
            "unit_price": str(line.unit_price),
            "tax_code_id": tax_code.pk if tax_code else None,
            "tax_code": tax_code.code if tax_code else None,
            "tax_category": tax_code.category if tax_code else None,
            "tax_rate": str(line.tax_rate),
            "discount_percent": str(line.discount_percent),
            "discount_amount": str(line.discount_amount),
            "net_amount": str(line.net_amount),
            "tax_amount": str(line.tax_amount),
            "total_amount": str(line.total_amount),
        })
    return snapshot


def financial_document_snapshot(document_type, document):
    """Capture immutable, JSON-safe approval details for purchasing and payment documents."""
    if document_type == "purchase_order":
        return {
            "document_type": document_type,
            "document_number": document.order_number,
            "date": document.order_date.isoformat(),
            "supplier_id": document.supplier_id,
            "supplier_name": document.supplier.name_ar,
            "warehouse_id": document.warehouse_id,
            "warehouse_name": document.warehouse.name_ar,
            "expected_date": document.expected_date.isoformat() if document.expected_date else None,
            "grand_total": str(document.grand_total),
            "lines": [
                {
                    "product_id": line.product_id,
                    "product_code": line.product.code,
                    "product_name": line.product.name_ar,
                    "quantity": str(line.quantity),
                    "unit_price": str(line.unit_price),
                    "tax_rate": str(line.tax_rate),
                }
                for line in document.lines.select_related("product").order_by("pk")
            ],
        }
    if document_type == "purchase_invoice":
        return {
            "document_type": document_type,
            "document_number": document.invoice_number,
            "date": document.invoice_date.isoformat(),
            "supplier_id": document.supplier_id,
            "supplier_name": document.supplier.name_ar,
            "receipt_id": document.receipt_id,
            "net_total": str(document.net_total),
            "tax_total": str(document.tax_total),
            "grand_total": str(document.grand_total),
            "lines": [
                {
                    "receipt_line_id": line.receipt_line_id,
                    "product_id": line.receipt_line.product_id,
                    "product_code": line.receipt_line.product.code,
                    "product_name": line.receipt_line.product.name_ar,
                    "quantity": str(line.billed_quantity),
                    "receipt_quantity": str(line.receipt_line.quantity),
                    "unit_price": str(line.unit_price),
                    "tax_code": line.tax_code.code if line.tax_code_id else None,
                    "tax_rate": str(line.tax_rate),
                    "net_amount": str(line.net_amount),
                    "tax_amount": str(line.tax_amount),
                    "total_amount": str(line.total_amount),
                }
                for line in document.lines.select_related(
                    "receipt_line__product", "tax_code"
                ).order_by("pk")
            ],
        }
    if document_type == "supplier_payment":
        return {
            "document_type": document_type,
            "document_number": document.payment_number,
            "date": document.payment_date.isoformat(),
            "supplier_id": document.supplier_id,
            "supplier_name": document.supplier.name_ar,
            "invoice_id": document.invoice_id,
            "invoice_number": document.invoice.invoice_number,
            "method": document.method,
            "amount": str(document.amount),
            "reference": document.reference,
        }
    if document_type == "treasury_payment":
        return {
            "document_type": document_type,
            "document_number": document.document_number,
            "date": document.transaction_date.isoformat(),
            "branch_id": document.branch_id,
            "source_account_id": document.source_account_id,
            "source_account": document.source_account.name_ar,
            "counter_account_id": document.counter_account_id,
            "counter_account": document.counter_account.name_ar,
            "amount": str(document.amount),
            "description": document.description,
            "reference": document.reference,
            "cash_flow_category": document.cash_flow_category,
        }

    if document_type == "asset_purchase_invoice":
        return {
            "document_type": document_type,
            "document_number": document.invoice_number,
            "date": document.invoice_date.isoformat(),
            "supplier_id": document.supplier_id,
            "supplier_name": document.supplier.name_ar,
            "asset_id": document.asset_id,
            "asset_code": document.asset.code,
            "asset_name": document.asset.name_ar,
            "net_amount": str(document.net_amount),
            "tax_code_id": document.tax_code_id,
            "tax_code": document.tax_code.code if document.tax_code_id else None,
            "tax_amount": str(document.tax_amount),
            "recoverable_tax_amount": str(document.recoverable_tax_amount),
            "nonrecoverable_tax_amount": str(document.nonrecoverable_tax_amount),
            "grand_total": str(document.grand_total),
        }
    if document_type == "asset_supplier_payment":
        return {
            "document_type": document_type,
            "document_number": document.payment_number,
            "date": document.payment_date.isoformat(),
            "supplier_id": document.supplier_id,
            "supplier_name": document.supplier.name_ar,
            "invoice_id": document.invoice_id,
            "invoice_number": document.invoice.invoice_number,
            "payment_account_id": document.payment_account_id,
            "payment_account": document.payment_account.name_ar,
            "amount": str(document.amount),
            "reference": document.reference,
        }

    raise ValueError("Unsupported financial approval document type")
