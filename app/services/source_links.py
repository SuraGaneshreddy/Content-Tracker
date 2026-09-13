"""User-reviewed chapter/episode links. No fabricated destinations or cross-site monitoring."""
import base64
import hashlib
import hmac
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, unquote
from ..config import settings
from . import metadata

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a' and len(self.links) < 3000:
            href = dict(attrs).get('href')
            if href: self.links.append(href)

def numbered(url, kind):
    field = 'episode' if kind == 'anime' else 'chapter'
    path = unquote(urlsplit(url).path)
    m = re.search(r'(?:^|[/_-])' + field + r'[-_/ ]+(\d{1,5})(?![\d.]|[-_]\d)', path, re.I)
    return (field, int(m.group(1))) if m else None

def sign(user_id, item_id, url, field, number):
    body = base64.urlsafe_b64encode(json.dumps([user_id,item_id,url,field,number,int(time.time())+1800],separators=(',',':')).encode()).decode()
    sig = hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + '.' + sig

def verify(token, user_id, item_id):
    try:
        if len(token) > 7000: raise ValueError()
        body,sig = token.rsplit('.',1)
        expected = hmac.new(settings.secret_key.encode(),body.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expected): raise ValueError()
        uid,iid,url,field,number,expiry = json.loads(base64.urlsafe_b64decode(body))
        if uid != user_id or iid != item_id or expiry < time.time(): raise ValueError()
        if field not in ('chapter','episode') or not isinstance(number,int) or not 0 <= number <= 99999: raise ValueError()
        return url,field,number
    except Exception:
        raise ValueError('This link expired or is invalid. Load the source links again.') from None

def discover(item, user_id):
    kind = item.category.slug
    if kind not in ('anime','manga','manhwa','manhua'):
        return [], 'Chapter/episode links are available only for anime and manga-family items.'
    try:
        response = metadata.safe_get(item.url, accept='text/html', max_bytes=512*1024, timeout=12)
        if response.status_code != 200 or 'html' not in response.content_type:
            return [], 'The source blocked this request or did not return a readable page. Open the saved URL manually.'
        parser = Links()
        parser.feed(response.text)
        rows,seen = [],set()
        for href in parser.links:
            url = metadata.clean_url(urljoin(response.final_url or item.url,href))
            if not url or url in seen or urlsplit(url).hostname != urlsplit(item.url).hostname: continue
            n = numbered(url,kind)
            if not n: continue
            seen.add(url)
            field,number = n
            rows.append(dict(url=url,field=field,number=number,token=sign(user_id,item.id,url,field,number)))
            if len(rows) == 100: break
        rows.sort(key=lambda x:x['number'], reverse=True)
        return rows, '' if rows else 'No explicit numbered chapter/episode links were found. This site may require JavaScript or use opaque IDs; automatic tracking is not supported for it yet.'
    except (metadata.FetchError,metadata.UnsafeUrlError,metadata.ThrottledError,ValueError,OSError):
        return [], 'The source is unavailable or rate-limited. Try again later.'
