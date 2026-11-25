from rest_framework.routers import DefaultRouter
from .api import BranchViewSet, CompanyMembershipViewSet, CompanyViewSet, CurrencyViewSet, ExchangeRateViewSet, FiscalYearViewSet

router = DefaultRouter()
router.register("currencies", CurrencyViewSet)
router.register("exchange-rates", ExchangeRateViewSet, basename="exchange-rate")
router.register("companies", CompanyViewSet, basename="company")
router.register("memberships", CompanyMembershipViewSet, basename="company-membership")
router.register("branches", BranchViewSet, basename="branch")
router.register("fiscal-years", FiscalYearViewSet, basename="fiscal-year")

urlpatterns = router.urls
