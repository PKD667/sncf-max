// TGV Max frontend app
var STATIONS={},ALIASES=[],map=null,routeLayer=null;
var C={
"PARIS (intramuros)":[48.86,2.35],"PARIS GARE DE LYON":[48.84,2.37],"PARIS MONTPARNASSE 1 ET 2":[48.84,2.32],
"PARIS NORD":[48.88,2.36],"PARIS EST":[48.88,2.36],"LYON (intramuros)":[45.76,4.84],
"LYON PART DIEU":[45.76,4.86],"MARSEILLE ST CHARLES":[43.30,5.38],"BORDEAUX ST JEAN":[44.83,-0.56],
"LILLE FLANDRES":[50.64,3.07],"LILLE EUROPE":[50.64,3.08],"STRASBOURG":[48.59,7.73],
"NANTES":[47.22,-1.54],"RENNES":[48.11,-1.67],"TOULOUSE MATABIAU":[43.61,1.45],
"MONTPELLIER ST ROCH":[43.60,3.88],"NICE VILLE":[43.70,7.26],"AVIGNON TGV":[43.92,4.79],
"AIX EN PROVENCE TGV":[43.46,5.32],"VALENCE TGV":[44.99,4.98],"DIJON VILLE":[47.32,5.04],
"GRENOBLE":[45.19,5.71],"ST PIERRE DES CORPS":[47.39,0.72],"LE MANS":[47.99,0.19],
"ANGERS ST LAUD":[47.47,-0.56],"POITIERS":[46.58,0.35],"METZ VILLE":[49.11,6.18],
"NANCY":[48.69,6.17],"CHAMPAGNE ARDENNE TGV":[49.26,4.03],
"LE CREUSOT MONTCEAU MONTCHANIN":[46.81,4.44],"MACON LOCHE":[46.30,4.82],
"CHAMBERY CHALLES LES EAUX":[45.57,5.92],"ARRAS":[50.29,2.78],"DOUAI":[50.37,3.08],
"BREST":[48.39,-4.48],"Marne-la-Vallee Chessy":[48.87,2.78],
"Lyon Saint-Exupery TGV":[45.72,5.08],"Aeroport Charles de Gaulle 2 TGV":[48.99,2.57],
"Massy TGV":[48.73,2.26],"PERPIGNAN":[42.70,2.88],"NIMES":[43.83,4.37],"PAU":[43.29,-0.37],
"BAYONNE":[43.50,-1.47],"LA ROCHELLE":[46.15,-1.15],"MULHOUSE":[47.75,7.34],"COLMAR":[48.07,7.36],
"CALAIS":[50.95,1.85],"BOULOGNE":[50.73,1.61],"ROUEN":[49.45,1.09],"LE HAVRE":[49.49,0.11],
"CAEN":[49.18,-0.35],"ST MALO":[48.65,-2.00],"VANNES":[47.66,-2.76],"LORIENT":[47.75,-3.36],
"QUIMPER":[48.00,-4.09],"ANGOULEME":[45.65,0.16],"TOURS":[47.39,0.69],
"BESANCON FRANCHE COMTE TGV":[47.31,5.95],"BELFORT MONTBELIARD TGV":[47.59,6.89],
"BLOIS":[47.59,1.33],"ORLEANS":[47.91,1.91],"BEZIERS":[43.34,3.22],"SETE":[43.41,3.70],
"NARBONNE":[43.19,3.01],"BIARRITZ":[43.46,-1.54],"DAX":[43.72,-1.07]
};

// helpers
function latlng(station){
  if(C[station]) return C[station];
  var u=station.toUpperCase();
  for(var k in C){if(u.indexOf(k.toUpperCase())!==-1||k.toUpperCase().indexOf(u)!==-1)return C[k]}
  return null;
}
function ymd(d){return d.toISOString().slice(0,10)}
function trunc(s,n){return s&&s.length>n?s.slice(0,n)+'...':(s||'')}
function show(id){document.getElementById(id).style.display='block'}
function hide(id){document.getElementById(id).style.display='none'}
function loading(on,msg){document.getElementById('st').innerHTML=on?'<span class=spin></span>'+msg:''}
function hideDetail(){hide('detail')}

