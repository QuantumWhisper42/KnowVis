(() => {
  'use strict';
  const data = window.KNOWVIS_DATA;
  const $ = s => document.querySelector(s);
  const safe = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const menu = $('.menu-toggle');
  menu.addEventListener('click', () => {
    const open = menu.getAttribute('aria-expanded') !== 'true';
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
    $('#site-nav').classList.toggle('open', open);
  });
  $('#site-nav').addEventListener('click', e => {
    if (e.target.closest('a')) { menu.setAttribute('aria-expanded', 'false'); menu.setAttribute('aria-label', 'Open navigation'); $('#site-nav').classList.remove('open'); }
  });
  document.addEventListener('keydown', e => { if(e.key === 'Escape') { menu.setAttribute('aria-expanded','false');menu.setAttribute('aria-label','Open navigation');$('#site-nav').classList.remove('open'); } });

  const dialog = $('#lightbox');
  function openImage(src, caption) {
    $('#lightboxImage').src = src;
    $('#lightboxImage').alt = caption;
    $('#lightboxCaption').textContent = caption;
    if(!dialog.open) dialog.showModal();
  }
  window.KnowVisOpenImage = openImage;
  $('.lightbox-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', e => {
    const r = dialog.getBoundingClientRect();
    if(e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dialog.close();
  });
  document.querySelectorAll('[data-lightbox]').forEach(button => button.addEventListener('click', () => openImage(button.dataset.lightbox,button.dataset.caption)));

  if (data) {
    $('#exampleGallery').innerHTML = data.examples.map((item,i) => `<button type="button" class="example-card" data-example="${i}" aria-label="View ${safe(item.title)} example"><div class="example-image"><img src="${safe(item.image)}" width="900" height="900" alt="${safe(item.prompt)}" loading="lazy"></div><strong>${safe(item.title)}</strong><span>${safe(item.category[item.category.length-1])} ↗</span></button>`).join('');
    $('#exampleGallery').addEventListener('click', e => {
      const button = e.target.closest('[data-example]');if(!button)return;
      const item=data.examples[Number(button.dataset.example)];openImage(item.image,item.prompt+'\n'+item.category.join(' / '));
    });

  }

  // The ranking and the download link both use data/skill-tree.json.
  (async function loadLeaderboard(){
    const body=$('#leaderboardBody');
    if(!body)return;
    let leaderboard;
    try{
      const response=await fetch('data/skill-tree.json');
      if(!response.ok)throw new Error(String(response.status));
      leaderboard=await response.json();
      if(!Array.isArray(leaderboard.models)||!leaderboard.models.length||!leaderboard.sliceGroups)throw new Error('incomplete');
    }catch(err){
      body.innerHTML='<div class="empty-state">Failed to load leaderboard data.</div>';
      return;
    }
    const paperModels=new Set(['nano_banana_2','flux2_pro','recraft_v4','qwen_image_2','wan_2_7_pro','hunyuan_image_3','midjourney_v7','seedream_4_5','seedream_5_lite','kling_image_3_0_omni']);
    const colors=['#9acb76','#75b5a9','#e8d87d','#5fa1d7','#a4c884','#79b887','#93c4a0','#72a8c2','#94c26e','#d8ca84','#e2aa92','#a8a6df','#d7b3dd','#87c7e0','#b8c5d6'];
    const groupLabels={domain:'Domains',subdomain:'Subjects',education:'Education',operation:'Operations'};
    const groupKeys=Object.keys(leaderboard.sliceGroups).sort((a,b)=>(leaderboard.sliceGroups[a].order||0)-(leaderboard.sliceGroups[b].order||0));
    const state={group:groupKeys[0],slice:leaderboard.sliceGroups[groupKeys[0]].slices[0].key,scope:'all',search:''};
    const paperCount=leaderboard.models.filter(m=>paperModels.has(m.key)).length;
    const allOption=$('#modelScope').querySelector('option[value="all"]');
    if(allOption)allOption.textContent=`All ${leaderboard.models.length} released models`;
    groupKeys.forEach(key=>{
      const b=document.createElement('button');b.type='button';b.textContent=groupLabels[key]||leaderboard.sliceGroups[key].label;b.dataset.group=key;
      b.addEventListener('click',()=>{state.group=key;state.slice=leaderboard.sliceGroups[key].slices[0].key;renderSlices();renderRows();});$('#groupTabs').appendChild(b);
    });
    function renderSlices(){
      $('#groupTabs').querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.group===state.group)));
      $('#sliceTabs').replaceChildren();
      leaderboard.sliceGroups[state.group].slices.forEach(slice=>{
        const b=document.createElement('button');b.type='button';b.textContent=slice.label;b.setAttribute('aria-pressed',String(slice.key===state.slice));
        b.addEventListener('click',()=>{state.slice=slice.key;renderSlices();renderRows();});$('#sliceTabs').appendChild(b);
      });
    }
    function renderRows(){
      const slice=leaderboard.sliceGroups[state.group].slices.find(x=>x.key===state.slice);
      const all=leaderboard.models.map((m,i)=>({...m,score:Number(slice.values[i]),color:m.color||colors[i%colors.length]})).filter(m=>state.scope==='all'||paperModels.has(m.key)).sort((a,b)=>b.score-a.score||a.name.localeCompare(b.name));
      const query=state.search.toLowerCase().trim();const rows=all.filter(m=>m.name.toLowerCase().includes(query)||m.key.includes(query));
      const extended=leaderboard.models.length-paperCount;
      $('#sliceTitle').textContent=slice.label;
      $('#boardContext').textContent=state.scope==='all'?`${leaderboard.models.length} models in the public release: the paper's ${paperCount} models plus ${extended} extended evaluations.`:`The ${paperCount} models evaluated in the paper. Recraft V4 denotes the Standard variant.`;
      body.innerHTML=rows.length?rows.map(row=>{
        const rank=all.indexOf(row)+1;const displayName=row.key==='recraft_v4'?'Recraft V4 Standard':row.name;
        return `<div class="ranking-row" role="row" style="--row-color:${safe(row.color)}"><span class="rank ${rank<4?'top-rank':''}" role="cell">${String(rank).padStart(2,'0')}</span><div class="model" role="cell"><span class="logo"><img src="${safe(row.logo)}" alt="" width="24" height="24" loading="lazy"></span><span>${safe(displayName)}</span></div><div class="bar-track" role="cell" aria-label="${safe(displayName)} correctness ${row.score.toFixed(3)}"><div class="bar" data-width="${Math.min(100,Math.max(0,row.score*100))}"></div></div><span class="score" role="cell">${row.score.toFixed(3)}</span></div>`;
      }).join(''):'<div class="empty-state">No matching models. Try another name.</div>';
      requestAnimationFrame(()=>body.querySelectorAll('.bar').forEach(bar=>bar.style.width=bar.dataset.width+'%'));
    }
    $('#modelSearch').addEventListener('input',e=>{state.search=e.target.value;renderRows();});
    $('#modelScope').addEventListener('change',e=>{state.scope=e.target.value;renderRows();});
    renderSlices();renderRows();
  })();

  document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{
    const text=document.getElementById(button.dataset.copy).textContent;const original=button.textContent;
    try{
      if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(text);
      else{const area=document.createElement('textarea');area.value=text;area.style.cssText='position:fixed;left:-9999px';document.body.appendChild(area);area.select();if(!document.execCommand('copy'))throw new Error('Clipboard unavailable');area.remove();}
      button.textContent='Copied';$('#copyStatus').textContent='Copied to clipboard.';
    }catch{button.textContent='Select to copy';const range=document.createRange();range.selectNodeContents(document.getElementById(button.dataset.copy));const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);$('#copyStatus').textContent='Text selected. Copy it with your keyboard.';}
    setTimeout(()=>{button.textContent=original;},2000);
  }));

  if('IntersectionObserver' in window&&!reducedMotion){
    document.documentElement.classList.add('js-ready');
    const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.remove('waiting');observer.unobserve(entry.target);}}),{threshold:.06,rootMargin:'0px 0px -25px 0px'});
    document.querySelectorAll('.reveal').forEach(el=>{el.classList.add('waiting');observer.observe(el);});
  }
  if('IntersectionObserver' in window){
    const navObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){document.querySelectorAll('.site-nav a').forEach(a=>a.classList.toggle('active',a.getAttribute('href')==='#'+entry.target.id));}}),{rootMargin:'-12% 0px -65% 0px'});
    document.querySelectorAll('section[id]').forEach(el=>navObserver.observe(el));
  }
})();
