"""Synthetic automatic styling: no AI, Notion, or user-data requests."""
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from services import policy_sync as ps, policy_full_check, policy_review_store, policy_undo


class AutomaticPolicyStyle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.guide = "간결한 문장"
        self.label = ps._PROP_LABEL["eligibility_detail"]
        self.rows = {}
        self.reviews = {}
        self.receipts = []
        previous = copy.deepcopy(ps.STYLE_STATUS)
        parent = copy.deepcopy(ps.ONE_CLICK_STATUS)
        self.addCleanup(lambda: (ps.STYLE_STATUS.clear(), ps.STYLE_STATUS.update(previous),
                                 ps.ONE_CLICK_STATUS.clear(), ps.ONE_CLICK_STATUS.update(parent)))
        self.stack.enter_context(patch.object(ps.utils, "get_env_value", side_effect=lambda key:
            self.guide if key == "POLICY_STYLE_GUIDE" else ""))
        self.stack.enter_context(patch.object(ps, "_active_configured_keys", return_value=["test"]))
        self.stack.enter_context(patch.object(ps.database, "execute_query", side_effect=lambda *a, **k:
            copy.deepcopy(list(self.rows.values()))))
        self.stack.enter_context(patch.object(ps, "_db_query_strict", new=AsyncMock(side_effect=lambda *a, **k:
            copy.deepcopy(list(self.rows.values())))))
        self.stack.enter_context(patch.object(policy_review_store, "unresolved_by_policy",
                                              side_effect=lambda *a: self.reviews))
        self.stack.enter_context(patch.object(policy_undo, "applied_records", side_effect=lambda *a, **k:
                                              copy.deepcopy(self.receipts)))

        async def run_db(function, *args, **kwargs):
            return function(*args, **kwargs)

        async def save(sql, params):
            self.rows[params[1]]["pending_diff"] = params[0]

        self.stack.enter_context(patch.object(ps.database, "run_db", new=run_db))
        self.write = self.stack.enter_context(patch.object(ps, "_db_write_strict", new=AsyncMock(side_effect=save)))
        self.client = self.stack.enter_context(patch.object(ps.ai_classifier, "get_client", return_value=object()))
        self.ai = self.stack.enter_context(patch.object(ps.ai_classifier, "_call_gemini_json",
                                                       new=AsyncMock(return_value={self.label: "소득 50% 이하"})))
        self.pause = self.stack.enter_context(patch.object(policy_full_check, "ai_pause_reason", return_value=""))
        ps.ONE_CLICK_STATUS.update(running=True, phase="style")

    def add_policy(self, policy_id=1, **field_values):
        field = dict(field=self.label, old="기존 조건", new="소득 50% 이하", level="high",
                     snapshot_current=True)
        field.update(field_values)
        proposal = dict(fields=[field], proposal_schema_version=ps._proposal_schema_version(),
                        extraction_meta={"prompt_version": ps._EXTRACTION_PROMPT_VERSION})
        self.rows[policy_id] = dict(id=policy_id, db_key="test", page_id=f"page-{policy_id}",
            title=f"시험 정책 {policy_id}", pending_diff=json.dumps(proposal, ensure_ascii=False),
            dead_links="[]", source_urls="[]", last_changed_at="2026-01-01", change_status="detected")
        return field

    def proposal(self, policy_id=1):
        return json.loads(self.rows[policy_id]["pending_diff"])

    def replace_proposal(self, proposal, policy_id=1):
        self.rows[policy_id]["pending_diff"] = json.dumps(proposal, ensure_ascii=False)

    async def test_success_is_saved_immediately_and_never_restyled(self):
        self.add_policy()
        result = await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(result["styled"], 1)
        field = self.proposal()["fields"][0]
        self.assertEqual(field["styled"], "소득 50% 이하")
        self.assertEqual(field["_auto_style"]["status"], "styled")
        self.assertEqual(ps.ONE_CLICK_STATUS["checked"], 1)
        self.assertEqual(ps.ONE_CLICK_STATUS["total"], 1)
        self.assertEqual(ps.preview_style_work(["test"])["eligible_count"], 0)
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(self.ai.await_count, 1)
        self.assertEqual(self.ai.await_args.kwargs['request_attempts'], 1)
        self.assertIn('verified_input', self.ai.await_args.kwargs)
        self.assertIn('model_override', self.ai.await_args.kwargs)
        self.assertEqual(self.write.await_count, 1)

    async def test_rejected_output_is_preserved_without_repeated_ai(self):
        self.add_policy()
        self.ai.return_value = {self.label: "소득 80% 이하"}
        first = await ps.restyle_pending_diffs(["test"], only_needed=True)
        second = await ps.restyle_pending_diffs(["test"], only_needed=True)
        field = self.proposal()["fields"][0]
        self.assertEqual(first["rejected_fields"], 1)
        self.assertEqual(second["styled"], 0)
        self.assertEqual(field["new"], "소득 50% 이하")
        self.assertNotIn("styled", field)
        self.assertEqual(field["_auto_style"]["status"], "rejected")
        self.assertEqual(self.ai.await_count, 1)

    async def test_invalid_or_missing_response_is_attempted_once(self):
        for result in (None, {}, {self.label: None}):
            with self.subTest(result=result):
                self.rows.clear()
                self.add_policy()
                self.ai.reset_mock()
                self.ai.return_value = result
                await ps.restyle_pending_diffs(["test"], only_needed=True)
                await ps.restyle_pending_diffs(["test"], only_needed=True)
                self.assertEqual(self.ai.await_count, 1)
                self.assertNotIn("styled", self.proposal()["fields"][0])

    async def test_changed_guide_or_source_can_retry_rejection_but_timestamps_cannot(self):
        self.add_policy()
        self.ai.return_value = {}
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        original = self.proposal()["fields"][0]["_auto_style"]["fingerprint"]
        self.rows[1]["last_changed_at"] = "2026-09-17"
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(self.ai.await_count, 1)
        self.guide = "다른 문체"
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(self.ai.await_count, 2)
        field = self.proposal()["fields"][0]
        self.assertNotEqual(field["_auto_style"]["fingerprint"], original)
        self.assertEqual(set(field["_auto_style"]), {"fingerprint", "status"})
        proposal = self.proposal()
        proposal["fields"][0]["new"] = "소득 60% 이하"
        self.replace_proposal(proposal)
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(self.ai.await_count, 3)

    async def test_existing_sparse_output_and_applied_fields_are_untouched(self):
        sparse = self.add_policy(styled="소득 50% 이하", note="sparse")
        applied = self.add_policy(2, applied=True, styled="기존 반영 값")
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(self.proposal()["fields"][0], sparse)
        self.assertEqual(self.proposal(2)["fields"][0], applied)
        self.ai.assert_not_awaited()
        self.write.assert_not_awaited()

    async def test_applied_field_in_mixed_policy_cannot_be_rewritten_by_ai(self):
        self.add_policy()
        applied = dict(field=ps._PROP_LABEL["support_detail"], old="기존", new="50% 할인",
                       styled="반영 완료", level="high", applied=True)
        proposal = self.proposal()
        proposal["fields"].append(copy.deepcopy(applied))
        self.replace_proposal(proposal)
        self.ai.return_value[applied["field"]] = "100% 할인"
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(self.proposal()["fields"][1], applied)

    async def test_stale_snapshots_extraction_and_source_holds_block_automatic_ai(self):
        for hold in ("schema", "extraction", "snapshot", "source", "receipt"):
            with self.subTest(hold=hold):
                self.rows.clear()
                self.reviews.clear()
                self.receipts.clear()
                self.add_policy()
                proposal = self.proposal()
                if hold == "schema":
                    proposal["proposal_schema_version"] = -1
                elif hold == "extraction":
                    proposal["extraction_meta"]["prompt_version"] = "old"
                elif hold == "snapshot":
                    proposal["fields"][0]["snapshot_current"] = False
                elif hold == "source":
                    self.reviews[1] = [{"code": "source_review"}]
                else:
                    self.receipts.append(dict(id="receipt", page_id="page-1", field="other",
                        before_value="before", after_value="after", needs_verification=True))
                self.replace_proposal(proposal)
                before = self.rows[1]["pending_diff"]
                self.assertEqual(ps.preview_style_work(["test"])["eligible_count"], 0)
                await ps.restyle_pending_diffs(["test"], only_needed=True)
                self.assertEqual(self.rows[1]["pending_diff"], before)
        self.ai.assert_not_awaited()
        self.write.assert_not_awaited()

    async def test_matching_applied_receipt_blocks_duplicate_pending_field(self):
        self.add_policy()
        self.receipts.append(dict(id="receipt", page_id="page-1", field=self.label,
            before_value="기존 조건", after_value="소득 50% 이하", needs_verification=False))
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.ai.assert_not_awaited()
        self.write.assert_not_awaited()

    async def test_uncertain_receipt_is_blocked_even_if_card_says_apply_ready(self):
        field = self.add_policy()
        with patch.object(ps, "get_changes", return_value=[dict(
                id=1, apply_ready=True, fields=[field], apply_history=[{"needs_verification": True}])]):
            await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.ai.assert_not_awaited()
        self.write.assert_not_awaited()

    async def test_quota_preflight_does_not_send_or_mark_any_proposal(self):
        self.add_policy()
        untouched = self.rows[1]["pending_diff"]
        self.pause.return_value = "local_budget_exhausted"
        result = await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertTrue(result["paused"])
        self.assertEqual(self.rows[1]["pending_diff"], untouched)
        self.ai.assert_not_awaited()
        self.write.assert_not_awaited()

    async def test_quota_pause_keeps_unfinished_policy_and_prior_success(self):
        self.add_policy()
        self.add_policy(2)
        untouched = self.rows[2]["pending_diff"]
        self.pause.side_effect = ["", "local_budget_exhausted"]
        result = await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertTrue(result["paused"])
        self.assertEqual(result["pause_reason"], "local_budget_exhausted")
        self.assertEqual(result["styled"], 1)
        self.assertIn("styled", self.proposal()["fields"][0])
        self.assertEqual(self.rows[2]["pending_diff"], untouched)
        self.assertEqual(self.ai.await_count, 1)

    async def test_storage_failure_is_visible_and_keeps_previously_saved_output(self):
        self.add_policy()
        self.add_policy(2)
        untouched = self.rows[2]["pending_diff"]
        save = self.write.side_effect

        async def fail_second(sql, params):
            if params[1] == 2:
                raise OSError("synthetic disk failure")
            await save(sql, params)

        self.write.side_effect = fail_second
        with self.assertRaises(OSError):
            await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertIn("styled", self.proposal()["fields"][0])
        self.assertEqual(self.rows[2]["pending_diff"], untouched)
        self.assertFalse(ps.STYLE_STATUS["running"])

    async def test_quota_failure_does_not_mark_unfinished_fields_processed(self):
        self.add_policy()
        untouched = self.rows[1]["pending_diff"]
        self.pause.side_effect = ["", "daily_limit"]
        self.ai.side_effect = RuntimeError("quota exhausted")
        result = await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertTrue(result["paused"])
        self.assertEqual(self.rows[1]["pending_diff"], untouched)
        self.write.assert_not_awaited()

    async def test_nonquota_failure_is_bounded_and_manual_retry_still_works(self):
        self.add_policy()
        self.ai.side_effect = RuntimeError("synthetic failure")
        result = await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["paused"])
        self.assertEqual(self.proposal()["fields"][0]["_auto_style"]["status"], "failed")
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(self.ai.await_count, 2)
        self.ai.side_effect = None
        await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertEqual(self.ai.await_count, 3)
        self.assertIn("styled", self.proposal()["fields"][0])

    async def test_missing_guide_and_preview_have_no_side_effects(self):
        self.add_policy()
        self.assertEqual(ps.preview_style_work(["test"]),
                         {"configured": True, "eligible_count": 1, "eligible_fields": 1})
        self.guide = ""
        self.assertFalse(ps.preview_style_work(["test"])["configured"])
        result = await ps.restyle_pending_diffs(["test"], only_needed=True)
        self.assertTrue(result["ok"])
        self.client.assert_not_called()
        self.ai.assert_not_awaited()
        self.write.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
