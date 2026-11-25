from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.http import JsonResponse

from companies.models import Branch, Company, Currency
from audit.middleware import AuditTrailMiddleware
from audit.models import AuditEvent


class ApprovalAuditTrailTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="approval-audit-user", password="test-password"
        )
        currency = Currency.objects.create(code="EGP", name_ar="جنيه")
        self.company = Company.objects.create(
            code="APR-AUD", name_ar="شركة موافقات", country="EG",
            activity="commercial", base_currency=currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="01", name_ar="الفرع الرئيسي"
        )
        self.factory = RequestFactory()

    def test_approve_and_reject_actions_are_recorded_without_decision_payload(self):
        for endpoint, status_code in (("approve", 200), ("reject", 400)):
            with self.subTest(endpoint=endpoint):
                request = self.factory.post(
                    f"/api/approvals/requests/17/{endpoint}/",
                    data='{"note":"قرار إداري سري"}',
                    content_type="application/json",
                    HTTP_USER_AGENT="NEXUS approval test",
                )
                request.user = self.user
                middleware = AuditTrailMiddleware(
                    lambda _request, status_code=status_code: JsonResponse({"id": 17}, status=status_code)
                )
                with patch(
                    "audit.middleware._target_before_request",
                    return_value=(17, self.company.pk, self.branch.pk),
                ):
                    middleware(request)

                event = AuditEvent.objects.latest("pk")
                self.assertEqual(event.actor, self.user)
                self.assertEqual(event.company, self.company)
                self.assertEqual(event.branch, self.branch)
                self.assertEqual(event.resource, f"approvals/requests/17/{endpoint}")
                self.assertEqual(event.object_id, "17")
                self.assertEqual(event.action, AuditEvent.Action.ACTION)
                self.assertEqual(event.status_code, status_code)
                self.assertEqual(event.succeeded, status_code < 400)
                self.assertNotIn("قرار إداري سري", str(event.metadata))
                self.assertNotIn("note", str(event.metadata))
