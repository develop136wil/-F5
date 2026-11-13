"""정책 필드 자동 반영의 의미 검증(_validate_field_semantics) 회귀 테스트.

(2026-08-07) 실제 사고 사례 — id=13 "도봉구가족센터 다문화자녀 언어발달서비스"에서 AI
재추출이 대상특성 값("다문화")을 세부 연령 필드에 잘못 옮겨 적어("다문화자녀") 실제
운영 Notion에 그대로 반영됐다(사후 복구 완료). 기존 "필드 오배치" 감지(_content_moved)는
최소 10자 매칭을 요구해 짧은 태그성 오배치는 못 잡는 사각지대가 있었다. 이 사각지대를
막는 별도 검증과, 정상적인 비숫자 연령 표현까지 막지 않는지를 함께 검증한다.
"""
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from services import policy_sync as ps
from services import notion_service


class TestValidateFieldSemanticsUnit(unittest.TestCase):
    """순수 함수 단위 테스트 — DB/네트워크 불필요."""

    def test_age_detail_misplaced_target_characteristic_blocked(self):
        """실제 사고값 그대로 재현: 연령 표현이 전혀 없는 값은 경고를 받아야 한다."""
        warn = ps._validate_field_semantics("age_detail", "다문화자녀")
        self.assertIsNotNone(warn)
        self.assertIn("연령", warn)

    def test_age_detail_numeric_expression_passes(self):
        self.assertIsNone(ps._validate_field_semantics("age_detail", "만 12세 이하"))

    def test_age_detail_non_numeric_expressions_pass(self):
        """숫자가 없어도 정상적인 연령 표현은 막으면 안 된다(GPT 리뷰 지적)."""
        for value in ("영유아", "미취학 아동", "초등학생", "청소년", "성인", "임산부", "전 연령"):
            with self.subTest(value=value):
                self.assertIsNone(ps._validate_field_semantics("age_detail", value))

    def test_age_detail_empty_value_passes(self):
        """빈 값은 별도 빈값 처리 로직(7번 규칙)의 영역이므로 여기서 막지 않는다."""
        self.assertIsNone(ps._validate_field_semantics("age_detail", ""))

    def test_age_detail_numeric_range_and_infant_expressions_pass(self):
        for value in ("생후 36개월", "3~5세", "만 2세~만 5세"):
            with self.subTest(value=value):
                self.assertIsNone(ps._validate_field_semantics("age_detail", value))

    def test_age_detail_non_age_digit_values_blocked(self):
        """(2026-08-07, 2차 GPT Codex 리뷰) 처음 구현은 '숫자가 하나라도 있으면 통과'라
        나이와 무관한 숫자값도 오통과시켰다 — 재현 확인 후 수정, 회귀 고정."""
        for value in ("다문화 2자녀", "3인 가구", "2회 지원"):
            with self.subTest(value=value):
                self.assertIsNotNone(ps._validate_field_semantics("age_detail", value))

    def test_age_detail_bare_substring_no_longer_false_matches(self):
        """처음 구현은 '세'가 어디든 포함되면 통과라 '세계'의 '세'만 보고 나이와 무관한
        값도 오통과시켰다 — 이제 숫자+단위 조합이나 독립 토큰만 인정하므로, 진짜 나이
        정보가 없는 이 값은(이전 버그라면 통과했을 것) 정확히 차단돼야 한다."""
        self.assertIsNotNone(ps._validate_field_semantics("age_detail", "세계 최고 수준의 지원"))

    def test_age_detail_matches_target_characteristics_cross_field_blocked(self):
        """(2026-08-07, 2차 리뷰) id=13 사고의 핵심 패턴 — 세부연령 제안값이 '같은 진단'의
        대상특성 제안값과 겹치면, 연령 표현처럼 보이더라도(혹은 안 보이더라도) 필드가
        뒤바뀐 것으로 의심하고 차단해야 한다."""
        warn = ps._validate_field_semantics("age_detail", "다문화", target_characteristics_display="다문화")
        self.assertIsNotNone(warn)

    def test_support_detail_template_placeholder_blocked(self):
        """실제 사고값 그대로 재현: 헤더 아래 헤더명 재진술뿐인 값은 경고를 받아야 한다."""
        value = (
            "✅ 지원 내용\n- 언어발달서비스\n"
            "📋 신청 자격\n- 대상자 및 자격 요건\n"
            "📍 신청 방법\n- 방법 및 장소\n"
            "💰 지원 금액 및 혜택\n- 금액, 바우처, 서비스 내용\n"
            "📅 신청 기간\n- 접수기간\n"
            "📝 참고 사항\n- 기타 안내"
        )
        warn = ps._validate_field_semantics("support_detail", value)
        self.assertIsNotNone(warn)
        self.assertIn("항목명", warn)

    def test_support_detail_real_content_passes(self):
        """구체적 내용(금액·절차 등)이 있으면 짧아도 막지 않는다."""
        value = "✅ 지원 내용\n- 언어발달서비스 평가 및 맞춤형 언어교육 제공\n💵 비용 부담\n- 전액 무료"
        self.assertIsNone(ps._validate_field_semantics("support_detail", value))

    def test_other_fields_not_validated(self):
        """age_detail/support_detail 외 필드는 이 검증 대상이 아니다."""
        self.assertIsNone(ps._validate_field_semantics("eligibility_detail", "다문화자녀"))


