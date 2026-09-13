import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import pytest
from app.services import catalog, metadata, source_links
from tests.conftest import add_item, api_post, content_id_from, csrf_token

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    catalog._cache.clear()
    monkeypatch.setattr(metadata, 'safe_get', lambda *a, **k: (_ for _ in ()).throw(metadata.FetchError('test unavailable')))

def stub(monkeypatch, data):
    seen=[]
    def fetch(url, **kwargs):
        seen.append(url)
        return SimpleNamespace(status_code=200,text=json.dumps(data))
    monkeypatch.setattr(metadata,'safe_get',fetch)
    return seen

def entry(**kw):
    return dict(mal_id=1,title='テスト',title_english='Test',type='Manhwa',chapters=None,
                images={'jpg':{'image_url':'https://example.org/cover.jpg'}},published={'prop':{'from':{'year':2027,'month':2,'day':None}}},**kw)

@pytest.mark.parametrize('kind', ['anime','manga','manhwa','manhua'])
def test_query_and_pagination(monkeypatch,kind):
    seen=stub(monkeypatch,{'data':[entry()], 'pagination':{'has_next_page':True}})
    r=catalog.browse(kind,'ナノマシン',2,2027,'upcoming')
    qs=parse_qs(urlsplit(seen[0]).query)
    assert qs['q']==['ナノマシン'] and qs['sfw']==['true'] and qs['page']==['2']
    if kind!='anime': assert qs['type']==[kind]
    assert r['has_next'] and r['cards'][0]['url'].startswith('https://myanimelist.net/')
    if kind!='anime': assert r['cards'][0]['release_date']=='2027-02'
    catalog.browse(kind,'ナノマシン',2,2027,'upcoming')
    assert len(seen)==1

def test_errors_not_empty_success():
    r=catalog.browse('anime')
    assert r['error'] and not r['cards']

def test_filter_explicit_and_unsafe_links(monkeypatch):
    stub(monkeypatch,{'data':[entry(genres=[{'name':'Hentai'}]),entry(url='javascript:alert(1)')]})
    r=catalog.browse('manhwa')
    assert len(r['cards'])==1 and r['cards'][0]['url']=='https://myanimelist.net/manga/1'

def test_unknown_dates_and_counts(monkeypatch):
    stub(monkeypatch,{'data':[{'mal_id':1,'title':'Test'}]})
    r=catalog.browse('anime')
    assert r['cards'][0]['release_date']=='Not announced'
    assert r['cards'][0]['episodes'] is None

@pytest.mark.parametrize('path',['anime','manga','manhwa','manhua','movies'])
def test_category_form(auth_client,path):
    r=auth_client.get('/updates/'+path+'?q=hello&page=2&year=2027')
    assert r.status_code==200
    assert 'Discover titles' in r.text and 'name="q"' in r.text
    assert 'Page 2' in r.text

def test_movies(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(catalog,'settings',replace(metadata.settings,tmdb_api_key='test'))
    seen=stub(monkeypatch,{'results':[{'id':1,'title':'Film','release_date':'2027-01-02'}],'total_pages':3})
    r=catalog.browse('movie',year='2027',status='upcoming',country='IN')
    assert r['cards'][0]['release_date']=='2027-01-02' and r['has_next']
    assert 'with_origin_country=IN' in seen[0]

@pytest.mark.parametrize('path,expected', [('/series/home',None),('/reader/test-chapter-329-eng/',('chapter',329)),('/chapter-12.5/',None),('/chapter-12-5/',None),('/chapter-100000/',None)])
def test_numbered_links(path,expected):
    assert source_links.numbered('https://example.org'+path,'manhwa')==expected

def test_signature():
    token=source_links.sign(1,2,'https://example.org/chapter-3','chapter',3)
    assert source_links.verify(token,1,2)[2]==3
    for uid,iid,value in [(2,2,token),(1,3,token),(1,2,token+'x')]:
        with pytest.raises(ValueError): source_links.verify(value,uid,iid)

def test_discover_and_mark(auth_client,monkeypatch):
    response=add_item(auth_client,chapter=4)
    iid=content_id_from(response.headers['location'])
    page='<a href="/series/chapter-9/">9</a><a href="https://evil.example/chapter-20/">20</a><a href="/chapter-9.5/">9.5</a>'
    monkeypatch.setattr(metadata,'safe_get',lambda *a,**k: SimpleNamespace(status_code=200,text=page,content_type='text/html',final_url='https://example.org/series'))
    response=auth_client.post(f'/content/{iid}/source-links',data={'csrf_token':csrf_token(auth_client)})
    assert response.status_code==200 and 'Open & mark chapter 9' in response.text
    assert 'evil.example' not in response.text
    import re,html
    token=html.unescape(re.search(r'name="token" value="([^"]+)"',response.text)[1])
    url=f'/content/{iid}/open-numbered'
    assert auth_client.post(url,data={'token':token},follow_redirects=False).status_code==403
    response=auth_client.post(url,data={'token':token,'csrf_token':csrf_token(auth_client)},follow_redirects=False)
    assert response.status_code==303 and response.headers['location']=='https://example.org/series/chapter-9/'
    assert 'Chapter 9' in auth_client.get(f'/content/{iid}').text
    assert 'example.org/series/solo-leveling' in auth_client.get(f'/content/{iid}').text

def test_lookup_chapters_are_not_progress(auth_client,monkeypatch):
    monkeypatch.setattr(metadata,'scrape_page',lambda *a,**k: metadata.Metadata(title='Test',chapter=300,image_url='https://example.org/cover.jpg'))
    response=auth_client.post('/add',data={'title':'Test','url':'https://example.org/test','category':'manhwa','status':'reading','auto_metadata':'1','csrf_token':csrf_token(auth_client)},follow_redirects=False)
    assert response.status_code==303
    text=auth_client.get(response.headers['location']).text
    assert 'Chapter 300' not in text and 'https://example.org/cover.jpg' in text


def test_refresh_cover(auth_client,monkeypatch):
    response=add_item(auth_client,cover='https://example.org/old.jpg')
    iid=content_id_from(response.headers['location'])
    url=f'/content/{iid}/refresh-cover'
    assert auth_client.post(url).status_code==403
    monkeypatch.setattr(metadata,'scrape_page',lambda *a,**k: metadata.Metadata(image_url='https://example.org/new.jpg'))
    r=auth_client.post(url,data={'csrf_token':csrf_token(auth_client)})
    assert r.status_code==200 and 'https://example.org/new.jpg' in r.text
    monkeypatch.setattr(metadata,'scrape_page',lambda *a,**k: metadata.Metadata(error='Unavailable'))
    r=auth_client.post(url,data={'csrf_token':csrf_token(auth_client)})
    assert 'existing cover was kept' in r.text and 'https://example.org/new.jpg' in r.text
