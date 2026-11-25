import re

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse


class PasswordResetTests(TestCase):
    """Admin password reset by email, using Django's built-in views."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            "alice", "alice@example.com", "old-password", is_staff=True
        )

    def test_login_page_links_to_reset(self):
        response = self.client.get(reverse("admin:login"))
        self.assertContains(response, reverse("admin_password_reset"))

    def test_reset_flow_sets_new_password(self):
        response = self.client.post(
            reverse("admin_password_reset"), {"email": "alice@example.com"}
        )
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["alice@example.com"])

        link = re.search(r"http://testserver(/admin/reset/\S+/)", mail.outbox[0].body)
        self.assertIsNotNone(link)
        # The token URL redirects to a token-free "set-password" URL.
        set_password_url = self.client.get(link.group(1)).url
        new_password = "glacier-Moves-42"
        response = self.client.post(
            set_password_url,
            {"new_password1": new_password, "new_password2": new_password},
        )
        self.assertRedirects(response, reverse("password_reset_complete"))
        self.assertContains(
            self.client.get(reverse("password_reset_complete")),
            reverse("admin:login"),
        )
        self.assertTrue(self.client.login(username="alice", password=new_password))

    def test_unknown_email_sends_nothing(self):
        response = self.client.post(
            reverse("admin_password_reset"), {"email": "nobody@example.com"}
        )
        # Same response as a known address: account existence is not revealed.
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)
