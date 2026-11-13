import unittest
import asyncio
import os
import sys

# Add parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.scraper_service import retry_async

class TestScraperCore(unittest.IsolatedAsyncioTestCase):
    
    async def test_retry_logic_success(self):
        """Test if retry logic works and returns value on success."""
        async def success_func():
            return "success"
        
        result = await retry_async(success_func, retries=3)
        self.assertEqual(result, "success")

    async def test_retry_logic_failure(self):
        """Test if retry logic raises exception after retries exhausted."""
        mock_call_count = 0
        async def fail_func():
            nonlocal mock_call_count
            mock_call_count += 1
            raise ValueError("Fail")
            
        with self.assertRaises(ValueError):
            await retry_async(fail_func, retries=3, delay=0.1)
        
        self.assertEqual(mock_call_count, 3)

    async def test_retry_logic_eventual_success(self):
        """Test if retry logic succeeds after failure."""
        mock_call_count = 0
        async def eventual_success_func():
            nonlocal mock_call_count
            mock_call_count += 1
            if mock_call_count < 2:
                raise ValueError("Fail")
            return "success"
            
        result = await retry_async(eventual_success_func, retries=3, delay=0.1)
        self.assertEqual(result, "success")
        self.assertEqual(mock_call_count, 2)

if __name__ == '__main__':
    unittest.main()
