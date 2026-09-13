"""Bounded, cached public catalog discovery. Catalog data is not release verification."""
import json
import time
import threading
from collections import OrderedDict
from datetime import date
from urllib.parse import urlencode
from ..config import settings
from . import metadata

_cache = OrderedDict()
_lock = threading.Lock()

def browse(kind, query='', page=1, year='', status='all', country='all'):
    query = str(query).strip()[:200]
    try:
        page = max(1, min(500, int(page)))
    except (ValueError, TypeError):
        page = 1
    year = str(year)
    year = year if year.isdigit() and 1900 <= int(year) <= date.today().year + 5 else ''
    status = status if status in ('all', 'upcoming', 'current', 'finished') else 'all'
    result = dict(cards=[], error='', page=page, has_next=False, q=query, year=year, status=status,
                  source='TMDB' if kind == 'movie' else 'MyAnimeList via Jikan (unofficial API)', country=country)
    if kind not in ('anime', 'manga', 'manhwa', 'manhua', 'movie'):
        result['error'] = 'This category does not have a catalog provider.'
        return result
    if (kind == 'movie' and not settings.has_tmdb) or (kind != 'movie' and not settings.jikan_enabled):
        result['error'] = 'Set TMDB_API_KEY in Render Environment to enable movies.' if kind == 'movie' else 'Jikan catalog lookups are disabled.'
        return result
    key = (kind, query, page, year, status, country)
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
    params = dict(page=page)
    if kind == 'movie':
        params.update(api_key=settings.tmdb_api_key, include_adult='false')
        path = 'search/movie' if query else 'discover/movie'
        if query:
            params['query'] = query
            if year: params['primary_release_year'] = year
        else:
            params['sort_by'] = 'popularity.desc'
            if year: params['primary_release_year'] = year
            if country not in ('all', 'other') and len(country) == 2: params['with_origin_country'] = country
            if status == 'upcoming': params['primary_release_date.gte'] = date.today().isoformat()
            if status == 'finished': params['primary_release_date.lte'] = date.today().isoformat()
        url = 'https://api.themoviedb.org/3/' + path + '?' + urlencode(params)
    else:
        params.update(limit=24, sfw='true')
        if query: params['q'] = query
        else: params.update(order_by='members', sort='desc')
        if kind != 'anime': params['type'] = kind
        if year: params.update(start_date=year + '-01-01', end_date=year + '-12-31')
        if status != 'all': params['status'] = {'upcoming':'upcoming', 'current':'airing' if kind == 'anime' else 'publishing', 'finished':'complete'}[status]
        url = 'https://api.jikan.moe/v4/' + ('anime' if kind == 'anime' else 'manga') + '?' + urlencode(params)
    try:
        response = metadata.safe_get(url, accept='application/json', max_bytes=1024*1024, timeout=12,
                                     extra_headers=metadata.tmdb_headers() if kind == 'movie' else {})
        if response.status_code != 200:
            raise ValueError('provider unavailable')
        data = json.loads(response.text)
        entries = data.get('results' if kind == 'movie' else 'data')
        if not isinstance(entries, list): raise ValueError('invalid data')
        for entry in entries[:24]:
            if not isinstance(entry, dict): continue
            if entry.get('adult') or any(g.get('name') in ('Hentai', 'Erotica') for g in entry.get('genres', []) if isinstance(g, dict)):
                continue
            if kind == 'movie':
                ident = entry.get('id')
                if not isinstance(ident, int): continue
                card = dict(id=str(ident), title=metadata.clean_text(entry.get('title'), 200),
                            image_url=metadata.clean_url('https://image.tmdb.org/t/p/w342'+entry['poster_path']) if entry.get('poster_path') else None,
                            url=f'https://www.themoviedb.org/movie/{ident}', release_date=entry.get('release_date') or 'Not announced',
                            alternate_titles=[metadata.clean_text(entry.get('original_title'), 200)],
                            description=metadata.clean_text(entry.get('overview'), 600), type='Movie')
            else:
                ident = entry.get('mal_id')
                if not isinstance(ident, int): continue
                start = ((entry.get('aired' if kind == 'anime' else 'published') or {}).get('prop') or {}).get('from') or {}
                release = str(start['year']) if start.get('year') else 'Not announced'
                if start.get('year') and start.get('month'):
                    release += f"-{int(start['month']):02d}"
                    if start.get('day'): release += f"-{int(start['day']):02d}"
                images = (entry.get('images') or {}).get('jpg') or {}
                card = dict(id=str(ident), title=metadata.clean_text(entry.get('title_english') or entry.get('title'), 200),
                            image_url=metadata.clean_url(images.get('large_image_url') or images.get('image_url')),
                            url=f"https://myanimelist.net/{'anime' if kind == 'anime' else 'manga'}/{ident}",
                            release_date=release, type=metadata.clean_text(entry.get('type'), 30),
                            alternate_titles=[metadata.clean_text(t.get('title'), 200) for t in entry.get('titles', []) if isinstance(t, dict)][:5],
                            description=metadata.clean_text(entry.get('synopsis'), 600),
                            episodes=entry.get('episodes'), chapters=entry.get('chapters'), status=metadata.clean_text(entry.get('status'), 50))
            result['cards'].append(card)
        result['has_next'] = (page < min(int(data.get('total_pages') or 1), 500)) if kind == 'movie' else bool((data.get('pagination') or {}).get('has_next_page')) and page < 500
    except (metadata.FetchError, metadata.UnsafeUrlError, metadata.ThrottledError, ValueError, TypeError, KeyError, OSError):
        result['error'] = 'The catalog provider is unavailable or rate-limited. Try again later; your saved library is unaffected.'
    with _lock:
        _cache[key] = (time.monotonic() + (30 if result['error'] else 600), dict(result))
        _cache.move_to_end(key)
        while len(_cache) > 128: _cache.popitem(last=False)
    return result


def page_catalog(request, kind):
    p = request.query_params
    result = browse(kind, p.get('q', ''), p.get('page', 1), p.get('year', ''), p.get('status', 'all'), p.get('country', 'all'))
    for name, number in [('previous', result['page']-1), ('next', result['page']+1)]:
        result[name + '_url'] = '?' + urlencode(dict(q=result['q'], year=result['year'], status=result['status'], country=result['country'], page=number))
    return result


def cover_matches(kind, title):
    """Suggestions only: caller/user must confirm identity before choosing a cover."""
    data = browse(kind, title)
    return [dict(id=c['id'], title=c['title'], image_url=c.get('image_url'), description=c.get('description'),
                 source=data['source'], note=f"Catalog chapters: {c.get('chapters') or 'unknown'} · {c.get('release_date', '')}")
            for c in data['cards'][:6]]
