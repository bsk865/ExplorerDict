"""Readable source snapshots: semantic blocks, native MathML, no active HTML."""
from __future__ import annotations
import re
from copy import copy
from urllib.parse import urljoin, urlsplit, urldefrag
from bs4 import BeautifulSoup, Tag, NavigableString, Comment

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
    hostname=(urlsplit(url).hostname or '').lower()
    wechat = hostname == 'mp.weixin.qq.com'
    article = soup.select_one('#js_content') if wechat else None
    selectors=()
    if hostname == 'ithome.com' or hostname.endswith('.ithome.com'):
        selectors=('h1','#paragraph') if soup.select_one('#paragraph') else ()
    elif hostname == 'sciencedaily.com' or hostname.endswith('.sciencedaily.com'):
        selectors=('h1','#story_text','#story_source','#journal_references') if soup.select_one('#story_text') else ()
    if selectors:
        article=soup.new_tag('article')
        for selector in selectors:
            source=soup.select_one(selector)
            if source:
                article.append(copy(source))
    article = article or soup.select_one('[itemprop="articleBody"]') or soup.find('article') or soup.find('main')
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
    images={}

    def split_images(node):
        """Split at images while preserving inline formatting on both sides."""
        if isinstance(node, Tag) and node.name == 'img':
            return [(True, node)]
        if not isinstance(node, Tag) or not node.find('img'):
            return [(False, copy(node))]
        result=[]
        fragment=copy(node)
        fragment.clear()
        for child in node.contents:
            for is_image, part in split_images(child):
                if is_image:
                    if fragment.contents:
                        result.append((False,fragment))
                        fragment=copy(node)
                        fragment.clear()
                    result.append((True,part))
                else:
                    fragment.append(part)
        if fragment.contents:
            result.append((False,fragment))
        return result

    def emit_image(im, owner):
        figure=im.find_parent('figure')
        caption=figure.find('figcaption') if figure else None
        label=caption.get_text(' ',strip=True) if caption else im.get('alt') or '原文插图'
        address=next((im.get(key) for key in ('data-src','data-original','data-lazy-src','src') if im.get(key)), '')
        if not address and im.get('srcset'):
            address=im['srcset'].split(',')[0].strip().split()[0]
        image_url=urljoin(url,address)
        anchor=im.get('id') or owner.get('id','')
        try:
            parts=urlsplit(image_url)
            if not address or not load_image or parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password:
                raise ValueError('Image source not available')
            if image_url not in images:
                images[image_url]=store.image(load_image(image_url))
            aid,size=images[image_url]
            blocks.append({'type':'image','asset':aid,'caption':label,'width':size[0],'height':size[1],'source_anchor':anchor})
            stats['images']+=1
        except Exception:
            blocks.append({'type':'text','text':'[图片暂未保存] '+label,'image_missing':True,'source_anchor':anchor})
            stats['missing_images']+=1

    def emit(node):
        if node.find('img'):
            for is_image,part in split_images(node):
                emit_image(part,node) if is_image else emit(part)
            return
        html=safe_markup(node)
        clean=BeautifulSoup(html,'html.parser')
        text=re.sub(r'\s+',' ',clean.get_text()).strip()
        if not text:
            return
        stats['math']+=len(clean.find_all('math'))
        blocks.append({'type':'text','text':text,'html':html,'heading':int(node.name[1]) if node.name in {'h1','h2','h3','h4','h5','h6'} else 0,'source_anchor':node.get('id','')})
    def walk(node):
        if isinstance(node,Comment):
            return
        if isinstance(node,NavigableString):
            if str(node).strip():
                wrapper=soup.new_tag('p')
                wrapper.append(copy(node))
                emit(wrapper)
            return
        if not isinstance(node,Tag):
            return
        if node.name=='img':
            emit_image(node,node.parent)
            return
        if node.name=='figcaption' and node.find_parent('figure') and node.find_parent('figure').find('img'):
            return  # The image block already carries its original caption.
        if node.name in {'p','h1','h2','h3','h4','h5','h6','table','blockquote','li','span','strong','em','b','i','u','sub','sup'}:
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
    from .discovery import fetch
    def load(address,max_size):
        body,_,charset=fetch(address,max_size)
        return body,charset
    raw,charset=load(url,5_000_000)
    title,blocks,stats=readable_blocks(raw.decode(charset,errors='replace'),url,store,lambda u:load(u,24_000_000)[0])
    a=store.import_article(title,blocks,origin='web')
    a.update(source_url=url,source_status='readable',source_stats=stats)
    store.update_article(a)
    return store.article(a['id'])
