(function(){
  let panelAbierto = false;
  let ultimoConteo = null;
  let audioCtx = null;
  let nivelesConocidos = null;

  function obtenerAudioCtx(){
    if(!audioCtx){
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      audioCtx = new AudioCtx();
    }
    if(audioCtx.state === 'suspended'){
      audioCtx.resume();
    }
    return audioCtx;
  }

  document.addEventListener('click', ()=>{ obtenerAudioCtx(); }, {once:true});
  document.addEventListener('keydown', ()=>{ obtenerAudioCtx(); }, {once:true});

  function sonarAlertaNotif(){
    try{
      const ctx = obtenerAudioCtx();
      [880, 1180].forEach((freq, i)=>{
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(1, ctx.currentTime + i*0.18);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i*0.18 + 0.35);
        osc.connect(gain).connect(ctx.destination);
        osc.start(ctx.currentTime + i*0.18);
        osc.stop(ctx.currentTime + i*0.18 + 0.35);
      });
    }catch(e){}
  }

  function getCsrf(){
    const m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1] : '';
  }

  function renderNotificaciones(lista){
    const cont = document.getElementById('notifList');
    if(!cont) return;
    if(!lista || lista.length === 0){
      cont.innerHTML = '<div class="notif-empty">Sin notificaciones</div>';
      return;
    }
    cont.innerHTML = lista.map(n=>`
      <div class="notif-item ${n.leida ? '' : 'no-leida'}" onclick="marcarNotifLeida('${n.id_notificacion}', this)">
        <div class="notif-item-msg">${n.mensaje}</div>
        <div class="notif-item-fecha">${n.fecha}</div>
      </div>
    `).join('');
  }

  async function cargarNotificaciones(soloConteo){
    try{
      const res = await fetch('/notificaciones/lista/');
      if(!res.ok) return;
      const data = await res.json();
      if(data.niveles){
        const firma = JSON.stringify(data.niveles);
        if(nivelesConocidos !== null && firma !== nivelesConocidos){
          location.reload();
          return;
        }
        nivelesConocidos = firma;
      }
      const badge = document.getElementById('notifBadge');
      if(!badge) return;
      if(data.no_leidas > 0){
        badge.textContent = data.no_leidas > 9 ? '9+' : data.no_leidas;
        badge.style.display = 'flex';
      } else {
        badge.style.display = 'none';
      }
      if(ultimoConteo !== null && data.no_leidas > ultimoConteo){
        sonarAlertaNotif();
      }
      ultimoConteo = data.no_leidas;
      if(!soloConteo){
        renderNotificaciones(data.notificaciones);
      }
    }catch(e){}
  }

  window.toggleNotifPanel = function(){
    panelAbierto = !panelAbierto;
    const panel = document.getElementById('notifPanel');
    if(!panel) return;
    panel.classList.toggle('open', panelAbierto);
    if(panelAbierto) cargarNotificaciones(false);
  };

  window.marcarNotifLeida = async function(id, el){
    if(el) el.classList.remove('no-leida');
    try{
      await fetch(`/notificaciones/leida/${id}/`, {method:'POST', headers:{'X-CSRFToken': getCsrf()}});
      cargarNotificaciones(true);
    }catch(e){}
  };

  window.marcarTodasLeidas = async function(){
    try{
      await fetch('/notificaciones/leidas-todas/', {method:'POST', headers:{'X-CSRFToken': getCsrf()}});
      cargarNotificaciones(false);
    }catch(e){}
  };

  document.addEventListener('click', e=>{
    const wrap = document.querySelector('.notif-bell-wrap');
    const panel = document.getElementById('notifPanel');
    if(wrap && panel && !wrap.contains(e.target)){
      panelAbierto = false;
      panel.classList.remove('open');
    }
  });

  document.addEventListener('DOMContentLoaded', ()=>{
    cargarNotificaciones(true);
    setInterval(()=>cargarNotificaciones(true), 5000);
  });
})();
