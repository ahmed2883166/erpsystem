import json

from django.contrib.auth import get_user_model
from django.contrib.admin.models import LogEntry
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase
from django.db import connection, DatabaseError, transaction
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse, JsonResponse
from rest_framework.test import APIClient

from companies.models import Branch, Company, CompanyMembership, Currency
from .middleware import AuditTrailMiddleware
from .models import AuditEvent


class AuditTrailMiddlewareTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="audit-user", password="test-password")
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه")
        self.company = Company.objects.create(
            code="AUDIT", name_ar="شركة التدقيق", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        self.branch = Branch.objects.create(company=self.company, code="01", name_ar="الفرع الرئيسي")
        self.factory = RequestFactory()

    def test_write_attempt_is_recorded_without_payload_or_query_string(self):
        def response(_request):
            return JsonResponse({"id": 73, "secret": "must-not-be-saved"}, status=201)

        middleware = AuditTrailMiddleware(response)
        request = self.factory.post(
            "/api/accounting/journal-entries/?token=private",
            data=json.dumps({"company": self.company.pk, "branch": self.branch.pk, "password": "private"}),
            content_type="application/json",
            HTTP_USER_AGENT="NEXUS test",
            REMOTE_ADDR="127.0.0.1",
        )
        request.user = self.user
        result = middleware(request)
        event = AuditEvent.objects.get()
        self.assertEqual(result.status_code, 201)
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.company, self.company)
        self.assertEqual(event.branch, self.branch)
        self.assertEqual(event.object_id, "73")
        self.assertEqual(event.action, AuditEvent.Action.CREATE)
        self.assertTrue(event.succeeded)
        self.assertEqual(event.resource, "accounting/journal-entries")
        self.assertNotIn("private", str(event.metadata))
        self.assertNotIn("secret", str(event.metadata))

    def test_failed_write_attempt_is_recorded_as_failed(self):
        middleware = AuditTrailMiddleware(lambda _request: JsonResponse({"detail": "denied"}, status=403))
        request = self.factory.patch(
            "/api/inventory/products/5/",
            data=json.dumps({"company": self.company.pk}),
            content_type="application/json",
        )
        request.user = self.user
        middleware(request)
        event = AuditEvent.objects.get()
        self.assertFalse(event.succeeded)
        self.assertEqual(event.status_code, 403)
        self.assertEqual(event.action, AuditEvent.Action.UPDATE)

    def test_read_requests_are_not_recorded(self):
        middleware = AuditTrailMiddleware(lambda _request: JsonResponse({"ok": True}))
        request = self.factory.get("/api/accounting/journal-entries/")
        request.user = self.user
        middleware(request)
        self.assertEqual(AuditEvent.objects.count(), 0)


    def test_delete_keeps_company_scope_after_target_row_is_removed(self):
        branch = self.branch
        branch_id = branch.pk

        def delete_target(_request):
            branch.delete()
            return HttpResponse(status=204)

        middleware = AuditTrailMiddleware(delete_target)
        request = self.factory.delete(f"/api/setup/branches/{branch_id}/")
        request.user = self.user
        middleware(request)

        event = AuditEvent.objects.get()
        self.assertEqual(event.action, AuditEvent.Action.DELETE)
        self.assertEqual(event.company_id, self.company.pk)
        self.assertIsNone(event.branch_id)
        self.assertEqual(event.object_id, str(branch_id))

    def test_real_api_write_is_audited_before_drf_consumes_body(self):
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post("/api/setup/branches/", {
            "company": self.company.pk, "code": "DENIED", "name_ar": "محاولة مرفوضة",
        }, format="json")
        self.assertEqual(response.status_code, 403)
        event = AuditEvent.objects.get()
        self.assertEqual(event.company_id, self.company.pk)
        self.assertEqual(event.action, AuditEvent.Action.CREATE)
        self.assertFalse(event.succeeded)
        self.assertEqual(event.status_code, 403)
        self.assertNotIn("محاولة مرفوضة", str(event.metadata))

    def test_audit_events_cannot_be_changed_or_deleted(self):
        event = AuditEvent.objects.create(
            actor=self.user, company=self.company, action="create",
            resource="test", http_method="POST", status_code=201, succeeded=True,
        )
        event.resource = "tampered"
        with self.assertRaises(TypeError):
            event.save()
        with self.assertRaises(TypeError):
            event.delete()
        with self.assertRaises(TypeError):
            AuditEvent.objects.filter(pk=event.pk).update(resource="tampered")
        with self.assertRaises(TypeError):
            AuditEvent.objects.filter(pk=event.pk).delete()
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).resource, "test")


    def test_postgresql_trigger_blocks_raw_sql_mutation(self):
        event = AuditEvent.objects.create(
            actor=self.user, company=self.company, action="create",
            resource="immutable-test", http_method="POST", status_code=201, succeeded=True,
        )
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE audit_auditevent SET resource = %s WHERE id = %s",
                        ["tampered", event.pk],
                    )
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM audit_auditevent WHERE id = %s", [event.pk])
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).resource, "immutable-test")


    def test_audit_history_protects_branch_and_actor_identity(self):
        AuditEvent.objects.create(
            actor=self.user, company=self.company, branch=self.branch,
            action="create", resource="protected-test", http_method="POST",
            status_code=201, succeeded=True,
        )
        with self.assertRaises(ProtectedError):
            self.branch.delete()
        with self.assertRaises(ProtectedError):
            self.user.delete()
        self.assertTrue(Branch.objects.filter(pk=self.branch.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())


class AuditEventApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="audit-reader", password="test-password")
        self.currency = Currency.objects.create(code="SAR", name_ar="ريال")
        self.company = Company.objects.create(
            code="OWN-AUD", name_ar="شركة مسموحة", country="SA",
            activity="service", base_currency=self.currency,
        )
        self.branch = Branch.objects.create(company=self.company, code="A", name_ar="فرع أ")
        self.other_company = Company.objects.create(
            code="OTHER-AUD", name_ar="شركة أخرى", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        self.other_branch = Branch.objects.create(company=self.other_company, code="B", name_ar="فرع ب")
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="accountant", all_branches=True
        )
        AuditEvent.objects.create(
            actor=self.user, company=self.company, branch=self.branch,
            action="create", resource="accounting/journal-entries",
            http_method="POST", status_code=201, succeeded=True,
        )
        AuditEvent.objects.create(
            actor=self.user, company=self.other_company, branch=self.other_branch,
            action="delete", resource="inventory/products",
            http_method="DELETE", status_code=204, succeeded=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_audit_list_is_scoped_to_authorized_companies_and_branches(self):
        response = self.client.get("/api/audit/events/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["company_id"], self.company.pk)

    def test_viewer_role_cannot_read_company_audit_log(self):
        membership = CompanyMembership.objects.get(user=self.user, company=self.company)
        membership.role = "viewer"
        membership.save()
        response = self.client.get("/api/audit/events/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_audit_limit_is_bounded(self):
        response = self.client.get("/api/audit/events/?limit=9999")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)


class AuthenticationAndAdminAuditSignalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="signal-audit-user", password="test-password"
        )
        self.factory = RequestFactory()

    def test_successful_login_is_audited(self):
        request = self.factory.post("/login/")
        user_logged_in.send(
            sender=get_user_model(), request=request, user=self.user
        )
        event = AuditEvent.objects.get(resource="authentication.login")
        self.assertEqual(event.actor, self.user)
        self.assertTrue(event.succeeded)
        self.assertEqual(event.status_code, 200)
        self.assertEqual(event.http_method, "AUTH")
        self.assertEqual(event.metadata, {"event": "login"})

    def test_logout_is_audited(self):
        request = self.factory.post("/logout/")
        user_logged_out.send(
            sender=get_user_model(), request=request, user=self.user
        )
        event = AuditEvent.objects.get(resource="authentication.logout")
        self.assertEqual(event.actor, self.user)
        self.assertTrue(event.succeeded)
        self.assertEqual(event.status_code, 200)

    def test_failed_login_is_audited_without_credentials(self):
        request = self.factory.post("/login/")
        user_login_failed.send(
            sender=get_user_model(),
            credentials={"username": "private-user", "password": "private-password"},
            request=request,
        )
        event = AuditEvent.objects.get(resource="authentication.login_failed")
        self.assertIsNone(event.actor)
        self.assertFalse(event.succeeded)
        self.assertEqual(event.status_code, 401)
        self.assertEqual(event.object_id, "")
        self.assertEqual(event.metadata, {"event": "login_failed"})
        self.assertNotIn("private-user", str(event.metadata))
        self.assertNotIn("private-password", str(event.metadata))

    def test_django_admin_audit_is_scoped_to_company_and_branch(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه")
        company = Company.objects.create(
            code="ADMIN-SCOPE", name_ar="شركة الإدارة", country="EG",
            activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="ADM", name_ar="فرع الإدارة"
        )
        content_type = ContentType.objects.get_for_model(Branch)
        LogEntry.objects.create(
            user=self.user,
            content_type=content_type,
            object_id=str(branch.pk),
            object_repr="فرع الإدارة",
            action_flag=LogEntry.ActionFlag.CHANGE,
            change_message="changed",
        )
        event = AuditEvent.objects.get(
            resource="django_admin.companies.branch",
            object_id=str(branch.pk),
        )
        self.assertEqual(event.company, company)
        self.assertEqual(event.branch, branch)

    def test_django_admin_add_change_and_delete_are_audited(self):
        content_type = ContentType.objects.get_for_model(get_user_model())
        for flag, expected in (
            (LogEntry.ActionFlag.ADDITION, AuditEvent.Action.CREATE),
            (LogEntry.ActionFlag.CHANGE, AuditEvent.Action.UPDATE),
            (LogEntry.ActionFlag.DELETION, AuditEvent.Action.DELETE),
        ):
            with self.subTest(flag=flag):
                LogEntry.objects.create(
                    user=self.user,
                    content_type=content_type,
                    object_id=str(self.user.pk),
                    object_repr="Signal audit user",
                    action_flag=flag,
                    change_message="changed",
                )
                event = AuditEvent.objects.get(
                    resource="django_admin.accounts.user",
                    action=expected,
                )
                self.assertEqual(event.actor, self.user)
                self.assertEqual(event.object_id, str(self.user.pk))
                self.assertEqual(event.http_method, "ADMIN")
                self.assertTrue(event.succeeded)
                self.assertEqual(event.metadata["event"], "admin_change")

