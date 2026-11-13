"""정책 배치 작업이 DB 장애를 빈 결과나 정상 완료로 숨기지 않는지 검증한다."""
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from services import policy_sync as ps


class PolicyStrictPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_check_read_failure_is_not_empty_success(self):
        """조회 장애를 '점검 대상 없음'으로 오인하면 사용자가 정상 완료로 믿게 된다."""
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(database, 'get_connection',
                          side_effect=sqlite3.OperationalError('forced read failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'forced read failure'):
                await ps.check_policies(['medical'], scraper=object())

    async def test_recompute_write_failure_is_not_counted_as_resolved(self):
        """재계산 결과 저장 실패가 완료 응답으로 바뀌지 않아야 한다."""
        row = {
            'id': 1,
            'db_key': 'medical',
            'notion_fields': '{}',
            'last_extracted_fields': '{"title":"새 정책명"}',
            'pending_diff': '{}',
        }
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(database, 'execute_query', return_value=[row]), \
             patch.object(ps, '_normalize_extracted_fields', side_effect=lambda fields, _key: fields), \
             patch.object(ps, '_diff_fields', return_value=[]), \
             patch.object(database, 'get_connection',
                          side_effect=sqlite3.OperationalError('forced write failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'forced write failure'):
                await ps.recompute_diffs(['medical'])


if __name__ == '__main__':
    unittest.main()
