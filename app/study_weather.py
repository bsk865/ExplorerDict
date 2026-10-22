"""City-selected weather with a local cache; no device location access."""
import json
import math
import time
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError


def fetch(url):
    try:
        with urlopen(Request(url, headers={'User-Agent':'ExplorerDict/1.0'}), timeout=8) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError('天气服务返回内容过大。')
        return json.loads(raw)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise ValueError('天气服务暂时无法连接，请稍后重试。') from None


def cities(name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError('请输入城市名称。')
    data = fetch('https://geocoding-api.open-meteo.com/v1/search?'+urlencode({'name':name.strip(),'count':8,'language':'zh','format':'json'}))
    return [{'id':r['id'],'name':r['name'],'region':r.get('admin1',''),'country':r.get('country',''),
             'latitude':r['latitude'],'longitude':r['longitude']} for r in data.get('results', [])]


def choose(store, city):
    if not isinstance(city, dict):
        raise ValueError('请选择城市。')
    try:
        lat, lon = float(city['latitude']), float(city['longitude'])
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError()
        name = str(city['name']).strip()
        if not name or len(name)>100:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('城市信息无效，请重新搜索。') from None
    store.put('settings', 'study-weather-city', {'name':name,'latitude':lat,'longitude':lon})
    return {'ok':True}


def current(store):
    city = store.get('settings','study-weather-city')
    if not city:
        return {'status':'unconfigured'}
    cache = store.get('settings','study-weather-cache', {})
    if cache.get('city') == city and time.time()-cache.get('fetched_at',0)<1800:
        return dict(cache, status='ready')
    try:
        data = fetch('https://api.open-meteo.com/v1/forecast?'+urlencode({
            'latitude':city['latitude'],'longitude':city['longitude'],
            'current':'temperature_2m,apparent_temperature,weather_code','timezone':'auto'}))
        value = data.get('current', {})
        temperature = value.get('temperature_2m')
        if not isinstance(temperature, (int,float)) or not math.isfinite(temperature):
            raise ValueError('天气数据暂时不可用。')
        result={'city':city,'temperature':temperature,'feels_like':value.get('apparent_temperature'),
                'code':value.get('weather_code'),'observed_at':value.get('time'),
                'fetched_at':time.time(),'provider':'Open-Meteo'}
        store.put('settings','study-weather-cache',result)
        return dict(result,status='ready')
    except ValueError as exc:
        if cache.get('city')==city:
            return dict(cache,status='stale',error=str(exc))
        return {'status':'unavailable','city':city,'error':str(exc)}