// init
fetch('/api/stations').then(function(r){return r.json()}).then(function(s){
  ALIASES=Object.keys(s).sort(); STATIONS=s;
  function pop(sel,def){var e=document.querySelector('select[name='+sel+']');
    ALIASES.forEach(function(a){var o=document.createElement('option');
    o.value=a;o.text=a+' ['+trunc(s[a],20)+']';if(a===def)o.selected=true;e.appendChild(o)});}
  pop('origin','paris');pop('destination','lyon');
  document.querySelector('input[name=date]').value=ymd(new Date(Date.now()+7*86400000));
}).catch(function(e){console.error('station load failed',e)});

try{setTimeout(function(){
  var L=window.L;if(!L)return;
  map=L.map('map').setView([46.5,2.5],6);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{attribution:'&copy;CARTO'}).addTo(map);
  for(var k in C){L.circleMarker(C[k],{radius:3,fillColor:'#3730a3',color:'#fff',weight:1,fillOpacity:0.7}).bindTooltip(trunc(k,25)).addTo(map)}
  routeLayer=L.layerGroup().addTo(map);
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

// click: detail + map
function showTrip(t){
  routeLayer.clearLayers();var L=window.L;
  var o=t.origin,d=t.destination,oc=latlng(o),dc=latlng(d);
  if(oc&&dc){
    routeLayer.addLayer(L.polyline([oc,dc],{color:'#10b981',weight:3,opacity:0.9}));
    routeLayer.addLayer(L.circleMarker(oc,{radius:6,fillColor:'#10b981',color:'#fff',weight:2,fillOpacity:1}).bindTooltip(o));
    routeLayer.addLayer(L.circleMarker(dc,{radius:6,fillColor:'#3730a3',color:'#fff',weight:2,fillOpacity:1}).bindTooltip(d));
    map.fitBounds(L.latLngBounds([oc,dc]),{padding:[40,40],maxZoom:8});
  }
  show('detail');
  document.getElementById('dt').innerHTML=
    '<div style=line-height:1.8>'+
    '<span class=\"tag tag-m\">MAX</span> '+
    'Train <b style=color:var(--hi)>'+t.train_number+'</b><br>'+
    '<span style=color:var(--dim)>'+t.departure_date+'</span><br>'+
    '<b>'+t.departure_time+'</b> '+o+'<br>'+
    '<b>'+t.arrival_time+'</b> '+d+'<br>'+
    '<span style=color:var(--dim)>'+t.duration_min+' min | '+(t.entity||'')+'</span>'+
    '</div>';
}
function showComposite(c){
  routeLayer.clearLayers();var L=window.L;var pts=[];
  var html='<div style=line-height:1.8>';
  c.legs.forEach(function(l,i){
    var f=latlng(l.origin),t=latlng(l.destination);
    var cls=c.is_fully_max?'tag-m':'tag-p';
    html+='<span class=\"tag '+cls+'\">leg '+(i+1)+'</span> ';
    html+='Train <b style=color:var(--hi)>'+l.train_number+'</b><br>';
    html+='<b>'+l.departure_time+'</b> '+l.origin+'<br><b>'+l.arrival_time+'</b> '+l.destination+'<br>';
    html+='<span style=color:var(--dim)>'+l.duration_min+'min</span><br>';
    if(f&&t){
      var color=i===0?'#10b981':'#3730a3';
      routeLayer.addLayer(L.polyline([f,t],{color:color,weight:2,opacity:0.8}));
      routeLayer.addLayer(L.circleMarker(f,{radius:5,fillColor:color,color:'#fff',weight:1,fillOpacity:0.8}).bindTooltip(trunc(l.origin,25)));
      if(i===c.legs.length-1) routeLayer.addLayer(L.circleMarker(t,{radius:5,fillColor:color,color:'#fff',weight:1,fillOpacity:0.8}).bindTooltip(trunc(l.destination,25)));
      pts.push(f);pts.push(t);
    }
  });
  html+='<span style=color:var(--dim)>total: '+c.total_duration_min+'min | '+c.max_legs+' MAX + '+c.paid_legs+' paid</span></div>';
  if(pts.length)map.fitBounds(L.latLngBounds(pts),{padding:[40,40],maxZoom:7});
  show('detail');document.getElementById('dt').innerHTML=html;
}

function priceKey(trip){
  // estimate price order from duration — longer trip = more expensive
  // this ranks by estimated price without needing exact values
  return trip.duration_min || 60;
}

// render
function render(d){
  show('sum');
  var freeDc=d.decompositions.filter(function(c){return c.is_fully_max});
  var paidDc=d.decompositions.filter(function(c){return !c.is_fully_max});
  document.getElementById('sc').innerHTML=
    '<span class=\"tag tag-m\">direct: '+d.count_direct_free+'</span> '+
    '<span class=\"tag tag-m\">detour: '+freeDc.length+'</span> '+
    '<span class=\"tag tag-p\">payant: '+d.direct_paid.length+'</span> '+
    '<span class=\"tag tag-p\">detour payant: '+paidDc.length+'</span>';

  // DIRECT MAX
  show('directBox'); document.getElementById('fc').textContent='('+d.direct_free.length+')';
  document.getElementById('fl').innerHTML=d.direct_free.length?d.direct_free.map(trH).join(''):'<div class=e>none</div>';

  // DETOUR MAX
  if(freeDc.length){show('detourBox');document.getElementById('dtourc').textContent='('+freeDc.length+')';
    document.getElementById('dl').innerHTML=freeDc.slice(0,20).map(dcH).join('');
    if(freeDc.length>20)document.getElementById('dl').innerHTML+='<div class=e>+ '+(freeDc.length-20)+' more</div>'}
  else hide('detourBox');

  // PAYANT (ordered by estimated price)
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
  hide('detourBox');hide('detourPayBox');hide('payantBox');
  show('sum');show('directBox');
  document.getElementById('sc').innerHTML='<span class=\"tag tag-m\">'+trips.length+' free from '+origin+'</span>';
  document.getElementById('fc').textContent='('+trips.length+')';
  var g={};trips.forEach(function(t){var d=t.destination;if(!g[d])g[d]=[];g[d].push(t)});
  var h='';for(var d in g){h+='<div style=\"font-weight:bold;color:var(--hi);margin-top:4px\">'+d+' ('+g[d].length+')</div>';h+=g[d].slice(0,4).map(trH).join('');if(g[d].length>4)h+='<div class=e>+ '+(g[d].length-4)+' more</div>'}
  document.getElementById('fl').innerHTML=h||'<div class=e>none</div>';
}

function trH(t){return '<div class=tr onclick=\"showTrip('+JSON.stringify(t).replace(/\"/g,'&quot;')+')\" title=\"click for detail\"><span class=t-time>'+t.departure_time+' &rarr; '+t.arrival_time+'</span><span class=\"tag tag-m\">'+t.train_number+'</span><span class=t-route>'+trunc(t.origin,18)+' &rarr; '+trunc(t.destination,18)+'</span><span class=stat-d>'+t.duration_min+'m</span></div>'}
function dcH(c){var L=c.legs.map(function(l){return trunc(l.origin,9)+'('+l.departure_time+')';}).join(' &rarr; ')+' &rarr; '+trunc(c.destination,9)+'('+c.arrival_time+')';var cls=c.is_fully_max?'tag-m':'tag-p',label=c.is_fully_max?(c.max_legs+'M'):(c.max_legs+'M+'+c.paid_legs+'P');return '<div class=tr onclick=\"showComposite('+JSON.stringify(c).replace(/\"/g,'&quot;')+')\" title=\"click for detail\"><span class=t-time>'+c.departure_time+' &rarr; '+c.arrival_time+'</span><span class=\"tag '+cls+'\">'+label+'</span><span class=t-route>'+L+'</span><span class=stat-d>'+c.total_duration_min+'m</span></div>'}
