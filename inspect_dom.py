import sys
sys.stdout.reconfigure(encoding='utf-8')
from bs4 import BeautifulSoup

# Check the second page's debug HTML
with open('debug_full_article_uc20212022.html', 'r', encoding='utf-8') as f:
    soup = BeautifulSoup(f, 'html.parser')

# What tags exist in the article?
print("=== Top-level structure ===")
if soup.body:
    root = soup.body
else:
    root = soup
    
# Look for data-ad-preview
msg = root.find(attrs={'data-ad-preview': True})
print(f'data-ad-preview found: {msg is not None}')
if msg:
    print(f'  text: {msg.get_text()[:200]}')

msg2 = root.find(attrs={'data-ad-comet-preview': True})
print(f'data-ad-comet-preview found: {msg2 is not None}')

# Check the aria-posinset
posinset = root.find(attrs={'aria-posinset': True})
print(f'aria-posinset found: {posinset is not None}')

# Check role=article
articles = root.find_all(attrs={'role': 'article'})
print(f'role=article count: {len(articles)}')
for i, a in enumerate(articles):
    print(f'  Article {i}: aria-label={a.get("aria-label", "")[:50]}')

# What div[dir="auto"] elements exist?
autos = root.find_all('div', attrs={'dir': 'auto'})
print(f'\ndiv[dir=auto] count: {len(autos)}')
for i, a in enumerate(autos[:5]):
    print(f'  auto {i}: {a.get_text()[:80].replace(chr(10), " ")}')
    
# Does it have Like/Comment/Share?
buttons = root.find_all(['div', 'span'], string=lambda t: t and t.strip().lower() in ('like', 'comment', 'share'))
print(f'\nLike/Comment/Share buttons: {len(buttons)}')
