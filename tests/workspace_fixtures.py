import io
from PIL import Image, ImageDraw

def demo(store):
    text = ['咖啡的一杯风味，从哪里来？', '咖啡的风味受到研磨、水温与接触时间共同影响。较细的研磨增加接触面积，但不能单凭粗细判断最终风味。',
            '图1用颗粒示意粗研磨和细研磨。同等咖啡质量下，较细颗粒有更大的总表面积。示意图只说明几何差异，不代表实验测量。',
            '图2比较短、中、长三种接触时间。时间延长通常增加萃取，但不同咖啡和冲煮条件下，结果并不一致。不能从这张示意图推断最佳时间。',
            '调整冲煮时，每次只改一个变量，记录口感，再与上一杯比较。记录能帮助回顾，但不能代替控制条件下的实验。']
    assets = []
    for mode in range(2):
        im = Image.new('RGB', (1000, 360), '#f7f2ea')
        d = ImageDraw.Draw(im)
        if mode == 0:
            for x, y, r in [(130, 130, 50), (270, 230, 45), (330, 100, 40)]:
                d.ellipse((x-r, y-r, x+r, y+r), fill='#ad7554')
            for y in range(80, 300, 65):
                for x in range(590, 900, 65):
                    d.ellipse((x, y, x+26, y+26), fill='#ad7554')
            d.line((500, 40, 500, 320), fill='#d4c6b7', width=3)
        else:
            for i, h in enumerate([70, 140, 220]):
                x = 150 + i*280
                d.rounded_rectangle((x, 300-h, x+120, 300), radius=12, fill=['#b2c4ac', '#ba9d7e', '#9d5c43'][i])
            d.line((100, 302, 900, 302), fill='#c4b6a7', width=3)
        out = io.BytesIO(); im.save(out, 'PNG')
        aid, size = store.image(out.getvalue()); assets.append(aid)
    blocks = [{'type': 'text', 'text': text[0]}, {'type': 'text', 'text': text[1]},
              {'type': 'image', 'asset': assets[0], 'caption': '图1 · 研磨颗粒对照（自编示意）'}, {'type': 'text', 'text': text[2]},
              {'type': 'image', 'asset': assets[1], 'caption': '图2 · 接触时间对照（自编示意）'}, {'type': 'text', 'text': text[3]}, {'type': 'text', 'text': text[4]}]
    labels = [('root', None, '一杯咖啡的风味', 0), ('grind', 'root', '研磨与接触面积', 1), ('size', 'grind', '较细颗粒', 2), ('area', 'grind', '更大的总表面积', 2), ('time', 'root', '时间与萃取', 3), ('long', 'time', '接触时间延长', 3), ('limit', 'time', '不推断最佳时间', 3), ('record', 'root', '调整与记录', 4), ('one', 'record', '每次只改一个变量', 4), ('compare', 'record', '与上一杯比较', 4)]
    graph = {'root': 'root', 'nodes': [{'id': n, 'parent': p, 'label': l, 'quote': text[i], 'kind': 'branch' if p is None or p == 'root' else 'concept'} for n,p,l,i in labels],
             'relations': [{'id': 'r1', 'src': 'size', 'dst': 'area', 'label': '同等质量下，总表面积更大', 'quote': text[2]}], 'bindings': []}
    return store.import_article('一杯咖啡的风味', blocks, origin='demo', graph=graph, quality={'demo': True})

