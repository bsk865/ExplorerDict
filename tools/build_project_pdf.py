"""Build the project technical PDF and architecture figure from maintained sources."""
from pathlib import Path
import re
from urllib.parse import quote
from xml.sax.saxutils import escape

from PIL import Image as PILImage, ImageDraw, ImageFont
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle, KeepTogether

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'docs/项目资料/技术文档.md'
OUTPUT = SOURCE.with_suffix('.pdf')
FONT = Path('C:/Windows/Fonts/msyh.ttc')
BOLD = Path('C:/Windows/Fonts/msyhbd.ttc')


def architecture():
    picture = PILImage.new('RGB', (1680, 800), '#f4f7f4')
    draw = ImageDraw.Draw(picture)
    font = lambda size: ImageFont.truetype(str(FONT), size)
    def centered(y, text, size=28, fill='#47604d'):
        draw.text((840, y), text, anchor='mt', font=font(size), fill=fill)
    def box(bounds, title, subtitle, color='#ffffff'):
        draw.rounded_rectangle(bounds, radius=18, fill=color, outline='#b9cbbb', width=2)
        x1,y1,x2,y2=bounds
        draw.text(((x1+x2)/2,y1+20),title,anchor='mt',font=font(29),fill='#25442f')
        draw.text(((x1+x2)/2,y1+64),subtitle,anchor='mt',font=font(22),fill='#576b5c')
    def arrow(x,y1,y2):
        draw.line((x,y1,x,y2-8),fill='#66846e',width=4)
        draw.polygon([(x-9,y2-14),(x+9,y2-14),(x,y2)],fill='#66846e')
    centered(22,'探索词典 · 系统架构',36)
    box((80,90,810,205),'原生主界面','发现 · 阅读 · 翻译 · 问答 · 导图 · 计划')
    box((870,90,1600,205),'悬浮取词','Windows 选区与上下文 · 即时探索')
    arrow(445,207,250);arrow(1235,207,250)
    box((80,250,1600,365),'共享业务服务','资料接入 · 检索与引用 · 模型任务 · 学习记录','#e5eee5')
    arrow(390,366,405);arrow(1275,366,405)
    box((80,405,800,525),'本地检索','关键词 + 本地向量 · 原文位置与版本')
    box((880,405,1600,525),'模型服务','用户选择接口 · 问答 / 翻译 / 导图 / 规划')
    arrow(445,527,570);arrow(1235,527,570)
    box((80,570,1600,690),'本地资料与共享配置','SQLite · 文章与附件 · 会话与笔记 · 计划 · 主题 · 加密密钥','#e5eee5')
    centered(730,'文章、来源与会话连接每个步骤',25)
    path = ROOT/'docs/images/architecture.png'
    path.parent.mkdir(parents=True,exist_ok=True)
    picture.save(path)


def link_url(target):
    if target.startswith(('http://','https://')):
        return target
    path, separator, fragment = target.partition('#')
    resolved=(SOURCE.parent/path).resolve() if path else SOURCE
    relative=resolved.relative_to(ROOT).as_posix()
    return 'https://github.com/bsk865/ExplorerDict/blob/main/'+quote(relative,safe='/') + ('#'+quote(fragment) if separator else '')


def rich(value):
    pieces=[]; start=0
    pattern=re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
    for match in pattern.finditer(value):
        pieces.append(escape(value[start:match.start()]).replace('`',''))
        pieces.append('<link href="'+escape(link_url(match[2]),{'"':'&quot;'})+'" color="#35634b">'+escape(match[1])+'</link>')
        start=match.end()
    pieces.append(escape(value[start:]).replace('`',''))
    return re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', ''.join(pieces))