class TestLowQualityExtractionFilter(unittest.TestCase):
    """의심 필드 하나 때문에 정상 추출 전체를 버리거나, 실제 빈값을 되살리지 않는지 검증."""

    def test_suspicious_age_falls_back_without_losing_other_changes(self):
        previous = {"age_detail": "만 12세 이하", "start_age_months": 0,
                    "end_age_months": 144, "eligibility_detail": "기존 요건"}
        extracted = {
            "age_detail": "다문화자녀",
            "start_age_months": 36,
            "end_age_months": 72,
            "target_characteristics": ["다문화"],
            "eligibility_detail": "새로 바뀐 요건",
        }

        cleaned, rejected = ps._filter_low_quality_extracted_fields(extracted, previous)

        self.assertEqual(cleaned["age_detail"], "만 12세 이하")
        self.assertEqual(cleaned["start_age_months"], 0)
        self.assertEqual(cleaned["end_age_months"], 144)
        self.assertEqual(cleaned["eligibility_detail"], "새로 바뀐 요건")
        self.assertIn("age_detail", rejected)

    def test_empty_value_is_preserved_as_possible_real_removal(self):
        cleaned, rejected = ps._filter_low_quality_extracted_fields(
            {"age_detail": "", "support_detail": "", "eligibility_detail": ""},
            {"age_detail": "영유아", "support_detail": "기존 지원 내용",
             "eligibility_detail": "기존 요건"},
        )

        self.assertEqual(cleaned["age_detail"], "")
        self.assertEqual(cleaned["support_detail"], "")
        self.assertEqual(cleaned["eligibility_detail"], "")
        self.assertEqual(rejected, {})

    def test_template_placeholder_falls_back_but_normal_age_survives(self):
        placeholder = (
            "✅ 지원 내용\n- 언어발달서비스\n"
            "📋 신청 자격\n- 대상자 및 자격 요건\n"
            "📍 신청 방법\n- 방법 및 장소\n"
            "💰 지원 금액 및 혜택\n- 금액, 바우처, 서비스 내용\n"
            "📅 신청 기간\n- 접수기간\n"
            "📝 참고 사항\n- 기타 안내"
        )
        cleaned, rejected = ps._filter_low_quality_extracted_fields(
            {"age_detail": "만 13세 이하", "support_detail": placeholder},
            {"age_detail": "만 12세 이하", "support_detail": "맞춤형 언어교육 제공"},
        )

        self.assertEqual(cleaned["age_detail"], "만 13세 이하")
        self.assertEqual(cleaned["support_detail"], "맞춤형 언어교육 제공")
        self.assertEqual(set(rejected), {"support_detail"})

    def test_rejected_field_without_fallback_is_removed_only(self):
        cleaned, rejected = ps._filter_low_quality_extracted_fields(
            {"age_detail": "세계 최고 수준의 지원", "target_income": "중위소득 100% 이하"})

        self.assertNotIn("age_detail", cleaned)
        self.assertEqual(cleaned["target_income"], "중위소득 100% 이하")
        self.assertIn("age_detail", rejected)


