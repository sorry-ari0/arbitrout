from datetime import datetime, timedelta
from adapters.registry import AdapterRegistry

class ThetaScanner:
    def __init__(self, registry: AdapterRegistry):
        self.registry = registry

    def get_theta_opportunities(self):
        opportunities = []
        for event in self.registry.get_all_events():
            if event.get('end_date') or event.get('close_date'):
                end_date = event.get('end_date') or event.get('close_date')
                days_to_expiry = (datetime.strptime(end_date, '%Y-%m-%d') - datetime.now()).days
                if days_to_expiry <= 7:
                    implied_probability = event.get('implied_probability', 0)
                    current_price = event.get('current_price', 0)
                    edge = implied_probability - current_price
                    edge_pct = (edge / current_price) * 100 if current_price > 0 else 0
                    opportunities.append({
                        'event_id': event['id'],
                        'days_to_expiry': days_to_expiry,
                        'edge_pct': edge_pct,
                        'implied_probability': implied_probability,
                        'current_price': current_price,
                    })
        return sorted(opportunities, key=lambda x: (x['days_to_expiry'], x['edge_pct']), reverse=True)
