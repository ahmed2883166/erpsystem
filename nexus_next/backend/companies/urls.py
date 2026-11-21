from rest_framework.routers import DefaultRouter
from .api import BranchViewSet, CompanyViewSet, CurrencyViewSet, FiscalYearViewSet

router = DefaultRouter()
router.register("currencies", CurrencyViewSet)
router.register("companies", CompanyViewSet, basename="company")
router.register("branches", BranchViewSet, basename="branch")
router.register("fiscal-years", FiscalYearViewSet, basename="fiscal-year")

urlpatterns = router.urls
