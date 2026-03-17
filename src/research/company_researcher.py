import json
import time
from scrapling import Scrapler
from swarm_engine import _COMPANY_NAMES
import re

def research_company(ticker: str) -> dict:
    try:
        company_name = _COMPANY_NAMES[ticker]
    except KeyError:
        company_name = f"{ticker} company"
    url = f"https://en.wikipedia.org/wiki/{company_name}"
    scrapler = Scrapler()
    page = scrapler.scrape(url)
    data = {
        "CEO name": None,
        "founders": None,
        "founding year": None,
        "headquarters": None,
        "industry": None,
        "key investors": None,
        "board members": None,
        "recent controversies": None
    }
    try:
        with open('data/company_research_cache.json', 'r') as f:
            cache = json.load(f)
            if company_name in cache:
                return cache[company_name]
    except FileNotFoundError:
        pass

    infobox = page.css('table.infobox')
    if infobox:
        infobox_data = {}
        rows = infobox.css('tr')
        for row in rows:
            label_el = row.css('th')
            value_el = row.css('td')

            if label_el and value_el:
                label_text = label_el.css('::text').get()
                full_value_text = ' '.join(value_el.css('*::text').getall()).strip()

                if label_text and full_value_text:
                    infobox_data[label_text.strip()] = full_value_text

        ceo_info = infobox_data.get('CEO') or infobox_data.get('Key people')
        if ceo_info:
            if '(CEO)' in ceo_info:
                data['CEO name'] = ceo_info.split('(CEO)')[0].strip()
            else:
                data['CEO name'] = ceo_info.strip()

        data['founders'] = infobox_data.get('Founder') or infobox_data.get('Founders')
        
        founding_year_str = infobox_data.get('Founded')
        if founding_year_str:
            match = re.search(r'\b(\d{4})\b', founding_year_str)
            if match:
                data['founding year'] = int(match.group(1))

        data['headquarters'] = infobox_data.get('Headquarters') or infobox_data.get('Area served')
        data['industry'] = infobox_data.get('Industry')

    controversy_text_parts = []
    controversy_sections_elements = page.css('h2, h3')
    for section_header in controversy_sections_elements:
        headline_span = section_header.css('span.mw-headline')
        if headline_span:
            section_title = headline_span.css('::text').get()
            if section_title and any(k.lower() in section_title.lower() for k in ["controversies", "criticism", "legal issues", "scandals"]):
                current_element = section_header.next()
                temp_summary = []
                while current_element and current_element.tag_name not in ['h1', 'h2', 'h3', 'div', 'table']:
                    if current_element.tag_name == 'p':
                        paragraph_text = current_element.text().strip()
                        if paragraph_text:
                            temp_summary.append(paragraph_text)
                            if len(temp_summary) >= 2:
                                break
                    current_element = current_element.next()
                if temp_summary:
                    controversy_text_parts.append(f"{section_title}: {' '.join(temp_summary)}")
    if controversy_text_parts:
        data['recent controversies'] = "\n".join(controversy_text_parts)

    with open('data/company_research_cache.json', 'r+') as f:
        try:
            cache = json.load(f)
        except json.JSONDecodeError:
            cache = {}
        cache[company_name] = data
        f.seek(0)
        json.dump(cache, f)
        f.truncate()
    return data

def research_batch(tickers: list) -> list[dict]:
    results = []
    for ticker in tickers:
        results.append(research_company(ticker))
        time.sleep(1.5)
    return results
