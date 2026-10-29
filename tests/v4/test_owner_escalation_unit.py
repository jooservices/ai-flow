"""Standard-library unit tests for the owner-facing hand-off text and grouping."""
from __future__ import annotations

import ast
import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from aiflow.v4.control import escalations
from aiflow.v4.control.jira import Jira, JiraError
from aiflow.v4.control.owner_notifications import _group, sender_for
from aiflow.v4.control.triage_claim import DEVELOPMENT_ELIGIBILITY_PIPELINE
from aiflow.v4.core.owner_escalation import MAX_OWNER_QUESTION_COMMENT_CHARS
from aiflow.v4.core.settings import Settings
from aiflow.v4.core.text import adf_to_text, text_to_adf
from aiflow.v4.worker.triage import owner_question_comment


class OwnerEscalationUnitTests(unittest.TestCase):
    def test_escalation_service_delegates_sql_to_repositories(self):
        tree = ast.parse(Path(escalations.__file__).read_text(encoding="utf-8"))
        sql = re.compile(r"^\s*(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WITH)\b", re.IGNORECASE)
        statements = [node.value for node in ast.walk(tree)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str) and sql.match(node.value)]

        self.assertEqual(statements, [])

    def test_question_comment_starts_with_the_question_and_names_options_and_recommendation(self):
        question = "Should the default stay compatible?"
        comment = owner_question_comment(question, ["Keep compatibility", "Change the response"],
                                         "Keep compatibility")

        self.assertTrue(comment.startswith(question))
        self.assertIn("Options:\n- Keep compatibility\n- Change the response", comment)
        self.assertIn("Recommended option: Keep compatibility", comment)
        self.assertTrue(comment.endswith("Answer in a Jira comment, then move the ticket to To Do."))

    def test_question_comment_requires_question_options_recommendation_and_fits_the_route_limit(self):
        question = "Should the default stay compatible?"
        base = owner_question_comment(question, ["x", "Recommended"], "Recommended", grouped=True)
        maximum_option = "x" * (MAX_OWNER_QUESTION_COMMENT_CHARS - len(base) + 1)
        maximum = owner_question_comment(question, [maximum_option, "Recommended"], "Recommended", grouped=True)

        self.assertEqual(len(maximum), MAX_OWNER_QUESTION_COMMENT_CHARS)
        with self.assertRaisesRegex(ValueError, "exceeds 6000"):
            owner_question_comment(question, [maximum_option + "x", "Recommended"], "Recommended", grouped=True)
        for args in (("", ["A"], "A"), (question, [], "A"), (question, ["A"], ""),
                     (question, [1], "A")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                owner_question_comment(*args)

    def test_missing_notification_channel_has_no_sender(self):
        self.assertIsNone(sender_for(Settings()))

    def test_group_contains_only_questions_and_ticket_links(self):
        message, included = _group([
            {"id": 1, "question": "Should this stay compatible?", "ticket_url": "https://jira.invalid/browse/JAIF-1"},
            {"id": 2, "question": "Should we wait a day?", "ticket_url": "https://jira.invalid/browse/JAIF-2"},
        ])

        self.assertEqual(included, [1, 2])
        self.assertEqual(message, "Should this stay compatible?\nhttps://jira.invalid/browse/JAIF-1\n\n"
                         "Should we wait a day?\nhttps://jira.invalid/browse/JAIF-2")

    def test_owner_answer_guard_is_registered_for_development_eligibility(self):
        self.assertEqual([rule.name for rule in DEVELOPMENT_ELIGIBILITY_PIPELINE], ["verified-owner-answer"])

    def test_escalation_guide_requires_a_verified_jira_answer_for_ticket_questions(self):
        guide = Path(__file__).resolve().parents[2] / "docs/v4/escalations.md"
        text = guide.read_text(encoding="utf-8")

        self.assertIn("The owner answers a ticket-linked question in Jira", text)
        self.assertIn("Before creating a development run, the next claim verifies", text)
        self.assertIn("The Command Center cannot answer or resume a ticket-linked question", text)
        self.assertNotIn("The owner picks an option (or the recommended default) on screen", text)
        self.assertNotIn("every waiting ticket loses the label and returns to To Do", text)

    def test_jira_comment_lookup_matches_adf_paragraphs_after_spacing_is_collapsed(self):
        comment_text = ("Should refunds be available for 14 days or 30 days?\n\nOptions:\n- 14 days\n- 30 days"
                        "\n\nRecommended option: 30 days\n\nAI Flow hand-off: D42")
        flattened = adf_to_text(text_to_adf(comment_text))
        self.assertNotEqual(flattened, comment_text)

        client = httpx.Client(
            base_url="https://jira.example.test",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"comments": [{
                "id": "comment-42", "created": "2026-10-24T09:00:00.000+0000",
                "body": text_to_adf(comment_text),
            }]})),
        )
        try:
            jira = Jira("https://jira.example.test", client=client)
            self.assertEqual(jira.find_comment("TESTWP-42", comment_text), {
                "id": "comment-42", "created": "2026-10-24T09:00:00.000+0000"})
        finally:
            client.close()

    def test_committed_comment_followed_by_503_is_not_retried_and_can_be_recovered(self):
        key, comment_text = "TESTWP-42", "Should the default stay compatible?\n\nOptions:\n- Keep it"
        created = "2026-10-24T09:00:00.000+0000"
        writes = []

        def respond(request):
            if request.method == "POST":
                payload = json.loads(request.content)
                writes.append(adf_to_text(payload["body"]))
                return httpx.Response(503, json={"error": "temporary server failure"})
            return httpx.Response(200, json={"startAt": 0, "total": 1, "comments": [{
                "id": "comment-42", "created": created, "body": text_to_adf(writes[0]),
            }]})

        client = httpx.Client(base_url="https://jira.example.test", transport=httpx.MockTransport(respond))
        try:
            jira = Jira("https://jira.example.test", client=client)
            with self.assertRaisesRegex(JiraError, "503"):
                jira.comment_with_metadata(key, comment_text)

            self.assertEqual(writes, [adf_to_text(text_to_adf(comment_text))])
            self.assertEqual(jira.find_comment(key, comment_text), {"id": "comment-42", "created": created})
            self.assertEqual(writes, [adf_to_text(text_to_adf(comment_text))])
        finally:
            client.close()

    def test_other_state_changing_jira_post_is_not_retried_after_503(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(503, json={"error": "temporary server failure"})

        client = httpx.Client(base_url="https://jira.example.test", transport=httpx.MockTransport(respond))
        try:
            jira = Jira("https://jira.example.test", client=client)
            with self.assertRaisesRegex(JiraError, "503"):
                jira.link_issues("TESTWP-1", "TESTWP-2")

            self.assertEqual(len(requests), 1)
        finally:
            client.close()

    def test_jira_search_post_retries_transient_failure(self):
        requests = []

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(503, json={"error": "temporary server failure"})
            return httpx.Response(200, json={"issues": [{"key": "TESTWP-1"}], "isLast": True})

        client = httpx.Client(base_url="https://jira.example.test", transport=httpx.MockTransport(respond))
        try:
            jira = Jira("https://jira.example.test", client=client)
            with patch("aiflow.v4.control.jira.time.sleep"):
                self.assertEqual(list(jira.search("project = TESTWP")), [{"key": "TESTWP-1"}])

            self.assertEqual(len(requests), 2)
        finally:
            client.close()

    def test_question_timestamp_match_normalizes_adf_paragraph_spacing(self):
        question = "Should refunds be available for 14 days or 30 days?"
        comment = owner_question_comment(question, ["14 days", "30 days"], "30 days", grouped=True)
        body = f"{comment}\n\nAI Flow hand-off: D42"
        created = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
        comments = [{"body": adf_to_text(text_to_adf(body)), "created": created.isoformat()}]
        row = {"ticket_question": question, "options": ["14 days", "30 days"], "recommended": "30 days",
               "id": 42, "run_id": "triage-run", "grouped": True,
               "escalation_created_at": datetime(2026, 10, 24, 8, tzinfo=timezone.utc)}

        self.assertIn("This ticket is grouped with an existing owner decision.", comment)
        self.assertIn("Answer in a Jira comment, then move the ticket to To Do.", comment)
        self.assertEqual(escalations._owner_question_body(row), comment)
        self.assertEqual(escalations._owner_question_time(comments, row), created)

    def test_legacy_question_timestamp_recovers_without_correlation_marker(self):
        question = "Should refunds be available for 14 days or 30 days?"
        comment = owner_question_comment(question, ["14 days", "30 days"], "30 days")
        created = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
        comments = [{"body": adf_to_text(text_to_adf(comment)), "created": created.isoformat()}]
        row = {"ticket_question": question, "options": ["14 days", "30 days"], "recommended": "30 days",
               "id": 42, "grouped": False, "legacy_question_comment": True,
               "escalation_created_at": datetime(2026, 10, 24, 8, tzinfo=timezone.utc)}

        self.assertEqual(escalations._owner_question_body(row), comment)
        self.assertEqual(escalations._owner_question_time(comments, row), created)

    def test_legacy_question_timestamp_rejects_comments_before_escalation_creation(self):
        question = "Should refunds be available for 14 days or 30 days?"
        comment = owner_question_comment(question, ["14 days", "30 days"], "30 days")
        created = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
        row = {"ticket_question": question, "options": ["14 days", "30 days"], "recommended": "30 days",
               "id": 42, "grouped": False, "legacy_question_comment": True,
               "escalation_created_at": created}
        comments = [{"body": comment, "created": (created - timedelta(seconds=1)).isoformat()}]

        self.assertIsNone(escalations._owner_question_time(comments, row))

    def test_jira_comment_history_paginates_without_truncating_bodies(self):
        long_text = "legacy question " + ("x" * 2100)
        pages = {
            0: [{"id": "first", "created": "2026-10-24T08:00:00.000+0000", "body": text_to_adf("first")}],
            1: [{"id": "long", "created": "2026-10-24T09:00:00.000+0000", "body": text_to_adf(long_text)}],
        }
        requests = []

        def respond(request):
            start = int(request.url.params["startAt"])
            requests.append(start)
            return httpx.Response(200, json={"startAt": start, "total": 2, "comments": pages[start]})

        client = httpx.Client(base_url="https://jira.example.test", transport=httpx.MockTransport(respond))
        try:
            jira = Jira("https://jira.example.test", client=client)
            comments = jira.all_comments("TESTWP-42", page_size=1)
            self.assertEqual(requests, [0, 1])
            self.assertEqual(comments[1]["body"], long_text)
            self.assertEqual(jira.find_comment("TESTWP-42", long_text, limit=1)["id"], "long")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