class TestRecomputeCleansStoredExtraction(unittest.IsolatedAsyncioTestCase):
    async def test_recompute_does_not_upgrade_unknown_extraction_provenance(self):
        for previous in ({}, {'extraction_meta': {'prompt_version': 'policy-extract-v1'}}):
            with self.subTest(previous=previous):
                row = dict(id=1, db_key='medical', notion_fields='{}',
                    last_extracted_fields='{"support_detail":"월 10만원 지급"}',
                    pending_diff=json.dumps(previous))
                writes = []
                async def capture(sql, params=()):
                    writes.append((sql, params))
                with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
                     patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
                     patch.object(ps, '_db_write_strict', new=capture), \
                     patch.object(ps, '_normalize_extracted_fields'), \
                     patch.object(ps, '_diff_fields', return_value=[{'field': '지원내용',
                        'old': '월 1만원 지급', 'new': '월 10만원 지급', 'level': 'high'}]), \
                     patch.object(ps, '_extraction_meta', side_effect=AssertionError('No new extraction')):
                    await ps.recompute_diffs(['medical'])
                saved = json.loads(next(params[0] for sql, params in writes if 'pending_diff=?' in sql))
                self.assertEqual(saved['extraction_meta'], previous.get('extraction_meta', {}))

    async def test_recompute_persists_field_level_cleanup(self):
        row = {
            "id": 13,
            "db_key": "medical",
            "notion_fields": json.dumps({
                "age_detail": "만 12세 이하", "eligibility_detail": "기존 요건"},
                ensure_ascii=False),
            "last_extracted_fields": json.dumps({
                "age_detail": "다문화자녀", "target_characteristics": ["다문화"],
                "eligibility_detail": "새 자격요건"}, ensure_ascii=False),
            "pending_diff": "{}",
        }
        writes = []

        async def capture_write(sql, params=()):
            writes.append((sql, params))

        with patch.object(ps, "_active_configured_keys", return_value=["medical"]), \
             patch.object(ps, "_db_query_strict", new=AsyncMock(return_value=[row])), \
             patch.object(ps, "_db_write_strict", new=capture_write), \
             patch.object(ps, "_normalize_extracted_fields"), \
             patch.object(ps, "_diff_fields", return_value=[]), \
             patch.object(ps.ai_classifier, "get_client", return_value=None):
            result = await ps.recompute_diffs(["medical"])

        cleanup = next(params for sql, params in writes if "last_extracted_fields=?" in sql)
        stored = json.loads(cleanup[0])
        self.assertEqual(stored["age_detail"], "만 12세 이하")
        self.assertEqual(stored["eligibility_detail"], "새 자격요건")
        self.assertEqual(result["resolved"], 1)


