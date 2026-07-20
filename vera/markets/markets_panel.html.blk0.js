/* vera theme boot — paint cached theme before CSS parses, avoids flash */
  (function(){try{var d=document.documentElement,S=window.localStorage;
  var t=S.getItem('vera:ui:theme');if(t)d.setAttribute('data-theme',t);
  var v=JSON.parse(S.getItem('vera:ui:themeVars')||'null');
  if(v)for(var k in v)d.style.setProperty(k,v[k]);}catch(e){}})();