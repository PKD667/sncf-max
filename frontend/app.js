// TGV Max frontend app
var STATIONS=[],map=null,routeLayer=null;
var C={};          // API name -> [lat,lon]  (from backend)
var DISP={};       // API name -> pretty display label (from backend)

// helpers
function latlng(station){
  if(!station) return null;
  if(C[station]) return C[station];
  var u=station.toUpperCase();
  for(var k in C){if(u.indexOf(k.toUpperCase())!==-1||k.toUpperCase().indexOf(u)!==-1)return C[k]}
  return null;
}
// pretty label for a station name (backend-provided, with a JS fallback)
function disp(name){
  if(!name) return '';
  if(DISP[name]) return DISP[name];
  var s=name.replace(/\s*\(intramuros\)/i,'').replace(/\.+$/,'').trim();
  return s.toLowerCase().replace(/\b\w/g,function(c){return c.toUpperCase()})
          .replace(/\bTgv\b/g,'TGV').replace(/\bCdg\b/g,'CDG').replace(/\bSncf\b/g,'SNCF');
}
function ymd(d){return d.toISOString().slice(0,10)}
function trunc(s,n){return s&&s.length>n?s.slice(0,n)+'...':(s||'')}
function show(id){document.getElementById(id).style.display='block'}
function hide(id){document.getElementById(id).style.display='none'}
function loading(on,msg){document.getElementById('st').innerHTML=on?'<span class=spin></span>'+msg:''}
function hideDetail(){hide('detail')}

// init
fetch('/api/stations').then(function(r){return r.json()}).then(function(data){
  STATIONS=data.stations||[];
  STATIONS.forEach(function(s){
    DISP[s.name]=s.display;
    if(s.lat!=null&&s.lon!=null)C[s.name]=[s.lat,s.lon];
  });
  function pop(sel,def){var e=document.querySelector('select[name='+sel+']');
    STATIONS.forEach(function(s){var o=document.createElement('option');
      o.value=s.name;o.text=s.display;if(s.name===def)o.selected=true;e.appendChild(o)});}
  pop('origin','PARIS (intramuros)');pop('destination','LYON (intramuros)');
  document.querySelector('input[name=date]').value=ymd(new Date(Date.now()+7*86400000));
  drawStationMarkers();
}).catch(function(e){console.error('station load failed',e)});

function drawStationMarkers(){
  if(!map)return;
  for(var k in C){L.circleMarker(C[k],{radius:3,fillColor:'#3730a3',color:'#fff',weight:1,fillOpacity:0.7}).bindTooltip(disp(k)).addTo(map)}
}

try{setTimeout(function(){
  var L=window.L;if(!L)return;
  map=L.map('map').setView([46.5,2.5],6);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{attribution:'&copy;CARTO'}).addTo(map);
  routeLayer=L.layerGroup().addTo(map);
  drawStationMarkers();
},800)}catch(e){console.log('map disabled')}

// search
function search(){
  var o=document.querySelector('select[name=origin]').value;
  var d=document.querySelector('select[name=destination]').value;
  if(!o||!d)return; loading(true,'searching...');
  var p=new URLSearchParams({origin:o,destination:d,decompose:'1'});
  var dv=document.querySelector('input[name=date]').value;if(dv)p.set('date',dv);
  var da=document.querySelector('input[name=dep_after]').value;if(da)p.set('departure_after',da);
  var ab=document.querySelector('input[name=arr_before]').value;if(ab)p.set('arrival_before',ab);
  fetch('/api/search?'+p).then(function(r){return r.json()}).then(function(data){
    loading(false);render(data);drawRoute(data.origin,data.destination,data.direct_free);
  }).catch(function(){loading(false)});
}
function hunt(){
  var o=document.querySelector('select[name=origin]').value;if(!o)return;
  loading(true,'hunting...');var p=new URLSearchParams({origin:o});
  var dv=document.querySelector('input[name=date]').value;if(dv)p.set('date',dv);
  fetch('/api/broadcast?'+p).then(function(r){return r.json()}).then(function(trips){
    loading(false);renderHunt(o,trips);drawRoute(o,null,trips);
  }).catch(function(){loading(false)});
}

