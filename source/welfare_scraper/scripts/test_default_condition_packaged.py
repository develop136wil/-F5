"""Verify new default-condition routes in the actual EXE, with a disposable DB."""
import test_desktop_executable as smoke
import sys

original_check=smoke.check_news_gallery


def check_default(client,data,html_output=None):
    original_check(client,data,html_output)
    response=client.get('/api/news/default-condition')
    response.raise_for_status()
    assert response.json()['profile'] is None
    settings={'name':'격리 기본 조건','region':'서울특별시 도봉구',
        'life_cycles':['노년'],'characteristics':[], 'min_age_months':780,
        'max_age_months':None,'semantic_text':'노년 복지 소식'}
    response=client.put('/api/news/default-condition',json={'settings':settings,'expected_revision':0})
    response.raise_for_status()
    profile=response.json()['profile']
    assert profile['profile_id']=='default-news' and profile['revision']==1
    assert profile['min_age_months']==780 and profile['max_age_months'] is None
    assert client.get('/api/news/default-condition').json()['profile']==profile
    options=client.get('/api/news/profiles/default-news/workflow-options').json()
    assert options['analysis_mode']=='direct-body-bounded-v1'
    assert options['batch_limit'] is None
    assert options['semantic_calibrated'] is False
    assert options['enabled'] is False  # isolated EXE has no real API key
    assert client.put('/api/news/default-condition',json={'settings':settings,'expected_revision':0}).status_code==409
    assert client.post('/api/news/profiles/default-news/archive').status_code==409
    assert client.post('/api/scrape',json={'headless':True}).status_code==409
    assert client.get('/api/news/profiles/default-news/workflows').json()['items']==[]
    assert client.get('/api/status').json()['is_running'] is False
    draft_url = 'https://example.invalid/packaged-draft-recovery'
    recovery = client.post('/api/policy/draft-from-url/status', json={'url': draft_url})
    recovery.raise_for_status()
    assert recovery.json()['state'] == 'clear' and recovery.json()['can_retry'] is False
    assert client.post('/api/policy/draft-from-url/allow-retry', json={
        'url': draft_url, 'attempt_id': 'never-created', 'confirmed': False}).status_code == 400
    print('DRAFT_RECOVERY_PACKAGED_OK: no-receipt/read-only/explicit-confirmation-required')
    print('DEFAULT_CONDITION_PACKAGED_OK: stored/reloaded/conflict/unready-no-legacy')


smoke.check_news_gallery=check_default
if __name__=='__main__':
    smoke.run(sys.argv[1])
