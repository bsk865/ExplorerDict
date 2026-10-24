"""Readable source snapshots: semantic blocks, native MathML, no active HTML."""
from __future__ import annotations
import re
from urllib.parse import urljoin, urlsplit, urldefrag
from urllib.request import Request, build_opener
from bs4 import BeautifulSoup, Tag

ALLOWED = set('p pre code h1 h2 h3 h4 h5 h6 strong em b i u sub sup span br blockquote ul ol li table thead tbody tfoot tr th td caption math semantics mrow mi mo mn mtext mspace mfrac msqrt mroot msub msup msubsup munder mover munderover mtable mtr mtd mfenced menclose mpadded mstyle mmultiscripts mprescripts none'.split())
MATH_ATTRS = set('display mathvariant stretchy fence separator accent accentunder columnalign rowalign columnspacing rowspacing colspan rowspan linethickness notation lspace rspace width height depth voffset scriptlevel'.split())
DROP = 'script,style,iframe,object,embed,form,input,button,textarea,select,nav,header,footer,aside,annotation,annotation-xml,noscript,template,.ltx_tag_item'


def safe_markup(fragment):
    soup = BeautifulSoup(str(fragment), 'html.parser')
    for node in list(soup.select(DROP)):
        if node.parent:
            node.decompose()
    for node in list(soup.find_all(True)):
        if not node.parent:
            continue
        if node.name not in ALLOWED:
            node.unwrap()
            continue
        node.attrs = {k:v for k,v in node.attrs.items() if k in MATH_ATTRS and isinstance(v,str) and len(v)<100 and not any(s in v.lower() for s in ('url','javascript',':','\\'))}
    return str(soup)


def readable_blocks(html, url, store, load_image=None):
    soup = BeautifulSoup(html, 'html.parser')
    wechat = urlsplit(url).hostname == 'mp.weixin.qq.com'
    article = (soup.select_one('#js_content') if wechat else None) or soup.find('article') or soup.find('main')
    if article is None:
        raise ValueError('这页没有明确的文章区域。请导入原文件，或继续在原网页阅读；不把局部抓取冒充全文。')
    access = soup.find('meta', attrs={'itemprop':'isAccessibleForFree'})
    if (access and str(access.get('content', '')).lower() == 'false') or article.select_one('.paywall, #paywall, [data-paywall]'):
        raise ValueError('此页面存在订阅或登录限制，未能确认全文。请导入你已获取的原文件。')
    title = (soup.select_one('#activity-name') if wechat else None) or article.find('h1') or soup.find('title')
    title = title.get_text(' ',strip=True) if title else '网页文章'
    for node in list(article.select(DROP)):
        if node.parent:
            node.decompose()
    blocks=[]
    stats={'math':0,'images':0,'missing_images':0}
    def emit(node):
        html=safe_markup(node)
        clean=BeautifulSoup(html,'html.parser')
        text=re.sub(r'\s+',' ',clean.get_text()).strip()
        if not text:
            return
        stats['math']+=len(clean.find_all('math'))
        blocks.append({'type':'text','text':text,'html':html,'heading':int(node.name[1]) if node.name in {'h1','h2','h3','h4','h5','h6'} else 0,'source_anchor':node.get('id','')})
    def walk(node):
        if not isinstance(node,Tag):
            return
        if node.name=='figure' and node.find('img'):
            caption=node.find('figcaption')
            caption_text=caption.get_text(' ',strip=True) if caption else ''
            for im in node.find_all('img'):
                image_url=urljoin(url,im.get('src',''))
                label=caption_text or im.get('alt') or '原文插图'
                try:
                    if not load_image or urlsplit(image_url).scheme not in {'http','https'} or urlsplit(image_url).netloc!=urlsplit(url).netloc:
                        raise ValueError('Image source not available')
                    aid,size=store.image(load_image(image_url))
                    blocks.append({'type':'image','asset':aid,'caption':label,'width':size[0],'height':size[1],'source_anchor':node.get('id','')})
                    stats['images']+=1
                except Exception:
                    blocks.append({'type':'text','text':'[图片暂未保存] '+label,'image_missing':True,'source_anchor':node.get('id','')})
                    stats['missing_images']+=1
            return
        if node.name in {'p','h1','h2','h3','h4','h5','h6','table','blockquote','li'}:
            emit(node);return
        if node.name in {'pre','code'}:
            blocks.append({'type':'text','text':node.get_text().strip('\n'), 'html':safe_markup(node),
                           'source_code':True, 'source_anchor':node.get('id','')});return
        if node.name == 'math':
            emit(node);return
        for child in list(node.children):
            walk(child)
    walk(article)
    if not blocks:
        raise ValueError('没有获得可阅读的正文。')
    return title,blocks,stats


def capture_source(store,url):
    url=urldefrag(url)[0]
    parts=urlsplit(url)
    if parts.scheme not in {'https','http'} or not parts.netloc or parts.username or parts.password:
        raise ValueError('请提供文章的 HTTP/HTTPS 原网页地址。')
    opener=build_opener()
    def load(address,max_size):
        request=Request(address,headers={'User-Agent':'Mozilla/5.0','Accept':'text/html,image/*;q=0.9,*/*;q=0.5'})
        with opener.open(request,timeout=25) as response:
            body=response.read(max_size+1)
            if len(body)>max_size:
                raise ValueError('页面或图片过大，请导入原文件。')
            return body,response.headers.get_content_charset() or 'utf-8'
    raw,charset=load(url,5_000_000)
    title,blocks,stats=readable_blocks(raw.decode(charset,errors='replace'),url,store,lambda u:load(u,24_000_000)[0])
    a=store.import_article(title,blocks,origin='web')
    a.update(source_url=url,source_status='readable',source_stats=stats)
    store.update_article(a)
    return store.article(a['id'])
