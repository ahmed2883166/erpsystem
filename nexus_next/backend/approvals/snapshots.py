from decimal import Decimal


def invoice_discount_snapshot(invoice):
    """Return a deterministic, JSON-safe snapshot of every invoiced line."""
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
