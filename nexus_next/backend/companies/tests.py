from datetime import date
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.contrib.auth import get_user_model
from .access import accessible_branches, accessible_companies
from .models import Branch, Company, Currency, FiscalYear

class CompanyFoundationTests(TestCase):
    def setUp(self):
        self.egp = Currency.objects.create(code="EGP", name_ar="جنيه مصري", symbol="ج.م")
        self.company = Company.objects.create(
            code="NEX-EG", name_ar="شركة تجريبية", country=Company.Country.EGYPT,
            activity=Company.Activity.COMMERCIAL, base_currency=self.egp,
        )

    def test_company_supports_only_configured_countries_and_activities(self):
        self.assertEqual(self.company.country, "EG")
        self.assertEqual(len(Company.Country.choices), 2)
        self.assertNotIn("manufacturing", dict(Company.Activity.choices))

    def test_branch_code_is_unique_per_company(self):
        Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.objects.create(company=self.company, code="MAIN", name_ar="مكرر")

    def test_only_one_head_office_per_company(self):
        Branch.objects.create(company=self.company, code="HQ", name_ar="المركز", is_head_office=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.objects.create(company=self.company, code="HQ2", name_ar="مركز آخر", is_head_office=True)

    def test_fiscal_year_rejects_end_before_start(self):
        year = FiscalYear(
            company=self.company, name="FY 2026",
            start_date=date(2026, 12, 31), end_date=date(2026, 1, 1),
        )
        with self.assertRaises(ValidationError):
            year.full_clean()

    def test_superuser_has_global_active_company_and_branch_visibility(self):
        other = Company.objects.create(
            code="NEX-SA", name_ar="شركة ثانية", country=Company.Country.SAUDI_ARABIA,
            activity=Company.Activity.SERVICE, base_currency=self.egp,
        )
        active_branch = Branch.objects.create(company=other, code="ACTIVE", name_ar="فرع نشط")
        Branch.objects.create(company=other, code="INACTIVE", name_ar="فرع موقوف", is_active=False)
        root = get_user_model().objects.create_superuser(
            username="global-operator", email="global@example.test", password="long-test-password"
        )
        self.assertSetEqual(
            set(accessible_companies(root).values_list("pk", flat=True)),
            set(Company.objects.filter(is_active=True).values_list("pk", flat=True)),
        )
        self.assertIn(active_branch.pk, set(accessible_branches(root).values_list("pk", flat=True)))
        self.assertFalse(accessible_branches(root).filter(is_active=False).exists())


from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

class CompanySetupApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reader", password="test-password")
        Currency.objects.create(code="SAR", name_ar="ريال سعودي")

    def test_setup_endpoints_require_authentication(self):
        response = self.client.get("/api/setup/currencies/")
        self.assertEqual(response.status_code, 403)

    def test_authenticated_user_can_read_currencies(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/currencies/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["code"], "SAR")

    def test_non_admin_cannot_create_currency(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/setup/currencies/", {
            "code": "AED", "name_ar": "درهم"
        }, format="json")
        self.assertEqual(response.status_code, 403)


from django.core.management import call_command
from django.core.management.base import CommandError
from .models import PlatformOwner


class PlatformOwnerBootstrapCommandTests(TestCase):
    def test_registers_an_active_superuser(self):
        user = get_user_model().objects.create_superuser(
            username="initial-owner", email="owner@example.test", password="long-test-password"
        )
        call_command("bootstrap_platform_owner", username=user.username)
        owner = PlatformOwner.objects.get(user=user)
        self.assertTrue(owner.is_active)

    def test_rejects_a_regular_user(self):
        user = get_user_model().objects.create_user(
            username="regular-user", email="regular@example.test", password="long-test-password"
        )
        with self.assertRaises(CommandError):
            call_command("bootstrap_platform_owner", username=user.username)
        self.assertFalse(PlatformOwner.objects.filter(user=user).exists())

    def test_rejects_an_inactive_superuser(self):
        user = get_user_model().objects.create_superuser(
            username="inactive-owner", email="inactive@example.test", password="long-test-password"
        )
        user.is_active = False
        user.save(update_fields=("is_active",))
        with self.assertRaises(CommandError):
            call_command("bootstrap_platform_owner", username=user.username)
        self.assertFalse(PlatformOwner.objects.filter(user=user).exists())

    def test_does_not_replace_an_existing_platform_owner(self):
        User = get_user_model()
        owner_user = User.objects.create_superuser(
            username="owner-one", email="one@example.test", password="long-test-password"
        )
        other_user = User.objects.create_superuser(
            username="owner-two", email="two@example.test", password="long-test-password"
        )
        PlatformOwner.objects.create(user=owner_user)
        with self.assertRaises(CommandError):
            call_command("bootstrap_platform_owner", username=other_user.username)
        self.assertFalse(PlatformOwner.objects.filter(user=other_user).exists())


class BranchMembershipIsolationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="cairo-accountant", password="long-test-password",
        )
        self.egp = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="BRANCH-EG", name_ar="شركة الفروع",
            country=Company.Country.EGYPT,
            activity=Company.Activity.COMMERCIAL,
            base_currency=self.egp,
        )
        self.cairo = Branch.objects.create(
            company=self.company, code="CAI", name_ar="القاهرة",
        )
        self.alex = Branch.objects.create(
            company=self.company, code="ALX", name_ar="الإسكندرية",
        )
        self.other_company = Company.objects.create(
            code="BRANCH-SA", name_ar="شركة أخرى",
            country=Company.Country.SAUDI_ARABIA,
            activity=Company.Activity.SERVICE,
            base_currency=self.egp,
        )
        self.other_branch = Branch.objects.create(
            company=self.other_company, code="RYD", name_ar="الرياض",
        )

    def test_user_sees_only_explicitly_assigned_active_branch(self):
        from .models import CompanyMembership

        membership = CompanyMembership.objects.create(
            user=self.user,
            company=self.company,
            role=CompanyMembership.Role.ACCOUNTANT,
            all_branches=False,
        )
        membership.branches.add(self.cairo)

        visible_ids = set(
            accessible_branches(self.user, self.company.pk).values_list("pk", flat=True)
        )
        self.assertSetEqual(visible_ids, {self.cairo.pk})
        self.assertFalse(accessible_branches(self.user, self.other_company.pk).exists())

    def test_all_branches_membership_includes_only_that_company(self):
        from .models import CompanyMembership

        membership = CompanyMembership.objects.create(
            user=self.user,
            company=self.company,
            role=CompanyMembership.Role.ACCOUNTANT,
            all_branches=True,
        )
        membership.branches.add(self.other_branch)

        visible_ids = set(
            accessible_branches(self.user, self.company.pk).values_list("pk", flat=True)
        )
        self.assertSetEqual(visible_ids, {self.cairo.pk, self.alex.pk})
        self.assertNotIn(self.other_branch.pk, visible_ids)

    def test_inactive_membership_grants_no_branch_access(self):
        from .models import CompanyMembership

        CompanyMembership.objects.create(
            user=self.user,
            company=self.company,
            role=CompanyMembership.Role.ACCOUNTANT,
            all_branches=True,
            is_active=False,
        )
        self.assertFalse(accessible_branches(self.user, self.company.pk).exists())


    def test_membership_serializer_rejects_branch_from_another_company(self):
        from .serializers import CompanyMembershipSerializer

        serializer = CompanyMembershipSerializer(data={
            "company": self.company.pk,
            "user": self.user.pk,
            "role": "accountant",
            "all_branches": False,
            "branches": [self.cairo.pk, self.other_branch.pk],
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("branches", serializer.errors)
