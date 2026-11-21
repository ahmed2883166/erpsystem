import json

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
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

        def delete_target(_request):
            branch.delete()
            return HttpResponse(status=204)

        middleware = AuditTrailMiddleware(delete_target)
        request = self.factory.delete(f"/api/setup/branches/{branch.pk}/")
        request.user = self.user
        middleware(request)

        event = AuditEvent.objects.get()
        self.assertEqual(event.action, AuditEvent.Action.DELETE)
        self.assertEqual(event.company_id, self.company.pk)
        self.assertIsNone(event.branch_id)
        self.assertEqual(event.object_id, str(branch.pk))

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