// draw route
function drawRoute(originName,destName,trips){
  if(!map||!routeLayer)return;routeLayer.clearLayers();
  var o=latlng(originName),d=latlng(destName);
  if(o&&d){
    routeLayer.addLayer(L.polyline([o,d],{color:'#3730a3',weight:2,dashArray:'5 8',opacity:0.8}));
    routeLayer.addLayer(L.circleMarker(o,{radius:6,fillColor:'#10b981',color:'#fff',weight:2,fillOpacity:1}));
    routeLayer.addLayer(L.circleMarker(d,{radius:6,fillColor:'#3730a3',color:'#fff',weight:2,fillOpacity:1}));
  }
  if(trips&&trips.length){trips.forEach(function(t){var dd=latlng(t.destination);if(dd)routeLayer.addLayer(L.circleMarker(dd,{radius:4,fillColor:'#3730a3',color:'#fff',weight:1,fillOpacity:0.5}))})}
  if(o&&d){var pts=[o,d];if(trips)trips.forEach(function(t){var dd=latlng(t.destination);if(dd)pts.push(dd)});map.fitBounds(L.latLngBounds(pts),{padding:[40,40],maxZoom:9})}
}

// click: detail + map + stop list
function showTrip(t){
  routeLayer.clearLayers();var L=window.L;
  var o=t.origin,d=t.destination,oc=latlng(o),dc=latlng(d);
  if(oc&&dc){
    routeLayer.addLayer(L.polyline([oc,dc],{color:'#10b981',weight:3,opacity:0.9}));
    routeLayer.addLayer(L.circleMarker(oc,{radius:6,fillColor:'#10b981',color:'#fff',weight:2,fillOpacity:1}).bindTooltip(disp(o)));
    routeLayer.addLayer(L.circleMarker(dc,{radius:6,fillColor:'#3730a3',color:'#fff',weight:2,fillOpacity:1}).bindTooltip(disp(d)));
    map.fitBounds(L.latLngBounds([oc,dc]),{padding:[40,40],maxZoom:8});
  }
  show('detail');
  var html='<div style=line-height:1.8>'+
    '<span class=\"tag tag-m\">MAX</span> '+
    'Train <b style=color:var(--hi)>'+t.train_number+'</b><br>'+
    '<span style=color:var(--dim)>'+t.departure_date+'</span><br>'+
    '<div style=margin-top:4px>'+
    '<b>'+t.departure_time+'</b> '+disp(o)+'<br>'+
    '<b>'+t.arrival_time+'</b> '+disp(d)+'<br>'+
    '<span style=color:var(--dim)>'+t.duration_min+' min | '+(t.entity||'')+'</span>'+
    '</div>'+
    '<div id=\"stopList\" style=\"margin-top:6px;color:var(--dim);font-size:10px\">loading stops...</div>'+
    '</div>';
  document.getElementById('dt').innerHTML=html;

  // fetch stop list
  var params='train='+t.train_number+'&date='+t.departure_date;
  fetch('/api/train_stops?'+params).then(function(r){return r.json()}).then(function(s){
    var stops=s.stops||[];
    var h='<div style=\"margin-top:4px;border-top:1px solid var(--border);padding-top:4px\">stops: ';
    stops.forEach(function(s,i){
      var icon=s.type==='departure'?'&rarr;':'&larr;';
      h+='<span style=color:var(--hi)>'+s.time+'</span> '+icon+' '+trunc(disp(s.station),16)+(i<stops.length-1?' | ':'');
    });
    h+='</div>';
    document.getElementById('stopList').innerHTML=h;
  }).catch(function(){document.getElementById('stopList').innerHTML='';});
}
function showComposite(c){
  routeLayer.clearLayers();var L=window.L;var pts=[];
  var html='<div style=line-height:1.8>';
  c.legs.forEach(function(l,i){
    var f=latlng(l.origin),t=latlng(l.destination);
    var cls=c.is_fully_max?'tag-m':'tag-p';
    html+='<span class=\"tag '+cls+'\">leg '+(i+1)+'</span> ';
    html+='Train <b style=color:var(--hi)>'+l.train_number+'</b><br>';
    html+='<b>'+l.departure_time+'</b> '+disp(l.origin)+'<br><b>'+l.arrival_time+'</b> '+disp(l.destination)+'<br>';
    html+='<span style=color:var(--dim)>'+l.duration_min+'min</span><br>';
    html+='<div id=leg'+i+'stops style=\"font-size:9px;color:var(--dim)\"></div>';
    if(f&&t){
      var color=i===0?'#10b981':'#3730a3';
      routeLayer.addLayer(L.polyline([f,t],{color:color,weight:2,opacity:0.8}));
      routeLayer.addLayer(L.circleMarker(f,{radius:5,fillColor:color,color:'#fff',weight:1,fillOpacity:0.8}).bindTooltip(disp(l.origin)));
      if(i===c.legs.length-1) routeLayer.addLayer(L.circleMarker(t,{radius:5,fillColor:color,color:'#fff',weight:1,fillOpacity:0.8}).bindTooltip(disp(l.destination)));
      pts.push(f);pts.push(t);
    }
  });
  html+='<span style=color:var(--dim)>total: '+c.total_duration_min+'min | '+c.max_legs+' MAX + '+c.paid_legs+' paid</span></div>';
  if(pts.length)map.fitBounds(L.latLngBounds(pts),{padding:[40,40],maxZoom:7});
  show('detail');document.getElementById('dt').innerHTML=html;

  // fetch stop lists for each leg
  c.legs.forEach(function(l,i){
    var p='train='+l.train_number+'&date='+l.departure_date;
    fetch('/api/train_stops?'+p).then(function(r){return r.json()}).then(function(s){
      var stops=s.stops||[];
      var h='stops: ';
      stops.forEach(function(st,j){
        h+=st.time+' '+(st.type==='departure'?'&rarr;':'&larr;')+' '+trunc(disp(st.station),14)+(j<stops.length-1?' | ':'');
      });
      var el=document.getElementById('leg'+i+'stops');
      if(el)el.innerHTML=h;
    }).catch(function(){});
  });
}

