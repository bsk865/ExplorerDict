"""Recolor existing widgets without changing focus, drafts, or window visibility."""
import tkinter as tk
from . import theme


def palette(tokens):
    keys={'BG':'bg','PANEL':'bg','PANEL_ALT':'quiet','HOVER_BG':'soft','BORDER':'line','BORDER_STRONG':'strong',
          'CARD_BG':'surface','CARD_BG_HOVER':'soft','CARD_BORDER':'line','CARD_BORDER_HOVER':'accent-line',
          'RAIL_THUMB':'strong','RAIL_THUMB_ACTIVE':'accent','TEXT':'ink','TEXT_MUTED':'muted','TEXT_FAINT':'muted',
          'TEXT_BODY':'ink','ACCENT':'accent','ACCENT_SOFT':'accent-soft','OUTLINE':'strong',
          'OK':'ink','WARN':'ink','ERR':'ink','OK_SOFT':'quiet','WARN_SOFT':'quiet','ERR_SOFT':'quiet'}
    mapping={}
    for name,key in keys.items():
        old=getattr(theme,name);new=tokens.get(key,old).upper()
        mapping[old.lower()]=new
        setattr(theme,name,new)
    return mapping


def apply(root,panel,tokens):
    mapping=palette(tokens)
    def recolor(widget):
        for key in ('background','foreground','insertbackground','selectbackground','selectforeground','highlightbackground','highlightcolor','activebackground','activeforeground','disabledforeground'):
            try:
                old=str(widget.cget(key)).lower()
                if old in mapping:widget.configure(**{key:mapping[old]})
            except (tk.TclError,KeyError):pass
        if isinstance(widget,tk.Canvas):
            for item in widget.find_all():
                for key in ('fill','outline'):
                    try:
                        old=widget.itemcget(item,key).lower()
                        if old in mapping:widget.itemconfigure(item,**{key:mapping[old]})
                    except tk.TclError:pass
        if isinstance(widget,tk.Text):
            for tag in widget.tag_names():
                for key in ('foreground','background'):
                    old=widget.tag_cget(tag,key).lower()
                    if old in mapping:widget.tag_configure(tag,**{key:mapping[old]})
        restyle=getattr(widget,'_restyle',None)
        if callable(restyle):restyle()
        for child in widget.winfo_children():recolor(child)
    recolor(root)
    for obj in vars(panel).values():
        if type(obj).__module__!='app.ui.widgets':continue
        for name in ('_card_bg','_thumb_color','_active_color','_bg','_color'):
            old=getattr(obj,name,None)
            if isinstance(old,str) and old.lower() in mapping:setattr(obj,name,mapping[old.lower()])
    panel.refresh_terms(force=True)
    panel._render_border(force=True)
