from django.contrib import admin
from django.urls import include, path
from core.frontend import frontend_index

# هوية لوحة إدارة Django الخاصة بتطبيق Admrail ERP v2.
admin.site.site_header = "Admrail ERP v2"
admin.site.site_title = "Admrail ERP v2"
admin.site.index_title = "إدارة النظام"

urlpatterns = [
    path("", frontend_index, name="frontend-index"),
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
    path("api/setup/", include("companies.urls")),
    path("api/accounting/", include("accounting.urls")),
    path("api/sales/", include("customers.urls")),
    path("api/inventory/", include("inventory.urls")),
    path("api/purchases/", include("suppliers.urls")),
    path("api/purchasing/", include("purchasing.urls")),
    path("api/sales-documents/", include("sales.urls")),
    path("api/treasury/", include("treasury.urls")),
    path("api/assets/", include("assets.urls")),
    path("api/audit/", include("audit.urls")),
    path("api/approvals/", include("approvals.urls")),
    path("api/hr/", include("hr.urls")),
]
