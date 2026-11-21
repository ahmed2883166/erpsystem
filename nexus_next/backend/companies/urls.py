from rest_framework.routers import DefaultRouter
from .api import BranchViewSet, CompanyViewSet, CurrencyViewSet, FiscalYearViewSet

router = DefaultRouter()
router.register("currencies", CurrencyViewSet)
router.register("companies", CompanyViewSet)
router.register("branches", BranchViewSet)
router.register("fiscal-years", FiscalYearViewSet)

urlpatterns = router.urls
