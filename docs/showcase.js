(() => {
  'use strict';
  const data=window.KNOWVIS_DATA;if(!data?.showcase)return;
  const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  const condensed=[
    ['A wall clock is visible.','Both hour and minute hands are present.','The hands point in opposite directions along one straight line.'],
    ['The diamond has a multi-faceted cut.','The diamond is colorless and transparent.','Light produces visible rainbow-colored spots or bands.'],
    ['The spacecraft has the characteristic appearance of Hubble.','The background is a space environment.','The spacecraft is free of ground support or a launch vehicle.'],
    ['The scene is underwater.','A small aquatic animal is present.','It has the characteristic features of an early frog tadpole with external gills.'],
    ['The scene shows a stone building’s entrance facade.','One or more entrance doorways are present.','The doorways have the characteristic Gothic pointed arch.'],
    ['One or more stone columns are present.','The view is a close-up of the columns.','The columns have the Doric features of the Parthenon.'],
    ['A wooden sailing ship sits on the water.','The ship has the features of an early medieval Viking longship.','The square sail is billowing and waves splash before the bow.'],
    ['The curve is an upward-opening parabola.','Its vertex lies at the intersection of the axes.','The curve is symmetric about the vertical axis.'],
    ['The lower part of the image shows the sea.','Lightning connects the sky or clouds to the sea surface.','The strike illuminates the surrounding water.'],
    ['Two pillars and a crossbar form a gateway.','The gateway has the features of a Japanese torii.','It marks an entrance toward a sacred precinct.']
  ];
  const tiny=[['Clock','Hands','Straight angle'],['Facets','Transparency','Dispersion'],['Hubble','Space','Free-floating'],['Underwater','Animal','External gills'],['Stone facade','Doorway','Pointed arch'],['Columns','Close-up','Doric order'],['Wooden ship','Longship','Sail & waves'],['Parabola','Vertex','Symmetry'],['Ocean','Lightning','Illumination'],['Gateway','Torii','Sacred precinct']];
  data.showcase.forEach((item,i)=>{item.displayChecks=condensed[i];item.tiny=tiny[i];});
  const order=[0,5,1,6,3,9,2,7,8,4];const cases=order.map(i=>data.showcase[i]);
  cases.push(...(data.generalizedShowcase||[]));
  const current={hero:0,case:0};
  function newsPreview(){return `<div class="news-preview"><span class="news-eyebrow">Generalized · Task preview</span><span class="news-year">2022</span><h4>World Cup Final</h4><p>An infographic that calls on real-world events, national symbols, and factual results.</p><div class="news-scope"><span>Events</span><span>National flags</span><span>Results</span></div><p class="figure-note">Released task · No generated image shown</p></div>`;}
  function deckBody(item,index){return `<div class="deck-topline"><span class="eyebrow">Quick peek</span><span>${item.benchmark==='Generalized'?'Generalized · Evaluated output':'Skill-Tree · Evaluated output'}</span></div><div class="deck-image">${item.image?`<img src="${esc(item.image)}" alt="${esc(item.prompt)}" width="1100" height="1100" ${index?'loading="lazy"':'fetchpriority="high"'}>`:newsPreview()}</div><p class="deck-caption">“${esc(item.prompt)}”</p><div class="deck-checks">${item.checks.map((c,i)=>`<span><b class="${c.judgment==='FAIL'?'fail':''}">${c.judgment==='UNRATED'?'○':c.judgment==='FAIL'?'×':'✓'}</b>${esc(item.tiny[i])}</span>`).join('')}</div><a class="deck-inspect" href="#protocol" data-inspect="${index}"><span>${item.benchmark==='Generalized'?'Selected automated judgments':'Selected human annotations'}</span><span aria-hidden="true">→</span></a>`;}
  const deck=$('#heroDeck');
  // One real card over two static card backs, both rotated clockwise in
  // steps around a pivot nudged along the diagonal — a deck slightly spread.
  // Pure decoration, never updated.
  ['deck-echo--1','deck-echo--2'].forEach(cls=>{const el=document.createElement('div');el.className='deck-echo '+cls;el.setAttribute('aria-hidden','true');deck.appendChild(el);});
  cases.forEach((item,i)=>{const card=document.createElement('article');card.className='deck-card'+(!i?' is-active':'');card.dataset.deck=String(i);card.innerHTML=deckBody(item,i);card.inert=!!i;card.setAttribute('aria-hidden',String(!!i));deck.appendChild(card);const dot=document.createElement('button');dot.type='button';dot.setAttribute('aria-label',`Show ${item.label}`);dot.setAttribute('aria-pressed',String(!i));dot.addEventListener('click',()=>{showHero(i);schedule();});$('#deckDots').appendChild(dot);});
  cases.forEach(item=>{if(item.image){const pre=new Image();pre.src=item.image;}});
  let timer=0,hovering=false,focused=false,visible=true,paused=reduced.matches;
  function showHero(i){current.hero=(i+cases.length)%cases.length;deck.querySelectorAll('.deck-card').forEach((card,j)=>{const active=j===current.hero;card.classList.toggle('is-active',active);card.inert=!active;card.setAttribute('aria-hidden',String(!active));});$('#deckDots').querySelectorAll('button').forEach((b,j)=>b.setAttribute('aria-pressed',String(j===current.hero)));$('#deckDomain').textContent=cases[current.hero].domain;}
  function schedule(){clearTimeout(timer);if(!paused&&!hovering&&!focused&&visible&&!document.hidden&&!reduced.matches)timer=setTimeout(()=>{showHero(current.hero+1);schedule();},6500);}
  const hero=$('.hero-showcase');hero.addEventListener('pointerenter',()=>{hovering=true;clearTimeout(timer);});hero.addEventListener('pointerleave',()=>{hovering=false;schedule();});hero.addEventListener('focusin',()=>{focused=true;clearTimeout(timer);});hero.addEventListener('focusout',e=>{if(!hero.contains(e.relatedTarget)){focused=false;schedule();}});
  $('#deckPause').addEventListener('click',()=>{paused=!paused;$('#deckPause').textContent=paused?'▶':'Ⅱ';$('#deckPause').setAttribute('aria-label',paused?'Play case slideshow':'Pause case slideshow');schedule();});
  $('#deckPrev').addEventListener('click',()=>{showHero(current.hero-1);schedule();});
  $('#deckNext').addEventListener('click',()=>{showHero(current.hero+1);schedule();});
  new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;schedule();}).observe(hero);document.addEventListener('visibilitychange',schedule);reduced.addEventListener('change',()=>{paused=reduced.matches;$('#deckPause').textContent=paused?'▶':'Ⅱ';$('#deckPause').setAttribute('aria-label',paused?'Play case slideshow':'Pause case slideshow');schedule();});if(paused){$('#deckPause').textContent='▶';$('#deckPause').setAttribute('aria-label','Play case slideshow');}
  deck.addEventListener('click',e=>{const a=e.target.closest('[data-inspect]');if(a)showCase(Number(a.dataset.inspect));});showHero(0);schedule();
  // Only three selected Required checks are staged; the source data remains intact.
  const select=$('#caseSelect');cases.forEach((item,i)=>{const option=document.createElement('option');option.value=i;option.textContent=`${item.domain} · ${item.label}`;select.appendChild(option);});
  const inspector=$('#caseInspector');let fadeToken=0;
  function fadeIn(){if(reduced.matches)return;inspector.classList.remove('is-presenting');const token=++fadeToken;requestAnimationFrame(()=>requestAnimationFrame(()=>{if(token===fadeToken)inspector.classList.add('is-presenting');}));}
  // Three cards per case, always: Skill-Tree shows three Required checks;
  // Generalized shows two Required plus its recorded Optional richness check.
  function showCase(i){current.case=(i+cases.length)%cases.length;const item=cases[current.case];select.value=String(current.case);
    $('.case-image').innerHTML=item.image?`<button type="button" class="image-expand" id="caseImageButton" aria-label="Enlarge ${esc(item.label)}"><img id="caseImage" src="${esc(item.image)}" alt="${esc(item.prompt)}" width="1100" height="1100"><span class="expand-label">Expand ↗</span></button>`:newsPreview();
    $('#caseCategory').textContent=item.image?item.category.join(' / '):'Generalized / Real-Time Information & Events';$('#casePrompt').textContent='“'+item.prompt+'”';
    const opt=item.optional;
    const cards=item.checks.slice(0,opt?2:3).map((check,j)=>{const verdict=check.judgment;return `<div class="selected-check" title="${esc(check.question)}"><div><small>${esc(check.component.replaceAll('_',' '))}</small><p>${esc(item.displayChecks[j])}</p></div><span class="check-stamp ${verdict==='FAIL'?'fail':verdict==='UNRATED'?'unrated':''}"><i aria-hidden="true">${verdict==='PASS'?'✓':verdict==='FAIL'?'×':'○'}</i>${verdict==='UNRATED'?'UNRATED':verdict}</span></div>`;});
    if(opt)cards.push(`<div class="selected-check is-optional" title="${esc(opt.question)}"><div><small>Richness · ${esc(opt.component.replaceAll('_',' '))}</small><p>${esc(opt.display)}</p></div><span class="check-stamp ${opt.judgment==='FAIL'?'fail':opt.judgment==='NA'?'unrated':''}"><i aria-hidden="true">${opt.judgment==='PASS'?'✓':opt.judgment==='FAIL'?'×':'○'}</i>${opt.judgment}</span></div>`);
    $('#caseChecks').innerHTML=cards.join('');
    $('#caseLabelSource').textContent=item.benchmark==='Generalized'?'Two selected Required checks plus the recorded Optional richness check (dashed border), condensed for display. Labels come from the recorded automated evaluation.':'Selected Required checks, condensed for display. Labels are human annotations; this is not the full checklist.';
    $('#casePosition').textContent=`${String(current.case+1).padStart(2,'0')} / ${cases.length} · ${item.domain}`;fadeIn();
  }
  select.addEventListener('change',e=>showCase(Number(e.target.value)));$('#casePrevious').addEventListener('click',()=>showCase(current.case-1));$('#caseNext').addEventListener('click',()=>showCase(current.case+1));$('.case-image').addEventListener('click',()=>{const item=cases[current.case];if(item.image)window.KnowVisOpenImage(item.image,item.prompt);});
  showCase(0);
  // An editorial mapping of authentic prompts, not synthetic dataset pairings.
  const pairs=data.dualView||[];pairs.forEach((pair,i)=>{const b=document.createElement('button');b.type='button';b.textContent=['Everyday optics','Architecture','Earth systems','Fuel & arithmetic'][i]||pair.title;b.addEventListener('click',()=>showPair(i));$('#pairTabs').appendChild(b);});
  function showPair(i){const pair=pairs[i];$('#pairTabs').querySelectorAll('button').forEach((b,j)=>b.setAttribute('aria-pressed',String(j===i)));$('#atomicPrompts').innerHTML=pair.skillTree.map(task=>`<article class="atomic-prompt"><p class="task-domain">${esc(task.category[1])}</p><h4>${esc(task.category.at(-1))}</h4><p>${esc(task.prompt)}</p></article>`).join('');$('#generalizedPrompt').innerHTML=`<article class="generalized-body"><span class="pair-kind">${esc(pair.kind)} · ${esc(pair.domain)}</span><p>${esc(pair.generalized.prompt)}</p><p class="pair-connection">${esc(pair.connection)}</p></article>`;}
  if(pairs.length)showPair(0);
  const rewrites=data.rewrite?.examples||[];rewrites.forEach((item,i)=>{const b=document.createElement('button');b.type='button';b.textContent=i?'Parthenon':'Clock';b.addEventListener('click',()=>showRewrite(i));$('#rewriteTabs').appendChild(b);});
  function showRewrite(i){$('#rewriteTabs').querySelectorAll('button').forEach((b,j)=>b.setAttribute('aria-pressed',String(j===i)));const ex=rewrites[i];$('#originalPrompt').textContent=ex.prompt;let html=esc(ex.rewrittenPrompt);(ex.marks||[]).forEach(m=>{html=html.split(esc(m)).join('<mark>'+esc(m)+'</mark>');});$('#expandedPrompt').innerHTML=html;$('#rewriteAdds').innerHTML=(ex.adds||[]).map(a=>`<div class="rewrite-add"><b>${esc(a.label)}</b><span>${esc(a.text)}</span></div>`).join('');$('#rewriteSource').textContent='Highlighting shows knowledge made drawable: entity features, relations, directions, and bindings added to the released visual guidance. These are not paired before/after generation results from the experiment above.';}
  if(rewrites.length)showRewrite(0);
})();
