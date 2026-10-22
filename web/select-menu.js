/* In-page listboxes avoid native Qt/Windows popup painting and DPI artifacts. */
(() => {
  let current = null, serial = 0;
  function close(restore = false) {
    if (!current) return;
    const {button, menu} = current;
    current = null;
    if (menu.matches(':popover-open')) menu.hidePopover();
    menu.remove(); button.setAttribute('aria-expanded', 'false');
    if (restore && button.isConnected) button.focus();
  }
  function enhance(select) {
    if (select.multiple || select.dataset.choiceReady) return;
    select.dataset.choiceReady = '1';
    const wrapper = document.createElement('span'); wrapper.className = 'choice-select';
    select.before(wrapper); wrapper.append(select);
    select.classList.add('choice-native'); select.tabIndex = -1; select.setAttribute('aria-hidden','true');
    const button = document.createElement('button'); button.type = 'button';
    button.className = 'choice-select-button'; button.dataset.selectFor = select.id;
    button.setAttribute('role','combobox'); button.setAttribute('aria-haspopup','listbox');
    button.setAttribute('aria-expanded','false');
    const label = document.createElement('span'); label.className = 'choice-value';
    const caret = document.createElement('span'); caret.className = 'choice-caret'; caret.innerHTML = '<svg viewBox="0 0 16 16" width="12" height="12"><path d="m4 6 4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'; caret.setAttribute('aria-hidden','true');
    button.append(label,caret); wrapper.append(button);
    function sync() {
      const value = select.selectedOptions[0]?.textContent || '暂无选项';
      if (label.textContent !== value) label.textContent = value;
      const disabled = select.disabled || !select.options.length;
      if(button.disabled !== disabled) button.disabled = disabled;
      button.setAttribute('aria-label', select.getAttribute('aria-label') || ({'map-depth':'导图精度','link-target':'目标文章'}[select.id]) || '选择内容');
    }
    select.addEventListener('change', sync); select._syncChoice = sync; sync();
    function open(key) {
      sync(); if (button.disabled) return;
      if (current?.button === button) {close(); return;}
      close();
      const menu = document.createElement('div'); menu.className = 'choice-menu';
      menu.id = 'choice-menu-' + (++serial); menu.setAttribute('role','listbox'); menu.setAttribute('popover','manual');
      button.setAttribute('aria-controls',menu.id); button.setAttribute('aria-expanded','true');
      const items = [...select.options].map((option,index) => {
        const item = document.createElement('button'); item.type = 'button'; item.className = 'choice-option';
        item.setAttribute('role','option'); item.setAttribute('aria-selected',String(option.selected)); item.disabled = option.disabled;
        item.textContent = option.textContent; item.tabIndex = -1;
        item.onclick = () => {
          select.selectedIndex = index; sync(); close(true);
          select.dispatchEvent(new Event('input',{bubbles:true}));
          select.dispatchEvent(new Event('change',{bubbles:true}));
        };
        menu.append(item); return item;
      });
      (select.closest('dialog') || document.body).append(menu);
      current = {button,menu}; menu.showPopover();
      const rect = button.getBoundingClientRect(), margin = 8;
      menu.style.width = Math.min(Math.max(rect.width,160),innerWidth-margin*2) + 'px';
      const desired = Math.min(items.length*38+12,300), below = innerHeight-rect.bottom-margin;
      const above = rect.top-margin;
      const useAbove = below < desired && above > below;
      const height = Math.max(44,Math.min(desired,useAbove?above:below));
      menu.style.maxHeight = height+'px';
      menu.style.left = Math.max(margin,Math.min(rect.left,innerWidth-menu.offsetWidth-margin))+'px';
      menu.style.top = (useAbove?Math.max(margin,rect.top-menu.offsetHeight-5):rect.bottom+5)+'px';
      let active = Math.max(0,select.selectedIndex);
      if(key==='Home') active=0;
      if(key==='End') active=items.length-1;
      const focus = () => { items[active]?.focus(); items[active]?.scrollIntoView({block:'nearest'}); };
      focus();
      menu.onkeydown = e => {
        if(e.key==='Escape'){e.preventDefault();close(true);return;}
        if(e.key==='Tab'){close(true);return;}
        if(!['ArrowDown','ArrowUp','Home','End'].includes(e.key))return;
        e.preventDefault();
        const direction=e.key==='ArrowUp'?-1:1;
        active=e.key==='Home'?0:e.key==='End'?items.length-1:(active+direction+items.length)%items.length;
        for(let n=0;n<items.length&&items[active].disabled;n++)active=(active+direction+items.length)%items.length;
        focus();
      };
    }
    button.onclick = () => open();
    button.onkeydown = e => {if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();open(e.key);}};
  }
  function update() {
    document.querySelectorAll('select').forEach(select => {enhance(select);select._syncChoice?.();});
    if(current && (!current.button.isConnected || current.button.disabled))close();
  }
  new MutationObserver(update).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled','selected']});
  document.addEventListener('pointerdown',e=>{if(current&&!current.menu.contains(e.target)&&!current.button.contains(e.target))close();},true);
  window.addEventListener('resize',()=>close());
  document.addEventListener('scroll',e=>{if(current&&!current.menu.contains(e.target))close();},true);
  document.addEventListener('close',()=>close(),true);
  update();
})();
