// TGV Max frontend app
var STATIONS=[],map=null,routeLayer=null;
var C={};          // API name -> [lat,lon]  (from backend)
var DISP={};       // API name -> pretty display label (from backend)
var SNCF_AUTH=null; // SNCF Connect auth status
// Row click dispatch: trip objects are stored here and rows reference them by
// index. (Inline JSON.stringify into onclick="" broke on the embedded double
// quotes and left every row's detail panel dead with "Unexpected end of input".)
var LAST_TRIPS=[],LAST_COMPS=[];
function showTripIdx(i){showTrip(LAST_TRIPS[i])}
function showCompIdx(i){showComposite(LAST_COMPS[i])}

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
  // default to today, and only show trips leaving after right now
  var now=new Date();
  document.querySelector('input[name=date]').value=localDay(now);
  document.querySelector('input[name=dep_after]').value=hhmm(now);
  // SNCF open data only spans ~31 days ahead: clamp the picker so
  // out-of-range dates fail fast client-side instead of returning nothing.
  var maxD=new Date(now);maxD.setDate(maxD.getDate()+31);
  var di=document.querySelector('input[name=date]');
  di.min=localDay(now);di.max=localDay(maxD);
  // keep the "after now" filter only while the date is still today
  document.querySelector('input[name=date]').addEventListener('change',function(){
    var n=new Date();
    document.querySelector('input[name=dep_after]').value=(this.value===localDay(n))?hhmm(n):'';
  });
  drawStationMarkers();
  checkSncfAuth();
}).catch(function(e){console.error('station load failed',e)});

function pad(n){return (n<10?'0':'')+n}
function localDay(d){return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())}
function hhmm(d){return pad(d.getHours())+':'+pad(d.getMinutes())}

// ctrl/cmd+click two map dots to set origin then destination and search
var _pickOrigin=null;
function pickStation(name){
  var os=document.querySelector('select[name=origin]'),ds=document.querySelector('select[name=destination]');
  if(_pickOrigin===null){
    _pickOrigin=name;os.value=name;
    document.getElementById('st').textContent='origin: '+disp(name)+' — ctrl+click a destination';
  }else{
    ds.value=name;_pickOrigin=null;search();
  }
}

var _markersDrawn=false;
function drawStationMarkers(){
  if(!map||_markersDrawn||!Object.keys(C).length)return;
  _markersDrawn=true;
  Object.keys(C).forEach(function(name){
    L.circleMarker(C[name],{radius:3,fillColor:'#4338ca',color:'#fff',weight:1,fillOpacity:0.8})
      .bindTooltip(disp(name)+' — ctrl+click to pick')
      .on('click',function(e){if(e.originalEvent.ctrlKey||e.originalEvent.metaKey){pickStation(name)}})
      .addTo(map);
  });
}

try{setTimeout(function(){
  var L=window.L;if(!L)return;
  map=L.map('map').setView([46.5,2.5],6);
  // OpenStreetMap tiles: CARTO's basemaps started requiring an API key and
  // now serve an "API key required" placeholder instead of map content.
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',maxZoom:19}).addTo(map);
  routeLayer=L.layerGroup().addTo(map);
  drawStationMarkers();
},800)}catch(e){console.log('map disabled')}

// SNCF Connect Authentication
function checkSncfAuth(){
  fetch('/api/auth/sncf/status').then(function(r){return r.json()}).then(function(data){
    SNCF_AUTH=data;
    renderAuthUI(data);
  }).catch(function(){renderAuthUI({sncf_connected:false,pam_user:null})});
}

function renderAuthUI(data){
  var box=document.getElementById('authBox');
  if(!box) return;
  if(data.sncf_connected){
    box.innerHTML='<h2>SNCF CONNECT <span style="color:var(--g)">●</span> <span style="color:var(--dim)">('+data.email+')</span> <button onclick="showSncfModal()">Change</button></h2>';
  }else{
    box.innerHTML='<h2>SNCF CONNECT <span style="color:var(--r)">○</span> <button onclick="showSncfModal()">Connect</button></h2>';
  }
}

