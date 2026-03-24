import unittest
from unittest.mock import patch
from your_module import AdapterRegistry, NormalizedEvent

class TestAdaptersIntegration(unittest.TestCase):
    @patch('requests.get')
    def test_fetch_events(self, mock_get):
        # Mock HTTP response
        mock_get.return_value.json.return_value = [{'id': 1, 'name': 'Event 1'}]
        
        # Test each adapter's fetch_events() returns valid NormalizedEvent list
        for adapter in AdapterRegistry.adapters:
            events = adapter.fetch_events()
            self.assertIsInstance(events, list)
            for event in events:
                self.assertIsInstance(event, NormalizedEvent)

    def test_fetch_all(self):
        # Test AdapterRegistry.fetch_all() concurrent execution
        events = AdapterRegistry.fetch_all()
        self.assertIsInstance(events, list)
        for event in events:
            self.assertIsInstance(event, NormalizedEvent)

if __name__ == '__main__':
    unittest.main()
