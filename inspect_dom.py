import sys
sys.stdout.reconfigure(encoding='utf-8')
from bs4 import BeautifulSoup

with open('full_page_utarconfession2022.2023.html', 'r', encoding='utf-8') as f:
    soup = BeautifulSoup(f, 'html.parser')

comments = soup.find_all(lambda tag: tag.get('aria-label') and 'Comment by' in tag.get('aria-label'))
if comments:
    c = comments[0]
    p = c.parent
    while p:
        if p.get('aria-posinset'):
            print(f'Found post! role: {p.get("role")}, classes: {p.get("class")}')
            print('Does it have role=article?', p.get('role') == 'article')
            print(p.get_text()[:200].replace('\n', ' '))
            
            # Check if there is an inner role=article
            inner_articles = p.find_all(attrs={'role': 'article'})
            print(f'Inner role=article count: {len(inner_articles)}')
            for ia in inner_articles:
                print('  Inner article label:', ia.get('aria-label'))
            break
        p = p.parent