function priceKey(trip){
  // estimate price order from duration — longer trip = more expensive
  // this ranks by estimated price without needing exact values
  return trip.duration_min || 60;
}

// render
function render(d){
  show('sum');hide('descentBox'); // descentres merged into direct
  var freeDc=d.decompositions.filter(function(c){return c.is_fully_max});
  var paidDc=d.decompositions.filter(function(c){return !c.is_fully_max});
  var allDirect = d.direct_free.slice();
  // merge descentres into direct list with a note
  var descSeen = {}; d.direct_free.forEach(function(t){descSeen[t.trip_key||t.train_number+':'+t.departure_time]=true});
  (d.descentres||[]).forEach(function(c){
    var t = c.legs[0];
    if(!descSeen[t.trip_key||t.train_number+':'+t.departure_time]){
      t._descentre = true; // mark for display
      allDirect.push(t);
    }
  });
  document.getElementById('sc').innerHTML=
    '<span class=\"tag tag-m\">direct: '+d.direct_free.length+'</span> '+
    (d.descentres&&d.descentres.length?'<span class=\"tag tag-m\">descentres: '+d.descentres.length+'</span> ':'')+
    '<span class=\"tag tag-m\">detour: '+freeDc.length+'</span> '+
    '<span class=\"tag tag-p\">payant: '+d.direct_paid.length+'</span> '+
    '<span class=\"tag tag-p\">detour payant: '+paidDc.length+'</span>';

  // DIRECT MAX (including descentres)
  show('directBox'); document.getElementById('fc').textContent='('+allDirect.length+')';
  document.getElementById('fl').innerHTML=allDirect.length?allDirect.map(function(t){
    return trH(t,t._descentre);
  }).join(''):'<div class=e>none</div>';

  // DETOUR MAX
  if(freeDc.length){show('detourBox');document.getElementById('dtourc').textContent='('+freeDc.length+')';
    document.getElementById('dl').innerHTML=freeDc.slice(0,20).map(dcH).join('');
    if(freeDc.length>20)document.getElementById('dl').innerHTML+='<div class=e>+ '+(freeDc.length-20)+' more</div>'}
  else hide('detourBox');

  // PAYANT
  var paidSorted = d.direct_paid.slice().sort(function(a,b){return priceKey(a)-priceKey(b)});
  show('payantBox'); document.getElementById('pc').textContent='('+d.direct_paid.length+')';
  document.getElementById('pl').innerHTML=paidSorted.length?paidSorted.slice(0,10).map(trH).join('')+(paidSorted.length>10?'<div class=e>+ '+(paidSorted.length-10)+' more</div>':''):'<div class=e>none</div>';

  // DETOUR PAYANT
  if(paidDc.length){show('detourPayBox');document.getElementById('dpc').textContent='('+paidDc.length+')';
    document.getElementById('dpl').innerHTML=paidDc.slice(0,15).map(dcH).join('');
    if(paidDc.length>15)document.getElementById('dpl').innerHTML+='<div class=e>+ '+(paidDc.length-15)+' more</div>'}
  else hide('detourPayBox');
}
function renderHunt(origin,trips){
  hide('detourBox');hide('detourPayBox');hide('payantBox');hide('descentBox');
  show('sum');show('directBox');
  document.getElementById('sc').innerHTML='<span class=\"tag tag-m\">'+trips.length+' free from '+disp(origin)+'</span>';
  document.getElementById('fc').textContent='('+trips.length+')';
  var g={};trips.forEach(function(t){var d=t.destination;if(!g[d])g[d]=[];g[d].push(t)});
  var h='';for(var d in g){h+='<div style=\"font-weight:bold;color:var(--hi);margin-top:4px\">'+disp(d)+' ('+g[d].length+')</div>';h+=g[d].slice(0,4).map(trH).join('');if(g[d].length>4)h+='<div class=e>+ '+(g[d].length-4)+' more</div>'}
  document.getElementById('fl').innerHTML=h||'<div class=e>none</div>';
}

