"""policy_sync.py 순수 함수 단위 테스트.

DB/네트워크에 의존하지 않는 로직(diff 판정, 섹션 클램핑, 태그 정규화, 연령 정합성)을
검증한다. AI(임베딩) 호출이 필요한 경로는 client에 항상 실패하는 더미 객체를 넘겨
(client=object()) 환경의 실제 API 키 유무와 무관하게 네트워크 호출 없이 결정론적으로
동작하도록 한다 — 진짜 API 클라이언트를 넘기면 환경에 키가 있을 때 실제 과금 호출이
나갈 수 있어 절대 쓰지 않는다. `python -m unittest tests.test_policy_sync` 로 실행.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import policy_sync as ps

# 임베딩이 필요한 비교 분기에서도 절대 실제 API를 타지 않도록 하는 더미 클라이언트.
_NO_AI_CLIENT = object()


class TestClampSectionsToOld(unittest.TestCase):
    """(버그 2026-07-16) 짧은 '이모지헤더 - 내용' 한 줄이 헤더/본문 분리 전에
    통째로 헤더로 오판되어 클램핑이 무효화되던 문제의 회귀 테스트."""

    def test_short_header_with_body_still_clamped(self):
        old = "✅ 지원 내용\n다문화가족 자녀의 언어발달 서비스\n📝 참고 사항\n기타 안내"
        new = ("### ✅ 지원 내용 - 다문화가족 자녀의 언어발달 수준 평가 및 맞춤형 언어교육 제공 "
               "### 📋 신청 장소/방법/절차 - 가족센터 방문 신청 → 온라인 신청 → 초기 면담 "
               "### 💰 비용 부담 - 전액 무료 "
               "### 📝 참고 사항 - 주 2회, 총 6개월")
        out = ps._sanitize_support_detail(new, old)
        self.assertNotIn("###", out)
        self.assertNotIn("신청 장소", out, "기존에 없던 섹션은 제거되어야 한다")
        self.assertNotIn("비용 부담", out, "기존에 없던 섹션은 제거되어야 한다")
        self.assertIn("지원 내용", out)
        self.assertIn("참고 사항", out)

    def test_no_headers_in_old_means_no_clamping(self):
        """기존 값에 헤더가 아예 없으면(자유서술) 클램핑하지 않고 원문을 그대로 둔다."""
        old = "공통부모교육 - 영유아 자녀를 둔 모든 양육자 대상"
        new = "### 📋 상세 - 아주 긴 카탈로그성 설명" * 3
        out = ps._clamp_sections_to_old(new, old)
        self.assertEqual(out, new)

    def test_fully_disjoint_headers_falls_back_to_original(self):
        """새 제안의 헤더가 기존과 하나도 안 겹치면(전부 잘림) 정보 손실 대신 원본 유지."""
        old = "✅ 지원 내용\n원래 내용"
        new = "📋 완전히 다른 헤더\n다른 내용"
        out = ps._clamp_sections_to_old(new, old)
        self.assertEqual(out, new)


class TestSanitizeSupportDetail(unittest.TestCase):
    def test_sparse_note_when_clamp_loses_too_much(self):
        old = "✅ 지원 내용\n언어발달서비스\n📝 참고 사항\n기타 안내"
        new = ("### ✅ 지원 내용 - 언어발달서비스 "
               "### 📋 신청 장소/방법/절차 - 가족센터 방문 신청 → 온라인 신청 → 초기 면담 및 평가 → 수업배정까지 상세 진행 절차 안내 "
               "### 💰 비용 부담 - 전액 무료이며 예산 소진 시 지원금이 중단될 수 있음 안내 사항 포함 "
               "### 📝 참고 사항 - 기타 안내")
        out, note = ps._sanitize_support_detail(new, old, return_note=True)
        self.assertEqual(note, 'sparse')
        self.assertNotIn("###", out, "sparse여도 마크다운 기호는 제거되어야 한다")

    def test_bloat_note_when_far_longer_than_old(self):
        old = "공통부모교육 - 영유아 자녀를 둔 모든 양육자 대상 부모교육 진행"
        new = "공통부모교육 - " + ("부모인식개선교육, 큰믿책 교육 등 다양한 주제의 부모교육 진행. " * 20)
        out, note = ps._sanitize_support_detail(new, old, return_note=True)
        self.assertEqual(note, 'bloat')

    def test_no_note_for_reasonable_change(self):
        old = "✅ 지원 내용\n기존 설명"
        new = "✅ 지원 내용\n약간 더 자세해진 설명"
        out, note = ps._sanitize_support_detail(new, old, return_note=True)
        self.assertIsNone(note)

    def test_return_note_false_by_default_keeps_string_return(self):
        """기존 호출부(restyle/apply)와의 하위호환 — return_note 생략 시 문자열만 반환."""
        out = ps._sanitize_support_detail("### 지원 내용", "지원 내용")
        self.assertIsInstance(out, str)
        self.assertNotIn("###", out)


class TestAgeLifecycleMismatch(unittest.TestCase):
    def test_implied_stages_infant_range(self):
        self.assertEqual(ps._implied_stages(0, 20), {"영아"})

    def test_implied_stages_spanning_multiple(self):
        stages = ps._implied_stages(0, 216)
        self.assertEqual(stages, {"영아", "유아", "학령기"})

    def test_implied_stages_missing_values(self):
        self.assertEqual(ps._implied_stages(None, 20), set())
        self.assertEqual(ps._implied_stages(10, None), set())

    def test_mismatch_note_flags_missing_stage(self):
        fields = {"start_age_months": 0, "end_age_months": 216, "life_cycle": ["유아", "학령기"]}
        note = ps._age_lifecycle_mismatch_note(fields)
        self.assertIsNotNone(note)
        self.assertIn("영아", note)

    def test_no_note_when_consistent(self):
        fields = {"start_age_months": 0, "end_age_months": 216, "life_cycle": ["영아", "유아", "학령기"]}
        self.assertIsNone(ps._age_lifecycle_mismatch_note(fields))

    def test_extra_stage_flagged_when_age_does_not_support_it(self):
        """(2026-07-20 실측, 두리활동) 만 8~13세(96~156개월=학령기만)인데 life_cycle에
        영아·유아가 잘못 붙어있으면 과잉 추가로 안내해야 한다 — 기존엔 누락만 검사해서
        이 케이스를 완전히 놓쳤다."""
        fields = {"start_age_months": 96, "end_age_months": 156, "life_cycle": ["영아", "유아", "학령기"]}
        note = ps._age_lifecycle_mismatch_note(fields)
        self.assertIsNotNone(note)
        self.assertIn("영아", note)
        self.assertIn("유아", note)

    def test_no_note_when_life_cycle_missing_entirely(self):
        """생애주기 추출 자체가 안 됐으면(빈 값) 비교 의미가 없어 안내하지 않는다."""
        fields = {"start_age_months": 0, "end_age_months": 216, "life_cycle": []}
        self.assertIsNone(ps._age_lifecycle_mismatch_note(fields))


class TestDiffFieldsNoAiPaths(unittest.TestCase):
    """cosine 계산이 필요 없는(값이 같거나, 한쪽이 비었거나, 월령이 숫자로 정확히
    일치하는) 분기만 검증 — AI 호출 자체가 발생하지 않는 경로."""

    def test_identical_values_produce_no_diff(self):
        fields = {"life_cycle": ["영아"], "residence": ["도봉구"], "target_characteristics": ["일반"],
                   "category": "복지", "age_detail": "0~12개월", "eligibility_detail": "동일",
                   "target_income": "동일", "support_detail": "동일 내용"}
        diffs = ps._diff_fields(fields, dict(fields), _NO_AI_CLIENT)
        self.assertEqual(diffs, [])

    def test_numeric_age_match_is_low_even_if_wording_differs(self):
        old = {"start_age_months": 0, "end_age_months": 24, "age_detail": "0~2세"}
        new = {"start_age_months": 0, "end_age_months": 24, "age_detail": "만 2세 이하"}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        age_diff = next(d for d in diffs if d["field"] == "세부 연령")
        self.assertEqual(age_diff["level"], "low")

    def test_residence_overlap_is_low(self):
        old = {"residence": ["전국", "서울시", "도봉구"]}
        new = {"residence": ["도봉구"]}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        r = next(d for d in diffs if d["field"] == "거주지")
        self.assertEqual(r["level"], "low")

    def test_residence_no_overlap_is_high(self):
        old = {"residence": ["도봉구"]}
        new = {"residence": ["강남구"]}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        r = next(d for d in diffs if d["field"] == "거주지")
        self.assertEqual(r["level"], "high")

    def test_empty_new_value_demotes_high_to_info(self):
        """한쪽만 비어있는 실질변경은 '삭제'가 아니라 '보강 후보(info)'로 강등."""
        old = {"eligibility_detail": "만 12세 이하 다자녀 가정"}
        new = {"eligibility_detail": ""}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        d = next(d for d in diffs if d["field"] == "추가 자격요건")
        self.assertEqual(d["level"], "info")

    def test_target_characteristics_included_in_t1_pool(self):
        """(2026-07-15) 대상특성 문구가 대상연령 필드로 잘못 이동한 오탐을 info로 강등.
        주의: _content_moved는 10자 미만 짧은 값은 겹침 임계값(10자 이상)을 못 넘어
        절대 매칭되지 않는다 — 그래서 문장형 값으로 검증한다(짧은 단어형 오배치의
        잔여 한계는 별도로 보고)."""
        moved_text = "다문화가족 및 외국인 근로자 자녀 우선 지원 대상"
        old = {"age_detail": "만 12세 이하", "target_characteristics": ["일반"]}
        new = {"age_detail": moved_text, "target_characteristics": ["일반", moved_text]}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        age_diff = next(d for d in diffs if d["field"] == "세부 연령")
        self.assertEqual(age_diff["level"], "info", "대상특성 풀에서 매칭되어 이동으로 처리되어야 한다")


class TestClampResidence(unittest.TestCase):
    """(2026-07-20 실측) AI가 원문 '도봉 및 인접지역'에서 '인접지역'을 지역 태그로 추출해
    그대로 제안된 사고의 회귀 테스트. Notion multi_select는 없는 옵션을 쓰면 새 태그를
    자동 생성하므로 적용 전 반드시 걸러야 한다."""

    def test_unknown_tag_removed_with_fallback(self):
        out = ps._clamp_residence(["도봉구", "인접지역"])
        self.assertEqual(out, ["도봉구"])

    def test_schema_options_win_over_fallback(self):
        """사용자 Notion DB에 실제로 있는 옵션이면(스키마 제공) 유지된다 — 스키마 기반."""
        out = ps._clamp_residence(["도봉구", "인접지역"], valid=["전국", "도봉구", "인접지역"])
        self.assertEqual(out, ["도봉구", "인접지역"])

    def test_normalize_pipeline_strips_unknown_residence(self):
        fields = {"residence": ["도봉구", "인접지역"]}
        ps._normalize_extracted_fields(fields)
        self.assertEqual(fields["residence"], ["도봉구"])


class TestNarrowingNote(unittest.TestCase):
    """(2026-07-20) 태그 축소 제안('막내 만18세 이하' 정책에서 영아 탈락 실측) 안내 배지."""

    def test_strict_subset_gets_narrowing_note(self):
        old = {"life_cycle": ["영아", "유아", "학령기"]}
        new = {"life_cycle": ["유아", "학령기"]}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        d = next(d for d in diffs if d["field"] == "생애주기")
        self.assertEqual(d.get("note"), "narrowing")

    def test_addition_or_disjoint_has_no_narrowing_note(self):
        old = {"residence": ["도봉구"]}
        new = {"residence": ["도봉구", "서울시"]}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        d = next(d for d in diffs if d["field"] == "거주지")
        self.assertIsNone(d.get("note"))

    def test_money_date_signal_never_gets_narrowing_note(self):
        """지원금액·기간(파생 신호)은 태그 필드가 아니므로 축소 안내 대상이 아니다."""
        old = {"support_detail": "지원금 50만원, 30만원"}
        new = {"support_detail": "지원금 50만원"}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        d = next(d for d in diffs if d["field"] == "지원금액·기간")
        self.assertNotEqual(d.get("note"), "narrowing")


class TestMismatchNoteUsesNotionMonthsFallback(unittest.TestCase):
    def test_note_attached_when_new_months_missing(self):
        """새 추출에 월령이 없어도 Notion 기존 월령(0~216)으로 판정해, 생애주기에서
        영아가 빠진 제안에 불일치 안내가 붙는다 (다둥이 행복카드 실측 패턴)."""
        old = {"start_age_months": 0, "end_age_months": 216,
               "life_cycle": ["영아", "유아", "학령기"]}
        new = {"start_age_months": None, "end_age_months": None,
               "life_cycle": ["유아", "학령기"]}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        d = next(d for d in diffs if d["field"] == "생애주기")
        self.assertIn("영아", d.get("consistency_note") or "")


class TestLabelsMatchNotionProps(unittest.TestCase):
    def test_diff_labels_are_real_notion_property_names(self):
        """diff 카드 제목이 실제 Notion 속성명과 글자 그대로 일치해야 한다(사용자 실측
        지적 2026-07-20: '대상특성'·'자격요건' 등 축약형이라 Notion과 대조 어려움)."""
        from services import notion_service
        prop_names = {pname for _, pname, _ in notion_service.POLICY_PROPS}
        old = {"residence": ["도봉구"], "life_cycle": ["영아"], "target_characteristics": ["일반"],
               "category": "복지", "age_detail": "0~12개월", "eligibility_detail": "요건A",
               "target_income": "소득 무관", "support_detail": "내용A 50만원"}
        new = {"residence": ["강남구"], "life_cycle": ["유아"], "target_characteristics": ["임산부"],
               "category": "의료", "age_detail": "13~24개월", "eligibility_detail": "요건B",
               "target_income": "중위 100%", "support_detail": "내용B 30만원"}
        diffs = ps._diff_fields(old, new, _NO_AI_CLIENT)
        for d in diffs:
            if d["field"] == "지원금액·기간":   # 파생 신호 — Notion 속성 아님(표시도 제외됨)
                continue
            self.assertIn(d["field"], prop_names,
                          f"'{d['field']}'는 실제 Notion 속성명이 아니다")

    def test_label_to_field_accepts_legacy_labels(self):
        """라벨 개편 전에 저장된 pending_diff(구 축약 라벨)도 적용 경로에서 인식돼야 한다."""
        for legacy in ("대상특성", "대상연령", "자격요건", "대상소득", "상세지원내용"):
            self.assertIsNotNone(ps._label_to_field(legacy))


class TestRevertHashesOnFailedAnalysis(unittest.TestCase):
    """(2026-08-05, GPT Codex 리뷰로 발견한 실제 버그의 회귀 테스트)
    AI 재추출이 429 등으로 실패(None)하면 감지된 변경을 다음 점검에서도 다시 잡아낼 수
    있도록 해시·본문을 되돌려야 한다. 되돌리지 않으면 그 변경을 영구히 놓친다."""

    def test_changed_urls_are_reverted_to_old_hash(self):
        old_hashes = {"https://a.example": "hash_old_a", "https://b.example": "hash_old_b"}
        old_texts = {"https://a.example": "옛날 내용 A", "https://b.example": "옛날 내용 B"}
        # 점검 중 두 URL 모두 원문이 바뀌어 새 해시로 이미 계산돼 있는 상태(정상 흐름)
        new_hashes = {"https://a.example": "hash_new_a", "https://b.example": "hash_new_b"}
        new_texts = {"https://a.example": "새 내용 A", "https://b.example": "새 내용 B"}
        # a만 실제 변경으로 판정되어 AI 분석 대상이 됐고(b는 사소한 변경이라 애초에 후보 아님)
        real_changes = [("https://a.example", "옛날 내용 A", "새 내용 A")]

        ps._revert_hashes_for_unanalyzed_change(new_hashes, new_texts, old_hashes, old_texts, real_changes)

        # AI 분석 대상이었던 a는 다음 점검에서 다시 "변경됨"으로 잡히도록 옛 해시로 복귀
        self.assertEqual(new_hashes["https://a.example"], "hash_old_a")
        self.assertEqual(new_texts["https://a.example"], "옛날 내용 A")
        # 분석 대상이 아니었던 b는 정상적으로 새 해시 유지(불필요하게 되돌리지 않음)
        self.assertEqual(new_hashes["https://b.example"], "hash_new_b")
        self.assertEqual(new_texts["https://b.example"], "새 내용 B")

    def test_no_change_when_real_changes_empty(self):
        new_hashes = {"https://a.example": "hash_new_a"}
        new_texts = {"https://a.example": "새 내용 A"}
        original_hashes = dict(new_hashes)
        original_texts = dict(new_texts)
        ps._revert_hashes_for_unanalyzed_change(new_hashes, new_texts, {}, {}, [])
        self.assertEqual(new_hashes, original_hashes)
        self.assertEqual(new_texts, original_texts)

    def test_reverted_url_with_no_prior_baseline_falls_back_to_empty(self):
        """최초 점검(old_hashes에 해당 URL이 아예 없던 경우)에 실패하면, 다음 점검에서
        '최초 기준선'으로 다시 취급되어야 하므로 빈 문자열로 안전하게 폴백해야 한다."""
        new_hashes = {"https://new.example": "hash_new"}
        new_texts = {"https://new.example": "새로 생긴 내용"}
        real_changes = [("https://new.example", "", "새로 생긴 내용")]
        ps._revert_hashes_for_unanalyzed_change(new_hashes, new_texts, {}, {}, real_changes)
        self.assertIsNone(new_hashes["https://new.example"])
        self.assertEqual(new_texts["https://new.example"], "")


class TestPolicyPropMapOverride(unittest.TestCase):
    """(2026-07-30) NOTION_POLICY_PROP_MAP 오버라이드 — 다른 지자체가 Notion 속성명을
    다르게 지어도 코드 수정·재빌드 없이 대응 가능한지 검증. 오버라이드가 없으면 기존
    하드코딩 기본값과 완전히 동일하게 동작해야 한다(하위호환)."""

    def setUp(self):
        from services import notion_service
        self.notion_service = notion_service

    def test_no_override_returns_defaults(self):
        with patch('utils.get_env_value', return_value=None):
            self.assertEqual(self.notion_service.get_policy_props(), self.notion_service.POLICY_PROPS)
            self.assertEqual(self.notion_service.get_policy_source_props(), self.notion_service.POLICY_SOURCE_PROPS)

    def test_non_string_value_in_override_falls_back_for_that_key(self):
        """(GPT 리뷰 2차, 2026-08-05) 최상위는 dict라도 값이 문자열이 아니면(예: 리스트)
        이후 Notion 속성 딕셔너리 키로 쓰일 때 'unhashable type: list'로 죽던 버그의
        재현 테스트. 문자열이 아닌/빈 값은 무시하고 그 키만 기본값으로 폴백해야 한다."""
        import json
        override = json.dumps({"title": ["정책명"], "category": "  ", "residence": "지역"})
        with patch('utils.get_env_value', return_value=override):
            props = dict((fkey, pname) for fkey, pname, _ in self.notion_service.get_policy_props())
            self.assertEqual(props["title"], "사업명")      # 리스트 값 → 기본값 폴백
            self.assertEqual(props["category"], "분류")      # 공백뿐인 값 → 기본값 폴백
            self.assertEqual(props["residence"], "지역")     # 정상 문자열 값 → 오버라이드 적용

    def test_override_replaces_only_mapped_keys(self):
        import json
        override = json.dumps({"title": "정책명", "category": "구분", "_source_1": "홈페이지1"})
        with patch('utils.get_env_value', return_value=override):
            props = dict((fkey, pname) for fkey, pname, _ in self.notion_service.get_policy_props())
            self.assertEqual(props["title"], "정책명")
            self.assertEqual(props["category"], "구분")
            # 오버라이드하지 않은 키는 기본값 그대로 유지되어야 한다
            self.assertEqual(props["residence"], "거주지")

            sources = self.notion_service.get_policy_source_props()
            self.assertEqual(sources[0], "홈페이지1")
            self.assertEqual(sources[1], "관련 홈페이지 2")  # 미지정 → 기본값 유지

    def test_override_propagates_to_diff_labels_live(self):
        """_PROP_LABEL/_label_to_field가 매 호출마다 캐시 없이 다시 조회하는지 확인
        (utils.get_env_value 자체는 프로세스 캐시가 있어, 실제 config.json 직접 편집
        시나리오에서는 별개로 앱 재시작이 필요함 — 이 테스트는 함수 레벨 무캐시 여부만 검증)."""
        import json
        override = json.dumps({"category": "정책구분"})
        with patch('utils.get_env_value', return_value=override):
            self.assertEqual(ps._PROP_LABEL["category"], "정책구분")
            self.assertEqual(ps._label_to_field("정책구분"), "category")
        # 오버라이드 해제 시 원래 라벨로 즉시 복귀해야 한다(캐시되면 안 됨)
        with patch('utils.get_env_value', return_value=None):
            self.assertEqual(ps._PROP_LABEL["category"], "분류")

    def test_invalid_json_falls_back_to_defaults(self):
        with patch('utils.get_env_value', return_value="{not valid json"):
            self.assertEqual(self.notion_service.get_policy_props(), self.notion_service.POLICY_PROPS)

    def test_non_dict_json_falls_back_to_defaults(self):
        """(GPT 리뷰 2026-08-05) 문법은 유효한 JSON이지만 dict가 아닌 값(배열/문자열/숫자/null)
        — .get() 호출 시 AttributeError로 죽던 회귀 버그의 재현 테스트."""
        for bad in ('[]', '"문자열"', '123', 'null', '["a","b"]'):
            with patch('utils.get_env_value', return_value=bad):
                self.assertEqual(self.notion_service.get_policy_props(), self.notion_service.POLICY_PROPS)
                self.assertEqual(self.notion_service.get_policy_source_props(),
                                  self.notion_service.POLICY_SOURCE_PROPS)

    def test_read_write_roundtrip_with_custom_property_names(self):
        """(GPT 리뷰 2026-08-05) 커스텀 속성명의 Notion 페이지 → extract_page_fields()로
        내부 12개 필드 추출 → build_policy_properties()로 재구성 시, 다시 커스텀
        속성명 그대로(출처URL 3개 포함) 나오는지 왕복 검증."""
        import json
        ns = self.notion_service
        override = json.dumps({
            "title": "정책명", "category": "정책구분", "residence": "지역",
            "_source_1": "출처1", "_source_2": "출처2", "_source_3": "출처3",
        })
        fake_page = {
            "id": "page-123",
            "properties": {
                "정책명": {"title": [{"plain_text": "테스트 정책"}]},
                "정책구분": {"select": {"name": "복지"}},
                "지역": {"multi_select": [{"name": "도봉구"}]},
                "대상 특성": {"multi_select": [{"name": "일반"}]},
                "생애주기": {"multi_select": [{"name": "영아"}]},
                "시작 월령(개월)": {"number": 0},
                "종료 월령(개월)": {"number": 12},
                "세부 연령": {"rich_text": [{"plain_text": "0~12개월"}]},
                "대상 소득 수준": {"rich_text": [{"plain_text": "무관"}]},
                "추가 자격요건": {"rich_text": [{"plain_text": "없음"}]},
                "상세 지원 내용": {"rich_text": [{"plain_text": "지원 내용 예시"}]},
                "문의처": {"rich_text": [{"plain_text": "02-000-0000"}]},
                "출처1": {"url": "https://a.example.com"},
                "출처2": {"url": "https://b.example.com"},
                "출처3": {"url": "https://c.example.com"},
            },
        }
        with patch('utils.get_env_value', return_value=override):
            fields = ns.extract_page_fields(fake_page)
            # 내부 fkey로는 오버라이드와 무관하게 12개 전부 정상 추출되어야 한다
            self.assertEqual(fields["title"], "테스트 정책")
            self.assertEqual(fields["category"], "복지")
            self.assertEqual(fields["residence"], ["도봉구"])
            self.assertEqual(fields["contact"], "02-000-0000")
            self.assertEqual(fields["_source_urls"],
                              ["https://a.example.com", "https://b.example.com", "https://c.example.com"])

            props = ns.build_policy_properties(fields)
            # 되돌려 쓸 때도 커스텀 속성명 그대로 키가 나와야 한다
            self.assertIn("정책명", props)
            self.assertIn("정책구분", props)
            self.assertIn("지역", props)
            self.assertIn("출처1", props)
            self.assertIn("출처3", props)
            # 오버라이드 안 한 필드는 기본 속성명 그대로 유지
            self.assertIn("문의처", props)


class TestResolveCategoryLabel(unittest.TestCase):
    """(2026-07-20 실측) config에 하드코딩된 '가족지원'(공백없음)이 실제 Notion select
    옵션 '가족 지원'(공백)과 달라, 적용 시 Notion에 중복 옵션이 생길 뻔한 사고의 회귀 테스트."""

    def test_matches_real_option_with_space_difference(self):
        with patch.object(ps, '_get_db_schema', return_value={"category": ["가족 지원", "복지"]}):
            self.assertEqual(ps._resolve_category_label("family", "가족지원"), "가족 지원")

    def test_falls_back_when_no_schema_cached(self):
        with patch.object(ps, '_get_db_schema', return_value={}):
            self.assertEqual(ps._resolve_category_label("family", "가족지원"), "가족지원")

    def test_falls_back_when_no_match_in_schema(self):
        with patch.object(ps, '_get_db_schema', return_value={"category": ["의료/재활", "복지"]}):
            self.assertEqual(ps._resolve_category_label("family", "가족지원"), "가족지원")


class TestMoneyDateTokens(unittest.TestCase):
    def test_extracts_amounts_and_dates(self):
        toks = ps._money_date_tokens("지원금액 50만원, 신청기간 3월 15일까지")
        self.assertIn("50만원", toks)

    def test_phone_numbers_excluded(self):
        toks = ps._money_date_tokens("문의: 02-1234-5678")
        self.assertEqual(toks, set())


class TestNormHelpers(unittest.TestCase):
    def test_norm_text_strips_punctuation_and_space(self):
        self.assertEqual(ps._norm_text("전국, 서울시 · 도봉구"), ps._norm_text("전국서울시도봉구"))

    def test_norm_cat_keeps_hangul_only(self):
        self.assertEqual(ps._norm_cat("의료/재활"), ps._norm_cat("의료재활"))


if __name__ == '__main__':
    unittest.main()
