import random
import time

def _get_mock_research_data(ticker: str) -> dict:
    """Generates mock company research data for a given ticker."""
    ticker_upper = ticker.upper()
    
    # Specific data for common tickers
    if ticker_upper == "AAPL":
        return {
            "ticker": ticker_upper,
            "ceo": "Tim Cook",
            "founders": ["Steve Jobs", "Steve Wozniak", "Ronald Wayne"],
            "key_investors": ["Vanguard Group", "BlackRock", "Berkshire Hathaway"],
            "founding_year": 1976,
            "headquarters": "Cupertino, California, USA",
            "industry": "Technology",
            "board_members": ["Tim Cook", "Arthur Levinson", "Monica Lozano", "Al Gore"],
            "wikipedia_url": "https://en.wikipedia.org/wiki/Apple_Inc."
        }
    elif ticker_upper == "MSFT":
        return {
            "ticker": ticker_upper,
            "ceo": "Satya Nadella",
            "founders": ["Bill Gates", "Paul Allen"],
            "key_investors": ["Vanguard Group", "BlackRock", "State Street Corp"],
            "founding_year": 1975,
            "headquarters": "Redmond, Washington, USA",
            "industry": "Technology",
            "board_members": ["Satya Nadella", "John W. Thompson", "Emma Walmsley", "Padmasree Warrior"],
            "wikipedia_url": "https://en.wikipedia.org/wiki/Microsoft"
        }
    elif ticker_upper == "GOOGL":
        return {
            "ticker": ticker_upper,
            "ceo": "Sundar Pichai",
            "founders": ["Larry Page", "Sergey Brin"],
            "key_investors": ["Vanguard Group", "BlackRock", "T. Rowe Price Associates"],
            "founding_year": 1998,
            "headquarters": "Mountain View, California, USA",
            "industry": "Technology",
            "board_members": ["Sundar Pichai", "Larry Page", "Sergey Brin", "Ann Mather"],
            "wikipedia_url": "https://en.wikipedia.org/wiki/Alphabet_Inc."
        }
    elif ticker_upper == "AMZN":
        return {
            "ticker": ticker_upper,
            "ceo": "Andy Jassy",
            "founders": ["Jeff Bezos"],
            "key_investors": ["Vanguard Group", "BlackRock", "Capital Research Global Investors"],
            "founding_year": 1994,
            "headquarters": "Seattle, Washington, USA",
            "industry": "E-commerce, Cloud Computing",
            "board_members": ["Andy Jassy", "Jeff Bezos", "Jamie Gorelick", "Jonathan Rubinstein"],
            "wikipedia_url": "https://en.wikipedia.org/wiki/Amazon"
        }
    elif ticker_upper == "NVDA":
        return {
            "ticker": ticker_upper,
            "ceo": "Jensen Huang",
            "founders": ["Jensen Huang", "Chris Malachowsky", "Curtis Priem"],
            "key_investors": ["Vanguard Group", "BlackRock", "Fidelity Management & Research Co."],
            "founding_year": 1993,
            "headquarters": "Santa Clara, California, USA",
            "industry": "Semiconductors",
            "board_members": ["Jensen Huang", "Tench Coxe", "Harvey Jones", "Mark Stevens"],
            "wikipedia_url": "https://en.wikipedia.org/wiki/Nvidia"
        }
    elif ticker_upper == "TSLA":
        return {
            "ticker": ticker_upper,
            "ceo": "Elon Musk",
            "founders": ["Martin Eberhard", "Marc Tarpenning", "Elon Musk", "J.B. Straubel", "Ian Wright"],
            "key_investors": ["Elon Musk", "Vanguard Group", "BlackRock"],
            "founding_year": 2003,
            "headquarters": "Austin, Texas, USA",
            "industry": "Automotive, Energy",
            "board_members": ["Elon Musk", "Robyn Denholm", "James Murdoch", "Kimbal Musk"],
            "wikipedia_url": "https://en.wikipedia.org/wiki/Tesla,_Inc."
        }
    
    # Generic/default data for other tickers
    return {
        "ticker": ticker_upper,
        "ceo": f"CEO of {ticker_upper}",
        "founders": [f"Founder One of {ticker_upper}", f"Founder Two of {ticker_upper}"],
        "key_investors": ["Generic Investor A", "Generic Investor B"],
        "founding_year": random.randint(1950, 2000),
        "headquarters": f"City, State, Country for {ticker_upper}",
        "industry": "General Industry",
        "board_members": [f"Board Member X", f"Board Member Y"],
        "wikipedia_url": f"https://en.wikipedia.org/wiki/{ticker_upper}_(company)"
    }


def research_company(ticker: str) -> dict:
    """
    Simulates fetching detailed company research for a given ticker.
    In a real application, this would involve scraping or calling a third-party API.
    """
    # Simulate network delay for scraping
    time.sleep(random.uniform(0.1, 0.5)) 
    return _get_mock_research_data(ticker)