function trH(t){return '<div class=tr onclick=\"showTrip('+JSON.stringify(t).replace(/\"/g,'&quot;')+')\" title=\"click for detail\"><span class=t-time>'+t.departure_time+' &rarr; '+t.arrival_time+'</span><span class=\"tag tag-m\">'+t.train_number+'</span><span class=t-route>'+trunc(disp(t.origin),18)+' &rarr; '+trunc(disp(t.destination),18)+'</span><span class=stat-d>'+t.duration_min+'m</span></div>'}
function dcH(c){var L=c.legs.map(function(l){return trunc(disp(l.origin),9)+'('+l.departure_time+')';}).join(' &rarr; ')+' &rarr; '+trunc(disp(c.destination),9)+'('+c.arrival_time+')';var cls=c.is_fully_max?'tag-m':'tag-p',label=c.is_fully_max?(c.max_legs+'M'):(c.max_legs+'M+'+c.paid_legs+'P');return '<div class=tr onclick=\"showComposite('+JSON.stringify(c).replace(/\"/g,'&quot;')+')\" title=\"click for detail\"><span class=t-time>'+c.departure_time+' &rarr; '+c.arrival_time+'</span><span class=\"tag '+cls+'\">'+label+'</span><span class=t-route>'+L+'</span><span class=stat-d>'+c.total_duration_min+'m</span></div>'}
