"""A pure contact address must not masquerade as a recipient condition."""
import pytest
from services.policy_source_coverage import coverage_candidates
from services.policy_source_units import source_units, hydrate_policy_source_units


@pytest.mark.parametrize('destination', [
    '거주지 동주민센터', '주소지 행정복지센터',
    '주민등록 주소지 관할 읍·면·동 행정복지센터', '거주지 동주민센터.',
])
@pytest.mark.parametrize('separator', ['\n', ': '])
def test_explicit_contact_destination_is_not_eligibility(destination, separator):
    source = '문의처' + separator + destination
    assert coverage_candidates(source) == []
    assert all(not unit['required'] for unit in source_units(source))


@pytest.mark.parametrize('line,kind', [
    ('거주지 동주민센터. 방문 신청만 가능', 'application'),
    ('거주지 동주민센터에서 접수', 'application'),
    ('서울 거주자만 지원 대상', 'eligibility'),
    ('거주지 동주민센터. 서울 거주자에 한함', 'eligibility'),
    ('거주지 동주민센터. 중복 지원 불가', 'exclusion'),
    ('거주지 동주민센터. 증빙서류 필요', 'documents'),
    ('거주지 동주민센터. 매년 재판정 필요', 'continuation'),
])
@pytest.mark.parametrize('separator', ['\n', ': '])
def test_contact_heading_cannot_hide_a_real_condition(line, kind, separator):
    assert coverage_candidates('문의처' + separator + line) == [{'line_number': 2 if separator == '\n' else 1, 'kind': kind}]


@pytest.mark.parametrize('heading', ['지원대상', '신청방법', '담당기관', '사업목적', ''])
def test_exception_requires_contact_heading(heading):
    source = (heading + '\n' if heading else '') + '거주지 동주민센터'
    assert coverage_candidates(source)


def test_contact_context_ends_at_next_substantive_section():
    source = '문의처\n거주지 동주민센터\n지원대상\n서울 거주자\n신청방법\n방문만 가능'
    rows = coverage_candidates(source)
    assert {'line_number': 2, 'kind': 'eligibility'} not in rows
    assert {'line_number': 4, 'kind': 'eligibility'} in rows
    assert {'line_number': 6, 'kind': 'application'} in rows


def test_optional_contact_keeps_source_bytes_and_exact_contact_field():
    source = '지원내용\n장제비 80만원 지원\n신청서류\n신청서와 영수증\n문의처\n거주지 동주민센터'
    units = source_units(source)
    required = [u['id'] for u in units if u['required']]
    assert len(required) < len(units)
    result = hydrate_policy_source_units(dict(_source_units=dict(
        support_detail=required, eligibility_detail=[], age_detail=[], target_income=[]),
        contact='거주지 동주민센터'), source)
    assert result['contact'] == '거주지 동주민센터'
    assert '신청서와 영수증' in result['support_detail']
    assert source.endswith('문의처\n거주지 동주민센터')


@pytest.mark.parametrize('separator', ['\n', ': '])
def test_final_evidence_requires_exact_separately_preserved_contact(separator):
    from services.policy_evidence import diagnose_policy_evidence
    source = '지원내용\n장제비 80만원 지원\n신청서류\n신청서와 영수증\n문의처' + separator + '거주지 동주민센터'
    required = [u['id'] for u in source_units(source) if u['required']]
    result = hydrate_policy_source_units(dict(_source_units=dict(
        support_detail=required, eligibility_detail=[], age_detail=[], target_income=[]),
        contact='거주지 동주민센터'), source)
    assert diagnose_policy_evidence(result, source)['valid']
    assert not diagnose_policy_evidence(dict(result, contact=''), source)['valid']
    assert not diagnose_policy_evidence(dict(result, contact='주민센터'), source)['valid']


def test_contact_field_cannot_replace_substantive_prose():
    from services.policy_evidence import diagnose_policy_evidence
    source = '지원내용\n장제비 80만원 지원\n문의처\n서울 거주자만 신청 가능'
    result = dict(support_detail='장제비 80만원 지원', contact='서울 거주자만 신청 가능',
        _source_evidence={'support_detail': '장제비 80만원 지원'})
    assert not diagnose_policy_evidence(result, source)['valid']
