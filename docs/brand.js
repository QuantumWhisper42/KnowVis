/* A quiet field of color, revealed through the wordmark. No external renderer.
   Two procedural looks, drawn at random per page load: fbm "flow" and two-scale
   drifting Voronoi cells (fine grain under large plates). The color is masked
   per letter, not by a straight edge: "Vis" stays vivid while "Know" keeps a
   dim wash until hover lifts it. The pointer pulls the light: position sets the
   offset, speed widens the radius. At rest the glow sits on the visual center
   of the word — the left half of the "w" — wide enough to reach "n" and "i". */
(() => {
  const host = document.getElementById('livingBrand');
  if (!host) return;
  const ink = host.querySelector('.brand-ink'), canvas = host.querySelector('canvas');
  const ctx = canvas.getContext('2d');
  const texture = document.createElement('canvas'), scratch = document.createElement('canvas');
  const maskKnow = document.createElement('canvas'), maskVis = document.createElement('canvas');
  const sctx = scratch.getContext('2d');
  const gl = texture.getContext('webgl', {alpha:true, premultipliedAlpha:false, antialias:false});
  const quiet = matchMedia('(prefers-reduced-motion: reduce)');
  if (!gl || !ctx || !sctx) { host.classList.add('spectrum-fallback'); return; }
  const vertex = `attribute vec2 p; varying vec2 uv; void main(){uv=p*.5+.5;gl_Position=vec4(p,0.,1.);}`;
  const fragment = `precision mediump float;
    varying vec2 uv; uniform float t,mode,aspect,strength,radius,greymix; uniform vec2 center;
    float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
    vec2 hash2(vec2 p){return fract(sin(vec2(dot(p,vec2(127.1,311.7)),dot(p,vec2(269.5,183.3))))*43758.5453);}
    float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);return mix(mix(hash(i),hash(i+vec2(1,0)),f.x),mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),f.x),f.y);}
    float fbm(vec2 p){float v=0.;float a=.5;for(int i=0;i<4;i++){v+=a*noise(p);p=p*2.03+1.3;a*=.5;}return v;}
    vec3 palette(float x){return .53+.38*cos(6.28318*(vec3(1.,1.,1.)*x+vec3(.02,.32,.60)));}
    vec3 voro(vec2 p){vec2 g=floor(p),fr=fract(p);float f1=8.,f2=8.;vec2 id=vec2(0.);
      for(int y=-1;y<=1;y++){for(int x=-1;x<=1;x++){
        vec2 o=vec2(float(x),float(y));vec2 h=hash2(g+o);
        vec2 r=o+.5+.38*sin(t*.25+6.2831*h)-fr;float d=dot(r,r);
        if(d<f1){f2=f1;f1=d;id=h;}else if(d<f2){f2=d;}
      }}
      return vec3(sqrt(f1),sqrt(f2),hash(id*1.31+.17));}
    void main(){vec2 q=uv;vec3 col;
      if(mode<.5){vec2 warp=vec2(fbm(q*3.+t*.035),fbm(q*3.+vec2(2.7,-t*.04)));
        col=palette(fbm(q*4.+warp*3.+t*.045)+t*.018);}
      else{vec2 pa=vec2(q.x*aspect,q.y);
        vec3 big=voro(pa*2.2),fine=voro(pa*9.+11.7);
        col=palette(big.z*.55+fine.z*.45+.04*sin(t*.25+big.z*6.2831)+t*.018);
        col*=1.-.34*(1.-smoothstep(0.,.035,big.y-big.x))-.18*(1.-smoothstep(0.,.02,fine.y-fine.x));
      }
      float grey=dot(col,vec3(.2126,.7152,.0722));
      col=mix(vec3(grey)*.62,col,greymix);
      float d=length((q-center)*vec2(1.,.36));
      float spot=exp(-pow(d/max(radius,.01),2.)*2.2)*strength;
      gl_FragColor=vec4(col,spot);
    }`;
  function shader(type,source){const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error('shader');return s;}
  let program;
  try{program=gl.createProgram();gl.attachShader(program,shader(gl.VERTEX_SHADER,vertex));gl.attachShader(program,shader(gl.FRAGMENT_SHADER,fragment));gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error('program');}
  catch{host.classList.add('spectrum-fallback');return;}
  gl.useProgram(program);const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),gl.STATIC_DRAW);
  const pos=gl.getAttribLocation(program,'p');gl.enableVertexAttribArray(pos);gl.vertexAttribPointer(pos,2,gl.FLOAT,false,0,0);
  const u=Object.fromEntries(['t','mode','aspect','strength','radius','greymix','center'].map(k=>[k,gl.getUniformLocation(program,k)]));
  const mode=Math.floor(Math.random()*2);gl.uniform1f(u.mode,mode);host.dataset.spectrum=['flow','voronoi'][mode];
  let width=0,height=0,dpr=1,pad=20,hover=false,entered=0,last=0,time=Math.random()*100,visible=true;
  let homeX=.45,targetX=.45,targetY=.5,cx=.45,cy=.5,energy=0,amount=0,lastPoint=null,raf=0;
  function paintMask(mask,draw){
    const m=mask.getContext('2d');
    m.fillStyle='rgba(255,255,255,.06)';m.shadowColor='rgba(255,255,255,.26)';m.shadowBlur=14;draw(m);
    m.shadowBlur=0;m.fillStyle='#fff';draw(m);
  }
  function resize(){
    const r=ink.getBoundingClientRect(),style=getComputedStyle(ink);width=r.width+pad*2;height=r.height+pad*2;dpr=Math.min(devicePixelRatio||1,1.5);
    [canvas,texture,scratch,maskKnow,maskVis].forEach(c=>{c.width=Math.ceil(width*dpr);c.height=Math.ceil(height*dpr);});canvas.style.width=width+'px';canvas.style.height=height+'px';
    gl.viewport(0,0,texture.width,texture.height);
    const size=parseFloat(style.fontSize),font=`${style.fontWeight} ${size}px ${style.fontFamily}`;
    const probe=document.createElement('canvas').getContext('2d');probe.font=font;probe.letterSpacing=style.letterSpacing;
    const metric=probe.measureText('KnowVis'),know=probe.measureText('Know'),kno=probe.measureText('Kno'),vis=probe.measureText('Vis');
    const ascent=metric.fontBoundingBoxAscent||size*.9,descent=metric.fontBoundingBoxDescent||size*.22;
    const baseline=pad+(r.height-ascent-descent)/2+ascent,visX=pad+metric.width-vis.width;
    // Resting glow: visual center of the word — the left half of the "w".
    homeX=(pad+kno.width+(know.width-kno.width)*.25)/width;targetX=cx=homeX;
    const prep=m=>{m.setTransform(dpr,0,0,dpr,0,0);m.font=font;m.letterSpacing=style.letterSpacing;m.textBaseline='alphabetic';};
    paintMask(maskKnow,m=>{prep(m);m.fillText('Know',pad,baseline);});
    paintMask(maskVis,m=>{prep(m);m.fillText('Vis',visX,baseline);});
    gl.uniform1f(u.aspect,width/height);draw(performance.now());
  }
  function layer(mask,alpha){
    if(alpha<=.002)return;
    sctx.clearRect(0,0,scratch.width,scratch.height);
    sctx.globalCompositeOperation='source-over';sctx.drawImage(texture,0,0);
    sctx.globalCompositeOperation='destination-in';sctx.drawImage(mask,0,0);
    ctx.globalAlpha=Math.min(1,alpha);ctx.drawImage(scratch,0,0);ctx.globalAlpha=1;
  }
  function draw(now){
    cancelAnimationFrame(raf);if(now-last<33&&!quiet.matches){raf=requestAnimationFrame(draw);return;}const dt=Math.min((now-(last||now))/1000,.05);last=now;
    const ease=1-Math.exp(-dt*4);amount+=(Number(hover)-amount)*ease;energy*=Math.exp(-dt*2.8);
    cx+=(targetX-cx)*(1-Math.exp(-dt*3));cy+=(targetY-cy)*(1-Math.exp(-dt*3));
    const burst=hover?5*Math.exp(-(now-entered)/1600):0;time+=dt*(.55+burst+energy*3);
    const driftX=Math.sin(time*.08)*.025+Math.sin(time*.13)*.01,driftY=Math.sin(time*.11)*.07;
    gl.uniform1f(u.t,time);gl.uniform1f(u.radius,.46+amount*.12+energy*.32);
    gl.uniform2f(u.center,cx+driftX,cy+driftY);
    ctx.clearRect(0,0,canvas.width,canvas.height);
    gl.uniform1f(u.strength,.5+amount*.5);gl.uniform1f(u.greymix,.12+amount*.78);gl.drawArrays(gl.TRIANGLES,0,6);layer(maskKnow,.35+amount*.65);
    gl.uniform1f(u.strength,.8+amount*.2);gl.uniform1f(u.greymix,.92);gl.drawArrays(gl.TRIANGLES,0,6);layer(maskVis,1);
    if(visible&&!document.hidden&&!quiet.matches)raf=requestAnimationFrame(draw);
  }
  host.addEventListener('pointerenter',()=>{hover=true;entered=performance.now();lastPoint=null;if(!quiet.matches)draw(entered);});
  host.addEventListener('pointermove',e=>{const r=host.getBoundingClientRect(),now=performance.now();targetX=Math.min(homeX+.31,Math.max(homeX-.24,(e.clientX-r.left+pad)/width));targetY=Math.min(.65,Math.max(.35,1-(e.clientY-r.top+pad)/height));if(lastPoint){const speed=Math.hypot(e.clientX-lastPoint.x,e.clientY-lastPoint.y)/Math.max(16,now-lastPoint.t);energy=Math.min(1,energy+speed*.18);}lastPoint={x:e.clientX,y:e.clientY,t:now};});
  host.addEventListener('pointerleave',()=>{hover=false;targetX=homeX;targetY=.5;lastPoint=null;});
  new ResizeObserver(resize).observe(ink);
  new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;if(visible){last=0;draw(performance.now());}else cancelAnimationFrame(raf);}).observe(host);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)cancelAnimationFrame(raf);else if(visible){last=0;draw(performance.now());}});
  quiet.addEventListener('change',()=>{last=0;draw(performance.now());});
})();