def main():
    architecture()
    pdfmetrics.registerFont(TTFont('Project',str(FONT),subfontIndex=0))
    pdfmetrics.registerFont(TTFont('ProjectBold',str(BOLD),subfontIndex=0))
    pdfmetrics.registerFontFamily('Project',normal='Project',bold='ProjectBold',italic='Project',boldItalic='ProjectBold')
    common=dict(fontName='Project',textColor=HexColor('#304638'),wordWrap='CJK')
    body=ParagraphStyle('body',fontSize=10.2,leading=17.2,spaceAfter=10,**common)
    title=ParagraphStyle('title',fontName='ProjectBold',fontSize=27,leading=37,spaceAfter=20,textColor=HexColor('#244a34'),wordWrap='CJK')
    section=ParagraphStyle('section',fontName='ProjectBold',fontSize=19,leading=29,spaceBefore=16,spaceAfter=18,keepWithNext=True,textColor=HexColor('#244a34'),wordWrap='CJK')
    subsection=ParagraphStyle('subsection',fontName='ProjectBold',fontSize=12.2,leading=21,spaceBefore=10,spaceAfter=8,keepWithNext=True,textColor=HexColor('#244a34'),wordWrap='CJK')
    code=ParagraphStyle('code',fontSize=8.8,leading=15,spaceAfter=0,**common)
    small=ParagraphStyle('small',fontSize=8.8,leading=14,spaceAfter=10,**common)
    item=ParagraphStyle('item',fontSize=9.6,leading=16,spaceAfter=5,**common)
    cell=ParagraphStyle('cell',fontSize=8.4,leading=12,spaceAfter=0,**common)
    lines=SOURCE.read_text(encoding='utf-8').splitlines()
    story=[];index=0
    while index<len(lines):
        line=lines[index].strip()
        if not line:
            index+=1;continue
        if line.startswith('# '):
            story.append(Spacer(1,26));story.append(Paragraph(rich(line[2:]),title));index+=1
        elif line.startswith('## '):
            story.append(PageBreak())
            story.append(Paragraph(rich(line[3:]),section));index+=1
        elif line.startswith('### '):
            story.append(Paragraph(rich(line[4:]),subsection));index+=1
        elif line == '<!-- pagebreak -->':
            story.append(PageBreak());index+=1
        elif re.match(r'^(\d+\. |[-*] )',line):
            story.append(Paragraph(rich(line),item));index+=1
        elif line.startswith('```'):
            index+=1;block=[]
            while index<len(lines) and not lines[index].startswith('```'):
                block.append(escape(lines[index]).replace(' ', '&#160;'));index+=1
            index+=1
            panel=Table([[Paragraph('<br/>'.join(block),code)]],colWidths=[480],hAlign='LEFT')
            panel.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),HexColor('#f1f5f1')),('LEFTPADDING',(0,0),(-1,-1),12),('RIGHTPADDING',(0,0),(-1,-1),12),('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),10)]))
            story.extend([panel,Spacer(1,14)])
        elif line.startswith('!['):
            match=re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)',line)
            path=(SOURCE.parent/match[2]).resolve()
            with PILImage.open(path) as img:
                width,height=img.size
            ratio=min(450/width,270/height)
            story.append(KeepTogether([Image(str(path),width=width*ratio,height=height*ratio),Spacer(1,8),Paragraph(escape(match[1]),small),Spacer(1,12)]))
            index+=1
        elif line.startswith('|'):
            rows=[]
            while index<len(lines) and lines[index].strip().startswith('|'):
                row=lines[index].strip().strip('|').split('|')
                if not all(re.fullmatch(r'\s*:?-+:?\s*',part) for part in row):
                    rows.append([Paragraph(rich(part.strip()),cell) for part in row])
                index+=1
            table=Table(rows,colWidths=[165,315] if len(rows[0])==2 else None,repeatRows=1,hAlign='LEFT')
            table.setStyle(TableStyle([
                ('BACKGROUND',(0,0),(-1,0),HexColor('#e1ebe1')),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),[HexColor('#f4f7f4'),HexColor('#ffffff')]),
                ('VALIGN',(0,0),(-1,-1),'TOP'),('BOX',(0,0),(-1,-1),.5,HexColor('#c7d5c9')),
                ('INNERGRID',(0,0),(-1,-1),.3,HexColor('#d5dfd6')),
                ('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10),
                ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
            story.extend([table,Spacer(1,12)])
        else:
            paragraph=[line];index+=1
            while index<len(lines) and lines[index].strip() and not lines[index].startswith(('#','|','![','```','<!-- pagebreak -->')) and not re.match(r'^(\d+\. |[-*] )',lines[index]):
                paragraph.append(lines[index].strip());index+=1
            story.append(Paragraph(rich(' '.join(paragraph)),body))
    def footer(canvas,doc):
        canvas.setStrokeColor(HexColor('#cbd8ce'));canvas.line(48,42,A4[0]-48,42)
        canvas.setFont('Project',8);canvas.setFillColor(HexColor('#607765'))
        canvas.drawString(48,28,'ExplorerDict / 技术设计 / 1.0.20261025.3')
        canvas.drawRightString(A4[0]-48,28,str(doc.page))
    doc=SimpleDocTemplate(str(OUTPUT),pagesize=A4,rightMargin=48,leftMargin=48,topMargin=44,bottomMargin=58,title='探索词典 · 技术设计',author='ExplorerDict')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    print(OUTPUT)


if __name__=='__main__':
    main()
