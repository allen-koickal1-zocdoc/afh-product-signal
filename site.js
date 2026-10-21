(function(){
  var root=document.documentElement,key='afh-theme';
  try{var s=localStorage.getItem(key);if(s)root.setAttribute('data-theme',s);}catch(e){}
  document.addEventListener('DOMContentLoaded',function(){
    var b=document.querySelector('button.theme');
    function isDark(){return root.getAttribute('data-theme')==='dark'||(!root.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme: dark)').matches);}
    if(b){b.setAttribute('aria-pressed',String(isDark()));
    b.addEventListener('click',function(){
      var next=isDark()?'light':'dark';root.setAttribute('data-theme',next);
      b.setAttribute('aria-pressed',String(next==='dark'));
      try{localStorage.setItem(key,next);}catch(e){}
    });}
    var sel=document.querySelector('select.months');
    if(sel)sel.addEventListener('change',function(){location.href=sel.value;});
    var tip=document.getElementById('tip');
    document.querySelectorAll('[data-tip]').forEach(function(el){
      function show(ev){tip.textContent=el.getAttribute('data-tip');tip.style.display='block';move(ev);}
      function move(ev){var b=el.getBoundingClientRect(),r=ev&&ev.clientX!==undefined?ev:{clientX:b.left,clientY:b.top};
        tip.style.left=Math.min(r.clientX + 12, innerWidth - tip.offsetWidth - 8)+'px';tip.style.top=Math.max(4, r.clientY - tip.offsetHeight - 8)+'px';}
      el.addEventListener('mouseenter',show);el.addEventListener('mousemove',move);
      el.addEventListener('mouseleave',function(){tip.style.display='none';});
      el.addEventListener('focus',function(){show({});});el.addEventListener('blur',function(){tip.style.display='none';});
      el.addEventListener('keydown',function(e){if(e.key==='Escape')tip.style.display='none';});
    });
    document.querySelectorAll('button.toggle[aria-controls]').forEach(function(btn){
      btn.addEventListener('click',function(){
        var t=document.getElementById(btn.getAttribute('aria-controls'));
        var open=btn.getAttribute('aria-expanded')==='true';
        btn.setAttribute('aria-expanded',String(!open));t.hidden=open;
        btn.textContent=open?'Show as table':'Hide table';
      });
    });
  });
})();
