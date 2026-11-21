from django.contrib import admin
from django.urls import include, path

urlpatterns = [
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
]
