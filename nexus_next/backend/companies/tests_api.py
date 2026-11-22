from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from .models import Branch, Company, CompanyMembership, Currency, PlatformOwner


class CompanySetupApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reader", password="test-password")
        self.currency = Currency.objects.create(code="SAR", name_ar="ريال سعودي")
        self.company = Company.objects.create(
            code="OWN", name_ar="الشركة المصرح بها", country="SA",
            activity="service", base_currency=self.currency,
        )
        self.other_company = Company.objects.create(
            code="OTHER", name_ar="شركة غير مصرح بها", country="EG",
            activity="commercial", base_currency=self.currency,
        )

    def test_protected_company_delete_returns_clear_client_error(self):
        branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي")
        admin = get_user_model().objects.create_user(username="company-delete-admin", password="test-password")
        CompanyMembership.objects.create(
            user=admin, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(admin)
        response = self.client.delete(f"/api/setup/companies/{self.company.pk}/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("بيانات مرتبطة", response.data["detail"])
        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())
        self.assertTrue(Branch.objects.filter(pk=branch.pk).exists())

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

    def test_first_company_can_be_created_by_system_admin_and_grants_company_admin(self):
        system_admin = get_user_model().objects.create_superuser(
            username="first-company-admin", email="admin@example.test", password="test-password"
        )
        PlatformOwner.objects.create(user=system_admin)
        self.client.force_authenticate(system_admin)
        response = self.client.post("/api/setup/companies/", {
            "code": "FIRST-CO",
            "name_ar": "أول شركة",
            "country": "EG",
            "activity": "commercial",
            "base_currency": self.currency.pk,
            "is_active": True,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        company_id = response.data["id"]
        self.assertFalse(CompanyMembership.objects.filter(user=system_admin, company_id=company_id).exists())
        self.assertTrue(Company.objects.filter(pk=company_id).exists())

    def test_non_staff_user_cannot_create_first_company(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/setup/companies/", {
            "code": "DENIED-CO",
            "name_ar": "شركة مرفوضة",
            "country": "EG",
            "activity": "commercial",
            "base_currency": self.currency.pk,
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Company.objects.filter(code="DENIED-CO").exists())

    def test_company_list_contains_only_active_memberships(self):
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant")
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/companies/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["code"] for row in response.data], ["OWN"])

    def test_company_without_membership_is_not_visible(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/companies/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])


    def test_company_list_only_shows_assigned_companies(self):
        allowed = Company.objects.create(
            code="ALLOWED", name_ar="الشركة المسموحة", country="SA",
            activity="service", base_currency=self.currency,
        )
        hidden = Company.objects.create(
            code="HIDDEN", name_ar="الشركة المخفية", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        CompanyMembership.objects.create(user=self.user, company=allowed, all_branches=True)
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/companies/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [allowed.pk])
        self.assertNotIn(hidden.pk, [row["id"] for row in response.data])

    def test_branch_access_can_be_limited_to_selected_branches(self):
        company = Company.objects.create(
            code="BRANCH", name_ar="شركة الفروع", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        first = Branch.objects.create(company=company, code="A", name_ar="فرع أ")
        Branch.objects.create(company=company, code="B", name_ar="فرع ب")
        membership = CompanyMembership.objects.create(user=self.user, company=company)
        membership.branches.add(first)
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/branches/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [first.pk])



    def test_company_admin_can_update_company_profile_without_creating_new_company(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            f"/api/setup/companies/{self.company.pk}/",
            {
                "name_ar": "اسم الشركة بعد التحديث",
                "name_en": "Updated Company",
                "tax_registration_number": "123456789",
                "address": "عنوان الشركة",
                "phone": "+201000000000",
                "email": "office@example.test",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Company.objects.count(), 2)
        self.company.refresh_from_db()
        self.assertEqual(self.company.name_ar, "اسم الشركة بعد التحديث")
        self.assertEqual(self.company.name_en, "Updated Company")
        self.assertEqual(self.company.tax_registration_number, "123456789")

    def test_company_admin_cannot_change_foundational_company_settings(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            f"/api/setup/companies/{self.company.pk}/",
            {"country": "EG", "activity": "commercial", "code": "CHANGED"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.company.refresh_from_db()
        self.assertEqual(self.company.country, "SA")
        self.assertEqual(self.company.activity, "service")
        self.assertEqual(self.company.code, "OWN")

    def test_viewer_cannot_change_company_branch_or_fiscal_year_settings(self):
        branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(self.user)

        company_update = self.client.patch(
            f"/api/setup/companies/{self.company.pk}/",
            {"name_ar": "تغيير غير مصرح"},
            format="json",
        )
        self.assertEqual(company_update.status_code, 403)

        branch_create = self.client.post("/api/setup/branches/", {
            "company": self.company.pk, "code": "NEW", "name_ar": "فرع جديد",
        }, format="json")
        self.assertEqual(branch_create.status_code, 403)

        branch_update = self.client.patch(
            f"/api/setup/branches/{branch.pk}/",
            {"name_ar": "تعديل غير مصرح"},
            format="json",
        )
        self.assertEqual(branch_update.status_code, 403)

        year_create = self.client.post("/api/setup/fiscal-years/", {
            "company": self.company.pk, "name": "2027",
            "start_date": "2027-01-01", "end_date": "2027-12-31",
        }, format="json")
        self.assertEqual(year_create.status_code, 403)

    def test_company_admin_can_manage_branch_and_fiscal_year_without_global_staff(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        self.client.force_authenticate(self.user)

        branch = self.client.post("/api/setup/branches/", {
            "company": self.company.pk, "code": "ADMIN-BR", "name_ar": "فرع المدير",
        }, format="json")
        self.assertEqual(branch.status_code, 201, branch.data)

        year = self.client.post("/api/setup/fiscal-years/", {
            "company": self.company.pk, "name": "2027",
            "start_date": "2027-01-01", "end_date": "2027-12-31",
        }, format="json")
        self.assertEqual(year.status_code, 201, year.data)

    def test_fiscal_year_cannot_be_moved_to_another_company(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        from .models import FiscalYear
        year = FiscalYear.objects.create(
            company=self.company, name="2027",
            start_date="2027-01-01", end_date="2027-12-31",
        )
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            f"/api/setup/fiscal-years/{year.pk}/",
            {"company": self.other_company.pk}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        year.refresh_from_db()
        self.assertEqual(year.company_id, self.company.pk)

    def test_fiscal_year_rejects_reversed_date_range_on_partial_update(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        from .models import FiscalYear
        year = FiscalYear.objects.create(
            company=self.company, name="2027",
            start_date="2027-01-01", end_date="2027-12-31",
        )
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            f"/api/setup/fiscal-years/{year.pk}/",
            {"start_date": "2028-01-01"}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        year.refresh_from_db()
        self.assertEqual(str(year.start_date), "2027-01-01")
        self.assertEqual(str(year.end_date), "2027-12-31")

    def test_duplicate_branch_code_and_head_office_return_validation_errors(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True
        )
        self.client.force_authenticate(self.user)

        duplicate_code = self.client.post("/api/setup/branches/", {
            "company": self.company.pk, "code": "MAIN", "name_ar": "كود مكرر",
        }, format="json")
        self.assertEqual(duplicate_code.status_code, 400, duplicate_code.data)
        self.assertTrue("code" in duplicate_code.data or "non_field_errors" in duplicate_code.data)
        self.assertEqual(Branch.objects.filter(company=self.company, code="MAIN").count(), 1)

        duplicate_head = self.client.post("/api/setup/branches/", {
            "company": self.company.pk, "code": "SECOND", "name_ar": "رئيسي إضافي",
            "is_head_office": True,
        }, format="json")
        self.assertEqual(duplicate_head.status_code, 400, duplicate_head.data)
        self.assertIn("is_head_office", duplicate_head.data)
        self.assertEqual(Branch.objects.filter(company=self.company, is_head_office=True).count(), 1)

    def test_duplicate_fiscal_year_name_returns_validation_error(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        from .models import FiscalYear
        FiscalYear.objects.create(
            company=self.company, name="2027",
            start_date="2027-01-01", end_date="2027-12-31",
        )
        self.client.force_authenticate(self.user)

        response = self.client.post("/api/setup/fiscal-years/", {
            "company": self.company.pk, "name": "2027",
            "start_date": "2028-01-01", "end_date": "2028-12-31",
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue("name" in response.data or "non_field_errors" in response.data)
        self.assertEqual(FiscalYear.objects.filter(company=self.company, name="2027").count(), 1)

    def test_fiscal_year_rejects_overlapping_period_for_same_company(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        from .models import FiscalYear
        FiscalYear.objects.create(
            company=self.company, name="2027",
            start_date="2027-01-01", end_date="2027-12-31",
        )
        self.client.force_authenticate(self.user)

        response = self.client.post("/api/setup/fiscal-years/", {
            "company": self.company.pk, "name": "FY-2027-SECOND",
            "start_date": "2027-07-01", "end_date": "2028-06-30",
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(FiscalYear.objects.filter(company=self.company).count(), 1)

    def test_company_admin_can_update_branch_contact_details_and_deactivate(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        branch = Branch.objects.create(company=self.company, code="EDIT-BR", name_ar="الفرع قبل التعديل")
        Branch.objects.create(company=self.company, code="KEEP-BR", name_ar="فرع بديل نشط")
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            f"/api/setup/branches/{branch.pk}/",
            {"name_ar": "الفرع بعد التعديل", "address": "عنوان جديد", "phone": "01000000000", "is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        branch.refresh_from_db()
        self.assertEqual(branch.name_ar, "الفرع بعد التعديل")
        self.assertEqual(branch.phone, "01000000000")
        self.assertFalse(branch.is_active)

    def test_company_admin_cannot_disable_the_last_active_branch(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        branch = Branch.objects.create(company=self.company, code="ONLY-BR", name_ar="الفرع الوحيد")
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            f"/api/setup/branches/{branch.pk}/?include_inactive=true",
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        branch.refresh_from_db()
        self.assertTrue(branch.is_active)

    def test_company_admin_cannot_disable_both_active_branches(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        first = Branch.objects.create(company=self.company, code="FIRST", name_ar="الفرع الأول")
        second = Branch.objects.create(company=self.company, code="SECOND", name_ar="الفرع الثاني")
        self.client.force_authenticate(self.user)

        first_response = self.client.patch(
            f"/api/setup/branches/{first.pk}/", {"is_active": False}, format="json"
        )
        self.assertEqual(first_response.status_code, 200, first_response.data)

        second_response = self.client.patch(
            f"/api/setup/branches/{second.pk}/", {"is_active": False}, format="json"
        )
        self.assertEqual(second_response.status_code, 400, second_response.data)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)

    def test_only_company_admin_can_view_inactive_branches_in_setup(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        inactive = Branch.objects.create(
            company=self.company, code="INACTIVE-BR", name_ar="فرع موقوف", is_active=False
        )
        self.client.force_authenticate(self.user)
        normal = self.client.get(f"/api/setup/branches/?company={self.company.pk}")
        self.assertEqual(normal.status_code, 200, normal.data)
        self.assertNotIn(inactive.pk, [row["id"] for row in normal.data])
        setup = self.client.get(
            f"/api/setup/branches/?company={self.company.pk}&include_inactive=true"
        )
        self.assertEqual(setup.status_code, 200, setup.data)
        self.assertIn(inactive.pk, [row["id"] for row in setup.data])

        viewer = get_user_model().objects.create_user(username="branch-viewer", password="test-password")
        CompanyMembership.objects.create(user=viewer, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(viewer)
        viewer_setup = self.client.get(
            f"/api/setup/branches/?company={self.company.pk}&include_inactive=true"
        )
        self.assertEqual(viewer_setup.status_code, 200, viewer_setup.data)
        self.assertNotIn(inactive.pk, [row["id"] for row in viewer_setup.data])

    def test_company_admin_cannot_change_existing_branch_code(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        branch = Branch.objects.create(company=self.company, code="FIXED-BR", name_ar="فرع ثابت")
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            f"/api/setup/branches/{branch.pk}/", {"code": "NEW-CODE"}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.data)
        branch.refresh_from_db()
        self.assertEqual(branch.code, "FIXED-BR")

    def test_foreign_company_branch_cannot_be_granted_through_membership(self):
        own = CompanyMembership.objects.create(user=self.user, company=self.company)
        foreign_branch = Branch.objects.create(
            company=self.other_company, code="FOREIGN", name_ar="فرع شركة أخرى"
        )
        own.branches.add(foreign_branch)
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/branches/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(foreign_branch.pk, [row["id"] for row in response.data])


    def test_company_admin_can_manage_membership_role_and_branch_scope(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        target = get_user_model().objects.create_user(username="new-accountant", password="test-password")
        branch = Branch.objects.create(company=self.company, code="ROLE-BR", name_ar="فرع الصلاحيات")
        self.client.force_authenticate(self.user)
        created = self.client.post("/api/setup/memberships/", {
            "company": self.company.pk, "user": target.pk, "role": "accountant",
            "all_branches": False, "branches": [branch.pk], "is_active": True,
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["role"], "accountant")
        self.assertEqual(created.data["branches"], [branch.pk])

    def test_non_admin_cannot_change_company_membership_roles(self):
        admin = get_user_model().objects.create_user(username="role-admin", password="test-password")
        accountant = get_user_model().objects.create_user(username="role-accountant", password="test-password")
        target = get_user_model().objects.create_user(username="role-target", password="test-password")
        CompanyMembership.objects.create(user=admin, company=self.company, role="admin", all_branches=True)
        membership = CompanyMembership.objects.create(user=target, company=self.company, role="viewer")
        CompanyMembership.objects.create(user=accountant, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(accountant)
        visible = self.client.get(f"/api/setup/memberships/?company={self.company.pk}")
        self.assertEqual(visible.status_code, 200, visible.data)
        self.assertEqual([row["user"] for row in visible.data], [accountant.pk])
        response = self.client.patch(
            f"/api/setup/memberships/{membership.pk}/", {"role": "admin"}, format="json"
        )
        self.assertEqual(response.status_code, 404)
        membership.refresh_from_db()
        self.assertEqual(membership.role, "viewer")

    def test_last_active_company_admin_cannot_be_disabled_or_demoted(self):
        membership = CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            f"/api/setup/memberships/{membership.pk}/", {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.data)
        membership.refresh_from_db()
        self.assertTrue(membership.is_active)
        self.assertEqual(membership.role, "admin")

    def test_membership_rejects_branch_from_another_company(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        foreign_branch = Branch.objects.create(
            company=self.other_company, code="FOREIGN-ROLE", name_ar="فرع خارجي"
        )
        target = get_user_model().objects.create_user(username="foreign-branch-target", password="test-password")
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/setup/memberships/", {
            "company": self.company.pk, "user": target.pk, "role": "accountant",
            "all_branches": False, "branches": [foreign_branch.pk], "is_active": True,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)


    def test_company_admin_can_search_unassigned_active_users(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        target = get_user_model().objects.create_user(
            username="candidate-user", password="test-password", display_name="مستخدم جديد"
        )
        self.client.force_authenticate(self.user)
        response = self.client.get(
            f"/api/setup/memberships/users/?company={self.company.pk}&search=candidate"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data], [target.pk])

    def test_current_membership_endpoint_returns_role_without_exposing_other_users(self):
        membership = CompanyMembership.objects.create(
            user=self.user, company=self.company, role="accountant", all_branches=True
        )
        self.client.force_authenticate(self.user)
        response = self.client.get(f"/api/setup/memberships/current/?company={self.company.pk}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], membership.pk)
        self.assertEqual(response.data["role"], "accountant")


    def test_company_admin_cannot_reassign_existing_membership_to_another_user(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        original = get_user_model().objects.create_user(username="membership-original", password="test-password")
        replacement = get_user_model().objects.create_user(username="membership-replacement", password="test-password")
        membership = CompanyMembership.objects.create(user=original, company=self.company, role="viewer")
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            f"/api/setup/memberships/{membership.pk}/", {"user": replacement.pk}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.data)
        membership.refresh_from_db()
        self.assertEqual(membership.user_id, original.pk)



    def test_staff_without_platform_owner_profile_cannot_create_company(self):
        staff = get_user_model().objects.create_user(
            username="ordinary-staff", password="test-password", is_staff=True
        )
        self.client.force_authenticate(staff)
        response = self.client.post("/api/setup/companies/", {
            "code": "STAFF-DENIED", "name_ar": "محاولة موظف",
            "country": "EG", "activity": "commercial", "base_currency": self.currency.pk,
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Company.objects.filter(code="STAFF-DENIED").exists())

    def test_platform_owner_can_list_company_registry_without_tenant_membership(self):
        owner = get_user_model().objects.create_superuser(
            username="platform-owner-registry", email="owner@example.test", password="test-password"
        )
        PlatformOwner.objects.create(user=owner)
        self.company.is_active = False
        self.company.save(update_fields=["is_active"])
        self.client.force_authenticate(owner)
        response = self.client.get("/api/setup/companies/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual({row["id"] for row in response.data}, {self.company.pk, self.other_company.pk})
        self.assertFalse(CompanyMembership.objects.filter(user=owner).exists())

    def test_company_admin_cannot_permanently_delete_company(self):
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True
        )
        self.client.force_authenticate(self.user)
        response = self.client.delete(f"/api/setup/companies/{self.company.pk}/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())
