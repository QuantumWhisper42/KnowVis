/* Source: KnowVis_cameraready/fixed_sections/04_experiments.tex,
   Figure metric_comparison_clip_score_vqa_score, panel (b).
   L2 radar values come from the figure workbook (4-decimal precision),
   cross-checked against the published vector PDF. */
(function () {
  'use strict';
  function initKnowVisDiagrams() {
    const tour = document.getElementById('frameworkExplorer');
    if (tour && !tour.dataset.initialized) {
      tour.dataset.initialized = 'true';
      const tabs = [...tour.querySelectorAll('[role="tab"]')];
      const panels = [...tour.querySelectorAll('[role="tabpanel"]')];
      const play = document.getElementById('frameworkPlay');
      const status = document.getElementById('frameworkStatus');
      const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
      let current = 0, playing = !motion.matches, hovered = false, focused = false, elapsed = 0, previous = 0, visible = true;
      const duration = 8500;
      function updateStatus() {
        const paused = hovered || focused || !visible;
        status.textContent = 'Step ' + (current + 1) + ' of 4 · ' + (playing ? (paused ? 'Paused while exploring' : 'Auto tour') : 'Manual exploration');
        play.disabled = motion.matches;
        play.title = motion.matches ? 'Automatic playback is disabled by your reduced-motion preference.' : '';
        play.innerHTML = playing ? 'Pause tour <span aria-hidden="true">Ⅱ</span>' : 'Play tour <span aria-hidden="true">▷</span>';
        play.setAttribute('aria-pressed', String(playing));
      }
      function selectStep(index) {
        current = (index + tabs.length) % tabs.length;
        elapsed = 0;
        tabs.forEach((tab, i) => { tab.setAttribute('aria-selected', String(i === current)); tab.tabIndex = i === current ? 0 : -1; tab.style.setProperty('--progress', '0%'); panels[i].hidden = i !== current; });
        updateStatus();
      }
      tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => selectStep(index));
        tab.addEventListener('keydown', event => {
          const direction = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 0;
          if (direction || event.key === 'Home' || event.key === 'End') {
            event.preventDefault();
            selectStep(event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : current + direction);
            tabs[current].focus();
          }
        });
      });
      play.addEventListener('click', () => { playing = !playing; elapsed = 0; updateStatus(); });
      const stage = tour.querySelector('.kv-stage');
      stage.addEventListener('pointerenter', event => { if (event.pointerType === 'mouse' || event.pointerType === 'pen') { hovered = true; elapsed = 0; tabs[current].style.setProperty('--progress', '0%'); updateStatus(); } });
      stage.addEventListener('pointerleave', () => { hovered = false; elapsed = 0; updateStatus(); });
      tour.addEventListener('focusin', () => { focused = true; elapsed = 0; updateStatus(); });
      tour.addEventListener('focusout', () => { window.setTimeout(() => { focused = tour.contains(document.activeElement); elapsed = 0; updateStatus(); }, 0); });
      document.addEventListener('visibilitychange', () => { elapsed = 0; });
      motion.addEventListener('change', () => { if (motion.matches) playing = false; elapsed = 0; updateStatus(); });
      if ('IntersectionObserver' in window) new IntersectionObserver(entries => { visible = entries[0].isIntersecting; elapsed = 0; updateStatus(); }, { threshold: 0.15 }).observe(tour);
      function tick(time) {
        const delta = previous ? Math.min(time - previous, 100) : 0;
        previous = time;
        if (playing && !motion.matches && !hovered && !focused && visible && !document.hidden) {
          elapsed += delta;
          if (elapsed >= duration) selectStep(current + 1);
          tabs[current].style.setProperty('--progress', ((elapsed / duration) * 100) + '%');
        }
        requestAnimationFrame(tick);
      }
      selectStep(0);
      requestAnimationFrame(tick);
    }
    const chart = document.getElementById('metricPlot');
    if (chart && !chart.dataset.initialized) {
      chart.dataset.initialized = 'true';
      /* Paper Figure 4(b): L2-level consistency pattern across 12 subjects.
         Exact values from the figure workbook l2_metric_consistency_radar.xlsx
         (Sheet2, 4-decimal precision); cross-checked against the published
         vector figure KnowVis_cameraready/figures/l2_metric_consistency_radar.pdf.
         Per row: Human GT, KnowVis, VQAScore, CLIPScore. */
      const subjects = [
        { name: ['Physics'], values: [0.6565, 0.6853, 0.7493, 0.3133] },
        { name: ['Climate &', 'Astronomy'], values: [0.5362, 0.5631, 0.6183, 0.3103] },
        { name: ['Chemistry'], values: [0.5623, 0.6061, 0.6776, 0.2948] },
        { name: ['Biology'], values: [0.4949, 0.5777, 0.7397, 0.3054] },
        { name: ['Physical', 'Geography'], values: [0.6556, 0.6815, 0.6741, 0.3000] },
        { name: ['Mathematics'], values: [0.7065, 0.7307, 0.7224, 0.3040] },
        { name: ['History &', 'Culture'], values: [0.6074, 0.6126, 0.6530, 0.3184] },
        { name: ['Humanities &', 'Architecture'], values: [0.5849, 0.5585, 0.6916, 0.3083] },
        { name: ['Systems &', 'Processes'], values: [0.7072, 0.7206, 0.7959, 0.3211] },
        { name: ['General Social', 'Knowledge'], values: [0.7048, 0.7276, 0.8361, 0.3274] },
        { name: ['Language &', 'Writing'], values: [0.8109, 0.8109, 0.6722, 0.2966] },
        { name: ['Occupations &', 'Identities'], values: [0.7081, 0.7206, 0.7671, 0.3070] }
      ];
      const series = [
        { key: 'gt', name: 'Human GT', color: '#082845', width: 1.6, dash: '5 3.5', fill: 'none',
          note: 'Human GT: per-subject pass rates from human labels — the reference profile.' },
        { key: 'knowvis', name: 'KnowVis', color: '#397f77', width: 2.4, dash: 'none', fill: 'rgba(57,127,119,.08)',
          note: 'KnowVis: the structured VQA judge — within 0.09 of GT on every subject.' },
        { key: 'vqascore', name: 'VQAScore', color: '#7c94af', width: 1.5, dash: 'none', fill: 'none',
          note: 'VQAScore: inflates unevenly — above GT on 11 of 12 subjects, by up to 0.24.' },
        { key: 'clipscore', name: 'CLIPScore', color: '#c2cbd7', width: 1.5, dash: 'none', fill: 'none',
          note: 'CLIPScore: nearly flat between 0.29 and 0.33 — the profile carries no subject signal.' }
      ];
      const ns = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(ns, 'svg');
      svg.setAttribute('viewBox', '0 0 460 420');
      svg.setAttribute('role', 'group');
      svg.setAttribute('aria-label', 'Radar chart of per-subject scores for human GT, KnowVis, VQAScore, and CLIPScore across 12 L2 subjects. Exact values available in the table below.');
      function element(tag, attrs, text, parent = svg) {
        const node = document.createElementNS(ns, tag);
        Object.entries(attrs).forEach(([key, val]) => node.setAttribute(key, val));
        if (text !== undefined) node.textContent = text;
        parent.appendChild(node); return node;
      }
      const cx = 230, cy = 208, R = 148;
      const angle = k => (90 - k * 30) * Math.PI / 180;
      const point = (k, r) => [cx + r * Math.cos(angle(k)), cy - r * Math.sin(angle(k))];
      // grid rings and tick labels
      [0.2, 0.4, 0.6, 0.8, 1].forEach(step => {
        const pts = subjects.map((_, k) => point(k, step * R).map(n => n.toFixed(1)).join(',')).join(' ');
        element('polygon', { points: pts, fill: step === 1 ? '#f7fafc' : 'none', stroke: step === 1 ? '#c9d6df' : '#e4ebf0', 'stroke-width': step === 1 ? 1.2 : 1 });
        element('text', { x: cx - 7, y: cy - step * R + 4, 'text-anchor': 'end', 'font-size': 10, fill: '#97a7b3' }, step.toFixed(1));
      });
      // spokes and axis labels
      const spokes = [], labels = [];
      subjects.forEach((subject, k) => {
        const [x, y] = point(k, R);
        spokes.push(element('line', { x1: cx, y1: cy, x2: x, y2: y, stroke: '#e4ebf0', 'stroke-width': 1 }));
        const [lx, ly] = point(k, R + 20);
        const anchor = Math.abs(Math.cos(angle(k))) < .3 ? 'middle' : Math.cos(angle(k)) > 0 ? 'start' : 'end';
        const lines = subject.name;
        const firstY = ly + (lines.length > 1 ? -2 : 4);
        const text = element('text', { x: lx, y: firstY, 'text-anchor': anchor, 'font-size': 11, fill: '#54687a' });
        element('tspan', { x: lx }, lines[0], text);
        if (lines[1]) element('tspan', { x: lx, dy: 13 }, lines[1], text);
        labels.push(text);
      });
      // series polygons
      const polygons = series.map((s, si) => {
        const pts = subjects.map((subject, k) => point(k, subject.values[si] * R).map(n => n.toFixed(1)).join(',')).join(' ');
        return element('polygon', { points: pts, fill: s.fill, stroke: s.color, 'stroke-width': s.width, 'stroke-dasharray': s.dash, 'stroke-linejoin': 'round', class: 'kv-radar-series', 'data-series': s.key });
      });
      const readout = document.getElementById('metricReadout');
      const defaultText = readout.textContent;
      // axis hover: transparent sectors as hit areas
      subjects.forEach((subject, k) => {
        const a1 = angle(k) + Math.PI / 12, a2 = angle(k) - Math.PI / 12;
        const p1 = [cx + R * Math.cos(a1), cy - R * Math.sin(a1)];
        const p2 = [cx + R * Math.cos(a2), cy - R * Math.sin(a2)];
        const sector = element('path', { d: `M${cx},${cy} L${p1[0].toFixed(1)},${p1[1].toFixed(1)} A${R},${R} 0 0 0 ${p2[0].toFixed(1)},${p2[1].toFixed(1)} Z`, fill: 'transparent', 'pointer-events': 'all' });
        const name = subject.name.join(' ');
        const show = () => {
          readout.textContent = name + ': GT ' + subject.values[0].toFixed(2) + ' · KnowVis ' + subject.values[1].toFixed(2) + ' · VQAScore ' + subject.values[2].toFixed(2) + ' · CLIPScore ' + subject.values[3].toFixed(2);
          spokes[k].setAttribute('stroke', '#9db3c0'); spokes[k].setAttribute('stroke-width', '1.6');
          labels[k].setAttribute('fill', '#082845');
        };
        const hide = () => {
          readout.textContent = defaultText;
          spokes[k].setAttribute('stroke', '#e4ebf0'); spokes[k].setAttribute('stroke-width', '1');
          labels[k].setAttribute('fill', '#54687a');
        };
        sector.addEventListener('pointerenter', show);
        sector.addEventListener('pointerleave', hide);
        element('title', {}, name, sector);
      });
      // legend hover/focus: isolate one series
      document.querySelectorAll('#metricLegend span').forEach(span => {
        span.setAttribute('tabindex', '0');
        const key = span.dataset.series;
        const si = series.findIndex(s => s.key === key);
        const on = () => {
          polygons.forEach((p, pi) => p.setAttribute('opacity', pi === si ? 1 : .15));
          readout.textContent = series[si].note;
        };
        const off = () => {
          polygons.forEach(p => p.setAttribute('opacity', 1));
          readout.textContent = defaultText;
        };
        span.addEventListener('pointerenter', on); span.addEventListener('focus', on);
        span.addEventListener('pointerleave', off); span.addEventListener('blur', off);
      });
      chart.appendChild(svg);
    }
  }
  window.initKnowVisDiagrams = initKnowVisDiagrams;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initKnowVisDiagrams, {once:true});
  else initKnowVisDiagrams();
}());
