import unittest
from unittest.mock import patch
from adapters import AdapterRegistry, NormalizedEvent, Adapter

class TestAdaptersIntegration(unittest.TestCase):
    @patch('adapters.requests.get')
    def test_fetch_events_valid_response(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [{'id': 1, 'data': 'event1'}]
        adapter = AdapterRegistry.get_adapter('example_adapter')
        events = adapter.fetch_events()
        self.assertIsInstance(events, list)
        self.assertIsInstance(events[0], NormalizedEvent)

    @patch('adapters.requests.get')
    def test_fetch_events_invalid_response(self, mock_get):
        mock_get.return_value.status_code = 404
        adapter = AdapterRegistry.get_adapter('example_adapter')
        events = adapter.fetch_events()
        self.assertIsNone(events)

    @patch('adapters.requests.get')
    def test_fetch_all_concurrent_execution(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [{'id': 1, 'data': 'event1'}]
        registry = AdapterRegistry()
        registry.register_adapter('example_adapter', Adapter())
        registry.fetch_all()
        self.assertTrue(mock_get.called)

if __name__ == '__main__':
    unittest.main()