class TestApplyFieldBlocksSuspiciousValue(unittest.IsolatedAsyncioTestCase):
    """apply_field_to_notion()이 실제로 SUSPICIOUS_VALUE를 반환해 Notion PATCH를 막는지,
    force=True면 그래도 통과하는지 확인 — 실제 Notion 호출은 모킹한다."""

    def setUp(self):
        self.test_db = f'test_field_semantic_validation_{self._testMethodName}.db'
        database.close_all_connections()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()
        database.close_all_connections()
        database.init_db()

    def tearDown(self):
        database.close_all_connections()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    def _insert_policy(self, field, old, new):
        pd = {"intensity": "high", 'proposal_schema_version': 2, "extraction_meta": {"prompt_version": ps._EXTRACTION_PROMPT_VERSION}, "fields": [
            {"field": field, "old": old, "new": new, "level": "high", 'snapshot_current': True},
        ]}
        database.execute_write(
            """INSERT INTO notion_policies (db_key, page_id, title, notion_fields,
               change_status, pending_diff)
               VALUES (?, ?, ?, ?, 'detected', ?)""",
            ('medical', 'page-1', '테스트정책', json.dumps({}, ensure_ascii=False),
             json.dumps(pd, ensure_ascii=False)))
        row = database.execute_query(
            "SELECT id FROM notion_policies WHERE page_id='page-1'", fetch_one=True)
        return row['id']

    async def test_suspicious_age_value_blocked_without_force(self):
        """(핵심 회귀 방지) id=13 사고 재현 — force 없이는 절대 Notion PATCH가 나가면 안 된다."""
        policy_id = self._insert_policy("세부 연령", "만 12세 이하", "다문화자녀")
        with patch.object(notion_service, 'update_page_properties') as mock_patch:
            res = await ps.apply_field_to_notion(policy_id, "세부 연령", force=False)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "SUSPICIOUS_VALUE")
        mock_patch.assert_not_called()  # Notion에 실제로 안 나갔는지가 핵심

    async def test_suspicious_age_value_still_blocked_by_plain_force(self):
        """(2026-08-07, 2차 GPT Codex 리뷰) force(범위축소/팽창 우회)만으로는 의미검증을
        우회할 수 없어야 한다 — 서로 무관한 안전장치를 한 확인창으로 뭉뚱그리면 위험하다는
        지적 반영. force_semantic이 별도로 필요하다."""
        policy_id = self._insert_policy("세부 연령", "만 12세 이하", "다문화자녀")
        with patch.object(notion_service, 'update_page_properties') as mock_patch:
            res = await ps.apply_field_to_notion(policy_id, "세부 연령", force=True, force_semantic=False)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "SUSPICIOUS_VALUE")
        mock_patch.assert_not_called()

    async def test_semantic_override_cannot_discard_actual_age_condition(self):
        """A semantic override is not permission to erase a source condition."""
        policy_id = self._insert_policy("세부 연령", "만 12세 이하", "다문화자녀")
        with patch.object(notion_service, 'get_page', return_value={'properties': {
                '세부 연령': {'rich_text': [{'plain_text': '만 12세 이하'}]}}}), \
             patch.object(notion_service, 'update_page_properties', return_value={"ok": True}) as mock_patch:
            res = await ps.apply_field_to_notion(policy_id, "세부 연령", force_semantic=True)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get('error'), 'NEEDS_LINKED_REVIEW')
        mock_patch.assert_not_called()

    async def test_normal_age_value_not_blocked(self):
        policy_id = self._insert_policy("세부 연령", "만 12세 이하", "만 13세 이하")
        database.execute_write('UPDATE notion_policies SET source_texts=? WHERE id=?',
            (json.dumps({'https://example.invalid/policy': '지원대상: 만 13세 이하'}), policy_id))
        with patch.object(notion_service, 'get_page', return_value={'properties': {
                '세부 연령': {'id': 'age', 'type': 'rich_text', 'rich_text': [{'type': 'text', 'text': {'content': '만 12세 이하'}, 'plain_text': '만 12세 이하'}]}}}), \
             patch.object(notion_service, 'update_page_properties', return_value={"ok": True}):
            res = await ps.apply_field_to_notion(policy_id, "세부 연령", force=False)
        self.assertTrue(res.get("ok"))


class TestDiffFieldsAttachesValidationWarning(unittest.TestCase):
    """_diff_fields()가 진단 생성 시점에 미리 validation_warning을 붙이는지 확인 —
    사용자가 '적용' 버튼을 누르기 전 화면에서부터 알 수 있어야 하므로."""

    def test_misplaced_age_gets_validation_warning_at_diagnosis_time(self):
        notion_fields = {"age_detail": "만 12세 이하", "target_characteristics": ["다문화"]}
        new_fields = {"age_detail": "다문화자녀", "target_characteristics": ["다문화"]}
        diffs = ps._diff_fields(notion_fields, new_fields, client=None)
        age_diff = next(d for d in diffs if d["field"] == ps._PROP_LABEL["age_detail"])
        self.assertIn("validation_warning", age_diff)

    def test_normal_age_change_has_no_validation_warning(self):
        notion_fields = {"age_detail": "만 12세 이하"}
        new_fields = {"age_detail": "영유아"}
        diffs = ps._diff_fields(notion_fields, new_fields, client=None)
        age_diff = next(d for d in diffs if d["field"] == ps._PROP_LABEL["age_detail"])
        self.assertNotIn("validation_warning", age_diff)


if __name__ == '__main__':
    unittest.main()
