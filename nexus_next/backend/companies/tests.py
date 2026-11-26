from datetime import date
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.contrib.auth import get_user_model
from .access import accessible_branches, accessible_companies, can_access_branch
from .models import Branch, Company, CompanyMembership, Currency, FiscalYear, PlatformOwner
from .serializers import CompanySerializer

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

    def test_company_serializer_preserves_multiple_activities_and_soft_disables_removed_ones(self):
        serializer = CompanySerializer(data={
            "code": "MULTI-ACT", "name_ar": "شركة متعددة الأنشطة",
            "country": Company.Country.EGYPT, "activity": Company.Activity.COMMERCIAL,
            "activities": [
                Company.Activity.COMMERCIAL,
                Company.Activity.SERVICE,
                Company.Activity.MEDICAL,
            ],
            "base_currency": self.egp.pk,
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        company = serializer.save()
        self.assertSetEqual(
            set(company.activity_assignments.filter(is_active=True).values_list("activity", flat=True)),
            {"commercial", "service", "medical"},
        )

        update = CompanySerializer(
            company, data={"activities": ["commercial", "medical"]}, partial=True
        )
        self.assertTrue(update.is_valid(), update.errors)
        update.save()
        self.assertSetEqual(
            set(company.activity_assignments.filter(is_active=True).values_list("activity", flat=True)),
            {"commercial", "medical"},
        )
        self.assertFalse(
            company.activity_assignments.get(activity="service").is_active
        )
        self.assertNotIn("transport", dict(Company.Activity.choices))
        self.assertNotIn("contracting", dict(Company.Activity.choices))

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


class PlatformOwnerCompanyCreationApiTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(
            username="platform-owner-create",
            email="platform-owner-create@example.test",
            password="AdmRail-Owner-Password-2026!",
        )
        PlatformOwner.objects.create(user=self.owner)
        self.egp = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.client.force_authenticate(self.owner)

    def payload(self, username="new-company-manager"):
        return {
            "company": {
                "code": "OWNER-MULTI-ACT",
                "name_ar": "شركة المالك متعددة الأنشطة",
                "country": "EG",
                "activity": "commercial",
                "activities": ["commercial", "service", "medical"],
                "base_currency": self.egp.pk,
                "is_active": True,
            },
            "manager": {
                "username": username,
                "display_name": "مدير الشركة",
                "email": f"{username}@example.test",
                "password": "AdmRail-Manager-Password-2026!",
            },
        }

    def test_owner_creates_company_with_multiple_activities_and_manager_atomically(self):
        response = self.client.post(
            "/api/setup/companies/create-with-admin/",
            self.payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        company = Company.objects.get(code="OWNER-MULTI-ACT")
        self.assertSetEqual(
            set(company.activity_assignments.filter(is_active=True).values_list("activity", flat=True)),
            {"commercial", "service", "medical"},
        )
        manager = get_user_model().objects.get(username="new-company-manager")
        membership = CompanyMembership.objects.get(company=company, user=manager)
        self.assertEqual(membership.role, CompanyMembership.Role.ADMIN)
        self.assertTrue(membership.all_branches)
        self.assertTrue(membership.is_active)
        self.assertFalse(manager.is_superuser)
        self.assertFalse(manager.is_staff)
        self.assertFalse(CompanyMembership.objects.filter(company=company, user=self.owner).exists())
        self.assertEqual(Branch.objects.filter(company=company, is_head_office=True).count(), 1)

    def test_duplicate_manager_username_does_not_create_company(self):
        get_user_model().objects.create_user(
            username="already-used-manager",
            password="AdmRail-Existing-Password-2026!",
        )
        response = self.client.post(
            "/api/setup/companies/create-with-admin/",
            self.payload(username="already-used-manager"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Company.objects.filter(code="OWNER-MULTI-ACT").exists())

    def test_regular_user_cannot_create_company_and_manager(self):
        regular_user = get_user_model().objects.create_user(
            username="not-platform-owner",
            password="AdmRail-Regular-Password-2026!",
        )
        self.client.force_authenticate(regular_user)
        response = self.client.post(
            "/api/setup/companies/create-with-admin/",
            self.payload(username="blocked-manager"),
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Company.objects.filter(code="OWNER-MULTI-ACT").exists())


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
        self.assertTrue(can_access_branch(self.user, self.cairo.pk, self.company.pk))
        self.assertFalse(can_access_branch(self.user, self.alex.pk, self.company.pk))
        self.assertFalse(can_access_branch(self.user, self.other_branch.pk, self.company.pk))
        self.assertFalse(accessible_branches(self.user, self.other_company.pk).exists())

    def test_all_branches_membership_includes_only_that_company(self):
        from .models import CompanyMembership

        CompanyMembership.objects.create(
            user=self.user,
            company=self.company,
            role=CompanyMembership.Role.ACCOUNTANT,
            all_branches=True,
        )
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

    def test_company_admin_membership_requires_all_branches_at_model_save(self):
        from .models import CompanyMembership

        with self.assertRaises(ValidationError):
            CompanyMembership.objects.create(
                user=self.user,
                company=self.company,
                role=CompanyMembership.Role.ADMIN,
                all_branches=False,
            )

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


class CompanyDefaultBranchTests(TestCase):
    def test_company_creation_creates_active_head_office_automatically(self):
        egp = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        from .serializers import CompanySerializer

        serializer = CompanySerializer(data={
            "code": "AUTO-BRANCH",
            "name_ar": "شركة بدون إعداد فروع",
            "country": Company.Country.EGYPT,
            "activity": Company.Activity.COMMERCIAL,
            "base_currency": egp.pk,
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        company = serializer.save()

        branches = Branch.objects.filter(company=company)
        self.assertEqual(branches.count(), 1)
        main = branches.get()
        self.assertEqual(main.code, "MAIN")
        self.assertEqual(main.name_ar, "الفرع الرئيسي")
        self.assertTrue(main.is_head_office)
        self.assertTrue(main.is_active)

class HeadOfficeTransferApiTests(APITestCase):
    def setUp(self):
        from .models import CompanyMembership

        self.user = get_user_model().objects.create_user(
            username="branch-admin", password="long-test-password",
        )
        self.egp = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="HQ-TRANSFER", name_ar="شركة اختبار المركز الرئيسي",
            country=Company.Country.EGYPT,
            activity=Company.Activity.COMMERCIAL,
            base_currency=self.egp,
        )
        self.main = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="المركز الرئيسي",
            is_head_office=True, is_active=True,
        )
        self.new_branch = Branch.objects.create(
            company=self.company, code="BR-01", name_ar="فرع جديد",
            is_active=True,
        )
        CompanyMembership.objects.create(
            user=self.user, company=self.company,
            role=CompanyMembership.Role.ADMIN, all_branches=True, is_active=True,
        )
        self.client.force_authenticate(self.user)

    def test_admin_can_transfer_head_office_atomically(self):
        response = self.client.post(
            f"/api/setup/branches/{self.new_branch.pk}/make-head-office/",
            {}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.main.refresh_from_db()
        self.new_branch.refresh_from_db()
        self.assertFalse(self.main.is_head_office)
        self.assertTrue(self.new_branch.is_head_office)
        self.assertEqual(
            Branch.objects.filter(company=self.company, is_head_office=True).count(), 1
        )

    def test_branch_patch_cannot_change_head_office_flag(self):
        response = self.client.patch(
            f"/api/setup/branches/{self.main.pk}/",
            {"is_head_office": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.main.refresh_from_db()
        self.assertTrue(self.main.is_head_office)

    def test_inactive_branch_cannot_become_head_office(self):
        inactive = Branch.objects.create(
            company=self.company, code="BR-OFF", name_ar="فرع موقوف",
            is_active=False,
        )
        response = self.client.post(
            f"/api/setup/branches/{inactive.pk}/make-head-office/",
            {}, format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(self.main.refresh_from_db() is None)
        self.assertTrue(self.main.is_head_office)

    def test_non_admin_cannot_transfer_head_office(self):
        from .models import CompanyMembership

        CompanyMembership.objects.filter(user=self.user, company=self.company).update(
            role=CompanyMembership.Role.ACCOUNTANT,
        )
        response = self.client.post(
            f"/api/setup/branches/{self.new_branch.pk}/make-head-office/",
            {}, format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.main.refresh_from_db()
        self.assertTrue(self.main.is_head_office)


class PlatformOwnerTenantAdminIsolationTests(TestCase):
    def test_platform_owner_cannot_view_or_manage_company_memberships_in_django_admin(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from companies.admin import CompanyMembershipAdmin
        from .models import CompanyMembership, PlatformOwner

        user = get_user_model().objects.create_superuser(
            username="platform-owner-admin-check",
            email="platform-owner-admin@example.test",
            password="long-test-password",
        )
        PlatformOwner.objects.create(user=user)
        request = RequestFactory().get("/admin/companies/companymembership/")
        request.user = user
        model_admin = CompanyMembershipAdmin(CompanyMembership, admin.site)

        self.assertFalse(model_admin.has_module_permission(request))
        self.assertFalse(model_admin.has_view_permission(request))
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

