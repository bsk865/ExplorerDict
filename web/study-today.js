function weatherLabel(code){
  if(code===0)return '晴';if([1,2,3].includes(code))return '多云';if([45,48].includes(code))return '雾';
  if([51,53,55,56,57].includes(code))return '毛毛雨';if([61,63,65,66,67,80,81,82].includes(code))return '雨';
  if([71,73,75,77,85,86].includes(code))return '雪';if([95,96,99].includes(code))return '雷雨';return '天气';
}
async function renderStudyWeather(){
  const box=$('#study-weather');if(!box)return;
  try{const w=await api('weather');if(!box.isConnected)return;
    box.innerHTML=w.status==='unconfigured'?`<span>天气</span>${button('study-city','选择城市')}`:
      `<div class="study-weather-symbol ${w.temperature!=null?'weather-'+weatherIcon(w.code):''}" aria-hidden="true">${studyIcon(w.temperature!=null?weatherIcon(w.code):'cloud')}</div><div class="study-weather-copy"><strong>${esc(w.city.name)}</strong><p>${w.temperature!=null?`${w.temperature}°C · ${weatherLabel(w.code)}`:'暂时无法获取天气'}</p><small>${w.status==='stale'?'更新失败 · ':''}${w.observed_at?'更新于 '+esc(w.observed_at.replace('T',' ')):''}</small></div><div class="study-weather-actions">${button('study-city','更换城市')}<small>Open-Meteo</small></div>`;
  }catch(err){if(box.isConnected)box.textContent=err.message;}
}
const todayStudyPage=studyPage;
studyPage=async function(page='themes'){
  if(await todayStudyPage(page)===false)return false;
  if(page==='today'&&state.page==='study'&&study.page==='today'){
    $('#main .page-heading').insertAdjacentHTML('afterend',`<section class="study-day-strip"><div class="study-clock"><time id="study-clock"></time><span>此刻</span></div><div id="study-weather" class="study-weather">正在获取天气…</div><img src="${avatar()}" alt="小探"></section>`);
    $('#study-clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});renderStudyWeather();
  }
};
function weatherIcon(code){if(code===0)return 'sun';if([45,48].includes(code))return 'fog';if([71,73,75,77,85,86].includes(code))return 'snow';if([95,96,99].includes(code))return 'storm';if(code>=51&&code<=82)return 'rain';return 'cloud';}
setInterval(()=>{const c=$('#study-clock');if(c){const time=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});if(c.textContent!==time)c.textContent=time;}},1000);
const todayDispatch=dispatch;
dispatch=async function(action){
  if(action==='study-city'){
    modal('选择天气城市',`<form id="study-city-form"><label class="form-field">城市<input name="city" required maxlength="100" autocomplete="off" placeholder="城市名称或拼音"></label><button type="submit" class="primary">查找</button></form><div id="study-city-results"></div><p class="modal-note">仅向 Open-Meteo 查询城市与天气，不读取设备定位。</p>`);
    $('#study-city-form').onsubmit=async e=>{e.preventDefault();const target=$('#study-city-results');target.textContent='正在查找…';try{const result=await api('weather-cities',{name:new FormData(e.target).get('city')});if(!target.isConnected)return;target.innerHTML=result.cities.map((c,i)=>`<button data-study-city="${i}"><strong>${esc(c.name)}</strong><small>${esc(c.region)} · ${esc(c.country)}</small></button>`).join('')||'<p>没有找到城市，请试试拼音或其他名称。</p>';target.querySelectorAll('button').forEach(el=>el.onclick=async()=>{try{await api('weather-city',{city:result.cities[Number(el.dataset.studyCity)]});$('#dialog').close();await renderStudyWeather();}catch(err){toast(err.message);}});}catch(err){target.textContent=err.message;}};return;
  }
  return todayDispatch(action);
};