function showSncfModal(){
  var html='<div style=line-height:1.8><h3 style="margin-bottom:8px">SNCF Connect</h3>';
  if(SNCF_AUTH && SNCF_AUTH.sncf_connected){
    html+='<p style="color:var(--dim);font-size:11px;margin-bottom:8px">Connected as: <b>'+SNCF_AUTH.email+'</b></p>';
    html+='<button onclick="disconnectSncf()" style="background:var(--r);color:white;border-color:var(--r)">Disconnect</button>';
  }else{
    html+='<p style="color:var(--dim);font-size:11px;margin-bottom:8px">Enter your SNCF Connect credentials to enable booking and exact prices.</p>';
    html+='<div class=row style="margin-top:4px"><input type=email name=sncf_email placeholder="Email" required style="flex:1"></div>';
    html+='<div class=row style="margin-top:4px"><input type=password name=sncf_password placeholder="Password" required style="flex:1"></div>';
    html+='<div class=row style="margin-top:8px"><button class=prim onclick="saveSncfCreds()">Save</button></div>';
  }
  html+='</div>';
  show('detail');document.getElementById('dt').innerHTML=html;
}

function saveSncfCreds(){
  var email=document.querySelector('input[name=sncf_email]').value;
  var password=document.querySelector('input[name=sncf_password]').value;
  if(!email||!password){alert('Email and password required');return;}
  loading(true,'saving...');
  fetch('/api/auth/sncf/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email,password:password})})
    .then(function(r){return r.json()}).then(function(data){
      loading(false);
      if(data.success){
        checkSncfAuth();
        hideDetail();
      }else{alert(data.error||'Failed to save');}
    }).catch(function(){loading(false);alert('Request failed')});
}

function disconnectSncf(){
  if(!confirm('Disconnect from SNCF Connect?')) return;
  loading(true,'disconnecting...');
  fetch('/api/auth/sncf/delete',{method:'POST'})
    .then(function(r){return r.json()}).then(function(data){
      loading(false);
      if(data.success){checkSncfAuth();hideDetail();}
      else{alert(data.error||'Failed to disconnect');}
    }).catch(function(){loading(false);alert('Request failed')});
}

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
  loading(true,'hunting...');var p=new URLSearchParams({direction:'from',origin:o});
  var dv=document.querySelector('input[name=date]').value;if(dv)p.set('date',dv);
  fetch('/api/broadcast?'+p).then(function(r){return r.json()}).then(function(trips){
    loading(false);renderHunt(o,trips,'from');drawRoute(o,null,trips);
  }).catch(function(){loading(false)});
}
// reverse hunt: every free trip ARRIVING at the destination (the one feature
// the removed /tgvmax/ site had that this one lacked: "Arriver dans une gare")
function huntArr(){
  var d=document.querySelector('select[name=destination]').value;if(!d)return;
  loading(true,'hunting arrivals...');var p=new URLSearchParams({direction:'to',destination:d});
  var dv=document.querySelector('input[name=date]').value;if(dv)p.set('date',dv);
  fetch('/api/broadcast?'+p).then(function(r){return r.json()}).then(function(trips){
    loading(false);renderHunt(d,trips,'to');drawRouteTo(d,trips);
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
// reverse hunt map: anchor is the arrival station, spokes are origins
function drawRouteTo(destName,trips){
  if(!map||!routeLayer)return;routeLayer.clearLayers();
  var d=latlng(destName);
  if(d)routeLayer.addLayer(L.circleMarker(d,{radius:6,fillColor:'#3730a3',color:'#fff',weight:2,fillOpacity:1}));
  var pts=d?[d]:[];
  if(trips&&trips.length){trips.forEach(function(t){
    var o=latlng(t.origin);if(!o)return;
    routeLayer.addLayer(L.polyline([o,d],{color:'#3730a3',weight:1,dashArray:'5 8',opacity:0.5}));
    routeLayer.addLayer(L.circleMarker(o,{radius:4,fillColor:'#10b981',color:'#fff',weight:1,fillOpacity:0.5}));
    pts.push(o);
  })}
  if(pts.length)map.fitBounds(L.latLngBounds(pts),{padding:[40,40],maxZoom:9});
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
    '<span class="tag tag-m">MAX</span> '+
    'Train <b style=color:var(--hi)>'+t.train_number+'</b><br>'+
    '<span style=color:var(--dim)>'+t.departure_date+'</span><br>'+
    '<div style=margin-top:4px>'+
    '<b>'+t.departure_time+'</b> '+disp(o)+'<br>'+
    '<b>'+t.arrival_time+'</b> '+disp(d)+'<br>'+
    '<span style=color:var(--dim)>'+t.duration_min+' min | '+(t.entity||'')+'</span>'+
    '</div>';
  // Add book button if SNCF connected
  if(SNCF_AUTH && SNCF_AUTH.sncf_connected && t.is_free){
    html+='<div style="margin-top:8px"><button class=prim onclick=\'bookTrip('+JSON.stringify(t).replace(/'/g,"\\'")+')\'>Book this trip</button></div>';
  }
  html+='<div id="stopList" style="margin-top:6px;color:var(--dim);font-size:10px">loading stops...</div>'+
    '</div>';
  document.getElementById('dt').innerHTML=html;

  // fetch stop list
  var params='train='+t.train_number+'&date='+t.departure_date;
  fetch('/api/train_stops?'+params).then(function(r){return r.json()}).then(function(s){
    var stops=s.stops||[];
    var h='<div style="margin-top:4px;border-top:1px solid var(--border);padding-top:4px">stops: ';
    stops.forEach(function(s,i){
      var icon=s.type==='departure'?'&rarr;':'&larr;';
      h+='<span style=color:var(--hi)>'+s.time+'</span> '+icon+' '+trunc(disp(s.station),16)+(i<stops.length-1?' | ':'');
    });
    h+='</div>';
    document.getElementById('stopList').innerHTML=h;
  }).catch(function(){document.getElementById('stopList').innerHTML='';});
}

function bookTrip(t){
  if(!confirm('Book '+t.train_number+' '+disp(t.origin)+' → '+disp(t.destination)+' on '+t.departure_date+' at '+t.departure_time+'?')) return;
  loading(true,'booking...');
  fetch('/api/booking/book',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trip:t})})
    .then(function(r){return r.json()}).then(function(data){
      loading(false);
      if(data.success){
        alert('✅ Booked! Confirmation: '+(data.confirmation||'N/A'));
        hideDetail();
      }else{
        alert('❌ Booking failed: '+(data.message||data.error||'Unknown error'));
      }
    }).catch(function(){loading(false);alert('Booking request failed')});
}

function showComposite(c){
  routeLayer.clearLayers();var L=window.L;var pts=[];
  var html='<div style=line-height:1.8>';
  c.legs.forEach(function(l,i){
    var f=latlng(l.origin),t=latlng(l.destination);
    var legtag=l.is_free?'<span class="tag tag-m">MAX</span>':'<span class="tag '+(l.carrier==='TER'?'tag-c':'tag-p')+'">'+(l.carrier||'')+'</span>';
    html+='<span style=color:var(--dim)>leg '+(i+1)+'</span> '+legtag+' ';
    html+='Train <b style=color:var(--hi)>'+l.train_number+'</b>';
    html+=l.is_free?'':' <span style=color:var(--y)>'+(l.price_display||'')+'</span>';
    html+='<br><b>'+l.departure_time+'</b> '+disp(l.origin)+'<br><b>'+l.arrival_time+'</b> '+disp(l.destination)+'<br>';
    html+='<span style=color:var(--dim)>'+l.duration_min+'min</span><br>';
    html+='<div id=leg'+i+'stops style="font-size:9px;color:var(--dim)"></div>';
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
  LAST_TRIPS=[];LAST_COMPS=[];
  var freeDc=d.decompositions.filter(function(c){return c.is_fully_max});
  var paidDc=d.decompositions.filter(function(c){return !c.is_fully_max});
  var allDirect = d.direct_free.slice();
  // merge descentres into direct list, shown as origin -> target (where you
  // get off), noting the train is booked through to a later terminus
  var descSeen = {}; d.direct_free.forEach(function(t){descSeen[t.train_number+':'+t.departure_time]=true});
  (d.descentres||[]).forEach(function(c){
    var booked = c.legs[0];
    if(descSeen[booked.train_number+':'+c.departure_time]) return;
    allDirect.push({
      train_number: booked.train_number,
      origin: c.origin,
      destination: c.destination,        // the target — where you get off
      departure_date: booked.departure_date,
      departure_time: c.departure_time,
      arrival_time: c.arrival_time,       // arrival at the target
      duration_min: c.total_duration_min,
      is_free: true,
      _descentre: true,
      booked_to: c.booked_to
    });
  });
  document.getElementById('sc').innerHTML=
    '<span class="tag tag-m">direct: '+d.direct_free.length+'</span> '+
    (d.descentres&&d.descentres.length?'<span class="tag tag-m">descentres: '+d.descentres.length+'</span> ':'')+
    '<span class="tag tag-m">detour: '+freeDc.length+'</span> '+
    '<span class="tag tag-p">payant: '+d.direct_paid.length+'</span> '+
    '<span class="tag tag-p">detour payant: '+paidDc.length+'</span>';

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
function renderHunt(anchor,trips,dir){
  dir=dir||'from';
  LAST_TRIPS=[];LAST_COMPS=[];
  var key=dir==='to'?'origin':'destination';
  var pre=dir==='to'?'free to ':'free from ';
  hide('detourBox');hide('detourPayBox');hide('payantBox');hide('descentBox');
  show('sum');show('directBox');
  document.getElementById('sc').innerHTML='<span class="tag tag-m">'+trips.length+pre+disp(anchor)+'</span>';
  document.getElementById('fc').textContent='('+trips.length+')';
  var g={};trips.forEach(function(t){var k=t[key];if(!g[k])g[k]=[];g[k].push(t)});
  var h='';for(var d in g){h+='<div style="font-weight:bold;color:var(--hi);margin-top:4px">'+disp(d)+' ('+g[d].length+')</div>';h+=g[d].slice(0,4).map(trH).join('');if(g[d].length>4)h+='<div class=e>+ '+(g[d].length-4)+' more</div>'}
  document.getElementById('fl').innerHTML=h||'<div class=e>none</div>';
}

function priceCell(disp_str,est,dur){var c=est?'var(--y)':'var(--g)';return '<span class=stat-d title="'+dur+' min" style="color:'+c+'">'+disp_str+'</span>'}
function trH(t){
  var idx=LAST_TRIPS.length;LAST_TRIPS.push(t);
  var tag=t._descentre?'<span class="tag tag-c" title="book to '+disp(t.booked_to)+', get off here">desc</span>':'<span class="tag tag-m">'+t.train_number+'</span>';
  var last;
  if(t._descentre) last='<span class=stat-d title="booked through to '+disp(t.booked_to)+'">&darr;'+trunc(disp(t.booked_to),12)+'</span>';
  else if(t.is_free) last='<span class=stat-d>'+t.duration_min+'m</span>';
  else last=priceCell(t.price_display||'?',t.price_estimated,t.duration_min);
  return '<div class=tr onclick="showTripIdx('+idx+')" title="click for detail"><span class=t-time>'+t.departure_time+' &rarr; '+t.arrival_time+'</span>'+tag+'<span class=t-route>'+trunc(disp(t.origin),18)+' &rarr; '+trunc(disp(t.destination),18)+'</span>'+last+'</div>'}
function dcH(c){var idx=LAST_COMPS.length;LAST_COMPS.push(c);var L=c.legs.map(function(l){return trunc(disp(l.origin),9)+'('+l.departure_time+')';}).join(' &rarr; ')+' &rarr; '+trunc(disp(c.destination),9)+'('+c.arrival_time+')';var cls=c.is_fully_max?'tag-m':'tag-p',label=c.is_fully_max?(c.max_legs+'M'):(c.max_legs+'M+'+c.paid_legs+'P');var last=c.is_fully_max?'<span class=stat-d>'+c.total_duration_min+'m</span>':priceCell(c.price_display||'?',c.price_estimated,c.total_duration_min);return '<div class=tr onclick="showCompIdx('+idx+')" title="click for detail"><span class=t-time>'+c.departure_time+' &rarr; '+c.arrival_time+'</span><span class="tag '+cls+'">'+label+'</span><span class=t-route>'+L+'</span>'+last+'</div>'}