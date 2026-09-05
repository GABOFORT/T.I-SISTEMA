/* ─────────────────────────────────────────────────────────────
   T.I SISTEMA — comportamientos compartidos de interfaz
   Solo capa visual: no altera formularios, endpoints ni datos.
     · Toasts globales
     · Accesibilidad por teclado del select personalizado (.csel)
     · Cierre de modales con Escape y clic fuera
     · Estado de carga en botones de envío
     · Ordenamiento de tablas por columna
     · Menús desplegables de acciones
   ───────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  /* ── Iconos inline usados por los toasts ───────────────────── */
  var ICONOS = {
    ok:   '<svg class="icon" viewBox="0 0 24 24"><path d="M21.8 10A10 10 0 1 1 17 3.34"/><path d="m9 11 3 3L22 4"/></svg>',
    err:  '<svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/></svg>',
    warn: '<svg class="icon" viewBox="0 0 24 24"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><path d="M12 9v4M12 17h.01"/></svg>',
    info: '<svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>'
  };

  /* ═══════════════════════════════════════════════════════════
     TOASTS
     mostrarToast(mensaje, tipo)  tipo: ok | err | warn | info
     ═══════════════════════════════════════════════════════════ */
  function host() {
    var h = document.getElementById('toastHost');
    if (!h) {
      h = document.createElement('div');
      h.id = 'toastHost';
      h.setAttribute('role', 'status');
      h.setAttribute('aria-live', 'polite');
      document.body.appendChild(h);
    }
    return h;
  }

  window.mostrarToast = function (mensaje, tipo, duracion) {
    if (!mensaje) return;
    tipo = tipo || 'info';
    if (tipo === 'success') tipo = 'ok';
    if (tipo === 'error')   tipo = 'err';
    if (tipo === 'warning') tipo = 'warn';

    var el = document.createElement('div');
    el.className = 'toast ' + tipo;
    el.innerHTML = (ICONOS[tipo] || ICONOS.info) + '<span></span>';
    // Texto por textContent: nunca se interpreta HTML de origen externo
    el.querySelector('span').textContent = String(mensaje).replace(/^[^\w¿¡áéíóúÁÉÍÓÚñÑ]+/, '').trim() || mensaje;

    host().appendChild(el);
    requestAnimationFrame(function () { el.classList.add('show'); });

    var ms = duracion || 4000;
    setTimeout(function () {
      el.classList.remove('show');
      setTimeout(function () { el.remove(); }, 260);
    }, ms);
  };

  /* Convierte los banners de mensajes de Django en toasts.
     El banner se conserva en el HTML como respaldo sin JavaScript. */
  function mensajesAToast() {
    document.querySelectorAll('.notif-ok[data-toast], .notif-err[data-toast]').forEach(function (b) {
      var texto = b.textContent.trim();
      if (!texto) return;
      window.mostrarToast(texto, b.classList.contains('notif-err') ? 'err' : 'ok');
      b.remove();
    });
  }

  /* ═══════════════════════════════════════════════════════════
     SELECT PERSONALIZADO (.csel) — accesibilidad por teclado
     No cambia el marcado ni el <input hidden>: solo añade
     semántica ARIA y navegación con teclado sobre lo existente.
     ═══════════════════════════════════════════════════════════ */
  function prepararCsel(csel) {
    if (csel.dataset.a11y === '1') return;
    csel.dataset.a11y = '1';

    var trigger = csel.querySelector('.csel-trigger');
    var drop    = csel.querySelector('.csel-dropdown');
    if (!trigger || !drop) return;

    trigger.setAttribute('role', 'combobox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-haspopup', 'listbox');
    if (!trigger.hasAttribute('tabindex')) trigger.setAttribute('tabindex', '0');

    drop.setAttribute('role', 'listbox');
    drop.querySelectorAll('.csel-option').forEach(function (o) {
      o.setAttribute('role', 'option');
    });

    var teclado = '';
    var tTeclado = null;

    function opciones() {
      return Array.prototype.slice.call(drop.querySelectorAll('.csel-option'));
    }

    function marcar(op) {
      opciones().forEach(function (o) { o.classList.remove('is-active'); });
      if (!op) return;
      op.classList.add('is-active');
      var cajaOp = op.offsetTop;
      var altoOp = op.offsetHeight;
      var visIni = drop.scrollTop;
      var visFin = visIni + drop.clientHeight;
      if (cajaOp < visIni) drop.scrollTop = cajaOp;
      else if (cajaOp + altoOp > visFin) drop.scrollTop = cajaOp + altoOp - drop.clientHeight;
    }

    function activa() { return drop.querySelector('.csel-option.is-active'); }

    function abrir() {
      if (csel.classList.contains('open')) return;
      trigger.click();  // reutiliza toggleCsel() de la página
    }

    trigger.addEventListener('keydown', function (e) {
      var abierta = csel.classList.contains('open');

      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (abierta && activa()) { activa().click(); trigger.focus(); }
        else trigger.click();
        return;
      }
      if (e.key === 'Escape' && abierta) {
        e.preventDefault();
        window.cerrarCselAbiertos();
        return;
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!abierta) { abrir(); return; }
        var lista = opciones();
        var i = lista.indexOf(activa());
        i = e.key === 'ArrowDown' ? Math.min(i + 1, lista.length - 1) : Math.max(i - 1, 0);
        if (i < 0) i = 0;
        marcar(lista[i]);
        return;
      }
      if (e.key === 'Home' || e.key === 'End') {
        if (!abierta) return;
        e.preventDefault();
        var l = opciones();
        marcar(e.key === 'Home' ? l[0] : l[l.length - 1]);
        return;
      }
      if (e.key === 'Tab' && abierta) {
        window.cerrarCselAbiertos();
        return;
      }
      // Búsqueda por escritura: con 14 áreas o N equipos, teclear es más
      // rápido que recorrer la lista con el ratón.
      if (e.key.length === 1 && /\S/.test(e.key)) {
        if (!abierta) abrir();
        teclado += e.key.toLowerCase();
        clearTimeout(tTeclado);
        tTeclado = setTimeout(function () { teclado = ''; }, 700);
        var m = opciones().filter(function (o) {
          return o.textContent.trim().toLowerCase().indexOf(teclado) === 0;
        })[0];
        if (m) marcar(m);
      }
    });

    // Sincroniza aria-expanded y decide si el menú abre hacia arriba
    var obs = new MutationObserver(function () {
      var abierta = csel.classList.contains('open');
      trigger.setAttribute('aria-expanded', abierta ? 'true' : 'false');
      if (abierta) {
        var sel = drop.querySelector('.csel-option.selected');
        marcar(sel || opciones()[0]);
      } else {
        marcar(null);
      }
    });
    obs.observe(csel, { attributes: true, attributeFilter: ['class'] });
  }

  function prepararTodosLosCsel() {
    document.querySelectorAll('.csel').forEach(prepararCsel);
  }

  /* ═══════════════════════════════════════════════════════════
     API DEL SELECT PERSONALIZADO (.csel)
     Estaba duplicada en 8 plantillas con variantes que solo diferían en
     detalles (unas leen data-value y otras data-val; unas rellenan un
     input oculto y otras dos; equipos necesita un callback). Esta versión
     es el superconjunto de todas, así que las pantallas siguen llamando
     exactamente igual que antes.
     ═══════════════════════════════════════════════════════════ */

  function cselNodo(idOEl) {
    return typeof idOEl === 'string' ? document.getElementById(idOEl) : idOEl;
  }

  // Unas plantillas escriben data-value y otras data-val
  function valorOpcion(opt) {
    return opt.dataset.value !== undefined ? opt.dataset.value : (opt.dataset.val || '');
  }

  function ocultos(csel) {
    return csel.querySelectorAll('input[type=hidden]');
  }

  function pintarSeleccion(csel, opt) {
    var valEl = csel.querySelector('.csel-val');
    if (!valEl) return;
    var val = valorOpcion(opt);
    valEl.textContent = opt.textContent.trim();
    valEl.classList.toggle('csel-placeholder', !val);
    csel.querySelectorAll('.csel-option').forEach(function (o) { o.classList.remove('selected'); });
    opt.classList.add('selected');
    csel.classList.remove('error');
  }

  /* initCsel(id[, alCambiar])
     alCambiar es opcional. Ojo: varias pantallas hacen
     ['a','b'].forEach(initCsel), con lo que el 2.º argumento llega siendo
     el índice del array — de ahí la comprobación de que sea función. */
  window.initCsel = function (id, alCambiar) {
    var csel = cselNodo(id);
    if (!csel || csel.dataset.init === '1') return;
    csel.dataset.init = '1';
    var cb = typeof alCambiar === 'function' ? alCambiar : null;

    csel.querySelectorAll('.csel-option').forEach(function (opt) {
      opt.addEventListener('click', function () {
        var val = valorOpcion(opt);
        pintarSeleccion(csel, opt);
        var hs = ocultos(csel);
        if (hs[0]) hs[0].value = val;
        // Algunos selects guardan además el nombre legible en un 2.º oculto
        if (hs[1] && opt.dataset.nombre !== undefined) hs[1].value = opt.dataset.nombre || '';
        csel.classList.remove('open');
        if (cb) cb(val);
      });
    });
  };

  /* setCsel(id, valor[, idDelOculto]) — selecciona una opción por su valor */
  window.setCsel = function (id, valor, idOculto) {
    var csel = cselNodo(id);
    if (!csel) return;
    var opt = csel.querySelector('[data-value="' + valor + '"]') ||
              csel.querySelector('[data-val="' + valor + '"]');
    if (!opt) return;
    pintarSeleccion(csel, opt);
    var destino = idOculto ? document.getElementById(idOculto) : ocultos(csel)[0];
    if (destino) destino.value = valor;
    var hs = ocultos(csel);
    if (!idOculto && hs[1] && opt.dataset.nombre !== undefined) hs[1].value = opt.dataset.nombre || '';
  };

  /* resetCsel(id[, textoPlaceholder]) — vuelve al estado sin selección */
  window.resetCsel = function (id, texto) {
    var csel = cselNodo(id);
    if (!csel) return;
    var valEl = csel.querySelector('.csel-val');
    if (valEl) {
      valEl.textContent = texto || '— Selecciona —';
      valEl.classList.add('csel-placeholder');
    }
    ocultos(csel).forEach(function (i) { i.value = ''; });
    csel.querySelectorAll('.csel-option').forEach(function (o) { o.classList.remove('selected'); });
    csel.classList.remove('error', 'open');
  };

  /* toggleCsel(id) — abre uno y cierra los demás.
     El desplegable cuelga del campo (position: absolute). Eso le suma
     desbordamiento al .modal-body, que tiene overflow-y: por eso el cuerpo
     reserva siempre el hueco de la barra (scrollbar-gutter) y desplegar ya
     no estrecha los campos. Lo que si hace falta desde aqui es orientarlo y
     asomarlo cuando el campo esta pegado al borde del area con scroll. */
  var ALTO_DD = 220;

  /* Cuando el modal cabe entero, .modal-body lleva `sin-scroll` y deja de
     ser contenedor de scroll: no hay area que recorte el desplegable ni a
     la que asomarlo. Solo cuando si desplaza se tiene en cuenta su borde. */
  function areaConScroll(trigger) {
    var cont = trigger.closest('.modal-body');
    return (cont && !cont.classList.contains('sin-scroll')) ? cont : null;
  }

  /* Se orienta con el alto que el desplegable ocupa de verdad. Dando por
     hecho el tope de 220 px, una lista de tres opciones se abria hacia
     arriba en cuanto el campo no tenia 220 px libres debajo, aunque le
     sobrara sitio para los 120 que realmente necesitaba. */
  function orientarDropdown(csel) {
    var trigger = csel.querySelector('.csel-trigger');
    var dd = csel.querySelector('.csel-dropdown');
    if (!trigger || !dd) return;

    var cont = areaConScroll(trigger);
    var lim = cont ? cont.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
    var t = trigger.getBoundingClientRect();
    var abajo  = Math.min(lim.bottom, window.innerHeight) - t.bottom;
    var arriba = t.top - Math.max(lim.top, 0);
    var alto = Math.min(ALTO_DD, dd.scrollHeight + 2);

    csel.classList.toggle('drop-up', abajo < alto && arriba > abajo);
  }

  function cerrarCsel(csel) {
    if (csel) csel.classList.remove('open', 'drop-up');
  }

  window.cerrarCselAbiertos = function () {
    document.querySelectorAll('.csel.open').forEach(cerrarCsel);
  };

  function asomarDropdown(csel) {
    var trigger = csel.querySelector('.csel-trigger');
    var dd = csel.querySelector('.csel-dropdown');
    if (!trigger || !dd) return;
    var cont = areaConScroll(trigger);
    if (!cont) return;
    var r = dd.getBoundingClientRect();
    var c = cont.getBoundingClientRect();
    if (r.bottom > c.bottom) cont.scrollTop += r.bottom - c.bottom + 8;
    else if (r.top < c.top) cont.scrollTop -= c.top - r.top + 8;
  }

  window.toggleCsel = function (id) {
    var csel = cselNodo(id);
    if (!csel) return;
    var abierto = csel.classList.contains('open');
    window.cerrarCselAbiertos();
    if (abierto) return;
    // Con todo cerrado la medida es honesta: es el momento exacto para saber
    // si el modal sigue cabiendo, antes de decidir hacia donde abre.
    // (evaluarScroll se declara mas abajo, en el controlador de modales.)
    var cuerpo = csel.closest('.modal-body');
    if (cuerpo) evaluarScroll(cuerpo);

    // Visible primero, para poder medirlo; despues se decide hacia donde abre.
    csel.classList.add('open');
    orientarDropdown(csel);
    requestAnimationFrame(function () { asomarDropdown(csel); });
  };

  window.addEventListener('resize', function () { window.cerrarCselAbiertos(); });

  /* marcarCselError(id, etiqueta, lista) — marca el campo y acumula el aviso */
  window.marcarCselError = function (id, etiqueta, lista) {
    var csel = cselNodo(id);
    if (csel) csel.classList.add('error');
    if (lista && etiqueta) lista.push(etiqueta);
  };

  // Un clic fuera cierra cualquier select abierto. El desplegable abierto ya
  // no cuelga del .csel, asi que se comprueba tambien por su cuenta.
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.csel')) window.cerrarCselAbiertos();
  });

  /* ── Visor de imágenes a pantalla completa ── */
  window.abrirLb = function (src) {
    var img = document.getElementById('lbImg');
    var ov  = document.getElementById('lbOverlay');
    if (!img || !ov) return;
    img.src = src;
    ov.classList.add('open');
  };

  window.cerrarLb = function () {
    var ov = document.getElementById('lbOverlay');
    if (ov) ov.classList.remove('open');
  };

  /* ═══════════════════════════════════════════════════════════
     CONTROLADOR DE MODALES
     Un único responsable para los 19 modales del sistema. En vez de
     reescribir cada `overlay.classList.add('open')` que ya existe en las
     pantallas, se vigila esa clase con un observador por overlay
     (barato: solo el atributo class de ~3 nodos por página) y desde ahí
     se aplica todo el comportamiento común:
       · bloqueo del scroll de fondo, sin salto de layout
       · foco al primer campo útil y devolución al botón que abrió
       · trampa de foco mientras está abierto
       · Escape para cerrar
       · el clic fuera no descarta un formulario con cambios sin guardar
     ═══════════════════════════════════════════════════════════ */

  var SEL_OVERLAY = '.modal-overlay, .confirm-overlay, .lb-overlay';
  var FOCUSABLES = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]),' +
                   ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  var pila = [];          // overlays abiertos; el último es el activo
  var focoPrevio = null;  // elemento que tenía el foco antes de abrir

  function anchoBarraScroll() {
    return window.innerWidth - document.documentElement.clientWidth;
  }

  function bloquearFondo() {
    if (pila.length !== 1) return;              // ya estaba bloqueado
    var compensar = anchoBarraScroll();
    document.documentElement.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
    if (compensar > 0) document.body.style.paddingRight = compensar + 'px';
  }

  function liberarFondo() {
    if (pila.length) return;                    // aún hay otro modal abierto
    document.documentElement.style.overflow = '';
    document.body.style.overflow = '';
    document.body.style.paddingRight = '';
  }

  function caja(ov) {
    return ov.querySelector('.modal, .confirm-box') || ov;
  }

  /* Un modal que cabe entero no necesita area de scroll, y siendola recorta
     los desplegables contra su borde y reserva el hueco de una barra que
     nunca aparece. El contenido puede crecer despues de abrir (secciones que
     se revelan, filas que se agregan), asi que se revisa de nuevo cuando
     algo cambia de tamano y el area vuelve sola si hace falta. */
  function evaluarScroll(cuerpo) {
    // Un desplegable abierto falsea la medida: es el propio desbordamiento
    // que estamos evitando, no contenido real.
    if (cuerpo.querySelector('.csel.open')) return;
    cuerpo.classList.remove('sin-scroll');
    if (cuerpo.scrollHeight <= cuerpo.clientHeight + 1) cuerpo.classList.add('sin-scroll');
  }

  var vigilanteCuerpo = typeof ResizeObserver === 'function'
    ? new ResizeObserver(function (entradas) {
        entradas.forEach(function (e) {
          var cuerpo = e.target.closest('.modal-body');
          if (cuerpo) evaluarScroll(cuerpo);
        });
      })
    : null;

  function ajustarScrollCuerpo(c) {
    var cuerpo = c.querySelector('.modal-body');
    if (!cuerpo) return;
    evaluarScroll(cuerpo);
    if (cuerpo.dataset.vigilado) return;
    cuerpo.dataset.vigilado = '1';

    if (vigilanteCuerpo) {
      Array.prototype.forEach.call(cuerpo.children, function (hijo) {
        vigilanteCuerpo.observe(hijo);
      });
    }
    // Toda revelacion de campos en el sistema nace de un clic o de un cambio
    // de valor; revisar tras ellos cubre los casos que el observador no ve,
    // como un bloque que se agrega al DOM en vez de crecer.
    ['click', 'change'].forEach(function (evento) {
      cuerpo.addEventListener(evento, function () {
        setTimeout(function () { evaluarScroll(cuerpo); }, 0);
      });
    });
  }

  function enfocables(ov) {
    return Array.prototype.filter.call(
      caja(ov).querySelectorAll(FOCUSABLES),
      function (el) { return el.offsetParent !== null || el === document.activeElement; }
    );
  }

  function primerFoco(ov) {
    var c = caja(ov);
    // Un campo editable es mejor punto de partida que el botón de cerrar
    var campo = c.querySelector('input:not([type=hidden]):not([readonly]):not([disabled]),' +
                                ' textarea:not([readonly]):not([disabled]), select:not([disabled])');
    if (campo && campo.offsetParent !== null) return campo;
    var lista = enfocables(ov).filter(function (el) { return !el.classList.contains('modal-close'); });
    return lista[0] || c.querySelector('.modal-close') || c;
  }

  /* ── Cambios sin guardar ──
     Se toma una huella de los campos al abrir y se compara al intentar
     cerrar con un clic fuera. Así un descuido no borra lo capturado. */
  function huella(ov) {
    var vals = [];
    caja(ov).querySelectorAll('input, textarea, select').forEach(function (el) {
      if (el.type === 'hidden' && !el.name) return;
      vals.push(el.type === 'checkbox' || el.type === 'radio' ? (el.checked ? 1 : 0) : el.value);
    });
    return vals.join('');
  }

  function tieneCambios(ov) {
    if (ov.dataset.huella === undefined) return false;
    // Un modal de solo lectura nunca tiene cambios que perder
    if (caja(ov).classList.contains('modal-ver') ||
        caja(ov).classList.contains('modal-consultar')) return false;
    return huella(ov) !== ov.dataset.huella;
  }

  function alAbrir(ov) {
    if (pila.indexOf(ov) !== -1) return;
    if (!pila.length) focoPrevio = document.activeElement;
    pila.push(ov);
    bloquearFondo();

    var c = caja(ov);
    ov.setAttribute('aria-hidden', 'false');
    c.setAttribute('role', 'dialog');
    c.setAttribute('aria-modal', 'true');
    var titulo = c.querySelector('.modal-header h2, .confirm-title');
    if (titulo) {
      if (!titulo.id) titulo.id = 'ttl-' + (ov.id || Math.random().toString(36).slice(2, 8));
      c.setAttribute('aria-labelledby', titulo.id);
    }

    ov.dataset.huella = huella(ov);
    ajustarScrollCuerpo(caja(ov));

    // Tras la transición de entrada, para no pelear con la animación
    setTimeout(function () {
      if (pila[pila.length - 1] !== ov) return;
      var f = primerFoco(ov);
      if (f && typeof f.focus === 'function') f.focus({ preventScroll: true });
      // El canvas de firma ya tiene su tamaño definitivo: se ajusta aquí
      if (window.sincronizarFirmas) window.sincronizarFirmas(ov);
    }, 60);
  }

  function alCerrar(ov) {
    var i = pila.indexOf(ov);
    if (i === -1) return;
    pila.splice(i, 1);
    ov.setAttribute('aria-hidden', 'true');
    delete ov.dataset.huella;
    liberarFondo();
    if (!pila.length && focoPrevio && document.contains(focoPrevio)) {
      focoPrevio.focus({ preventScroll: true });
      focoPrevio = null;
    }
  }

  function vigilar(ov) {
    if (ov.dataset.ctrl === '1') return;
    ov.dataset.ctrl = '1';
    ov.setAttribute('aria-hidden', ov.classList.contains('open') ? 'false' : 'true');
    new MutationObserver(function () {
      if (ov.classList.contains('open')) alAbrir(ov);
      else alCerrar(ov);
    }).observe(ov, { attributes: true, attributeFilter: ['class'] });
    if (ov.classList.contains('open')) alAbrir(ov);
  }

  function vigilarTodos() {
    document.querySelectorAll(SEL_OVERLAY).forEach(vigilar);
  }

  /* Clic fuera: se intercepta en captura sobre el documento, así llega
     antes que el onclick que cada pantalla tiene puesto en el overlay. */
  document.addEventListener('click', function (e) {
    var ov = e.target;
    if (!ov || !ov.matches || !ov.matches(SEL_OVERLAY)) return;
    if (!ov.classList.contains('open')) return;

    if (ov.classList.contains('confirm-overlay')) {
      e.stopPropagation();                       // una confirmación se responde, no se descarta
      return;
    }
    if (tieneCambios(ov)) {
      e.stopPropagation();
      window.mostrarToast('Tienes cambios sin guardar. Usa Cancelar o la X para salir.', 'warn');
    }
  }, true);

  function cerrarActivo() {
    var ov = pila[pila.length - 1];
    if (!ov) return false;
    // Se prefiere el control de cierre de la pantalla: puede limpiar estado
    var cierra = caja(ov).querySelector('.modal-close, .btn-cancel');
    if (cierra) cierra.click();
    else ov.classList.remove('open');
    return true;
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var drop = document.querySelector('.dropdown.open');
      if (drop) { drop.classList.remove('open'); return; }
      var csel = document.querySelector('.csel.open');
      if (csel) { window.cerrarCselAbiertos(); return; }
      if (cerrarActivo()) e.preventDefault();
      return;
    }

    // Trampa de foco: el tabulador no debe salirse del modal abierto
    if (e.key !== 'Tab' || !pila.length) return;
    var ov = pila[pila.length - 1];
    var lista = enfocables(ov);
    if (!lista.length) return;
    var primero = lista[0], ultimo = lista[lista.length - 1];
    if (e.shiftKey && document.activeElement === primero) {
      e.preventDefault(); ultimo.focus();
    } else if (!e.shiftKey && document.activeElement === ultimo) {
      e.preventDefault(); primero.focus();
    } else if (!caja(ov).contains(document.activeElement)) {
      e.preventDefault(); primero.focus();
    }
  });

  /* ── Estado de carga dentro del modal ──
     El modal aparece de inmediato con un esqueleto: el clic siempre tiene
     respuesta visible mientras llegan los datos. */
  window.modalCargando = function (ov, activo) {
    if (typeof ov === 'string') ov = document.getElementById(ov);
    if (!ov) return;
    var cuerpo = ov.querySelector('.modal-body');
    if (!cuerpo) return;
    if (activo !== false) {
      cuerpo.classList.add('is-cargando');
      if (!cuerpo.querySelector('.modal-skeleton')) {
        var sk = document.createElement('div');
        sk.className = 'modal-skeleton';
        sk.setAttribute('aria-hidden', 'true');
        sk.innerHTML = '<span class="skeleton" style="width:38%"></span>' +
                       '<span class="skeleton skeleton-alto"></span>' +
                       '<span class="skeleton" style="width:52%"></span>' +
                       '<span class="skeleton skeleton-alto"></span>' +
                       '<span class="skeleton" style="width:44%"></span>';
        cuerpo.appendChild(sk);
      }
    } else {
      cuerpo.classList.remove('is-cargando');
      var v = cuerpo.querySelector('.modal-skeleton');
      if (v) v.remove();
    }
  };

  /* Abre el overlay al instante con esqueleto y lo llena cuando llegan los
     datos. Si falla, cierra y avisa: nunca deja la pantalla en suspenso. */
  window.abrirModalConDatos = function (ov, promesa, alLlegar, alFallar) {
    if (typeof ov === 'string') ov = document.getElementById(ov);
    if (!ov) return;
    ov.classList.add('open');
    window.modalCargando(ov, true);
    return Promise.resolve(promesa).then(function (datos) {
      window.modalCargando(ov, false);
      if (typeof alLlegar === 'function') alLlegar(datos);
      ov.dataset.huella = huella(ov);            // la huella se toma ya con datos
      var f = primerFoco(ov);
      if (f && f.focus) f.focus({ preventScroll: true });
      return datos;
    }).catch(function (err) {
      window.modalCargando(ov, false);
      ov.classList.remove('open');
      if (typeof alFallar === 'function') alFallar(err);
      else window.mostrarToast('No se pudo cargar la información. Inténtalo de nuevo.', 'err');
      return null;
    });
  };

  /* El sondeo automático consulta esto para no recargar la página ni
     repintar la tabla mientras el usuario está capturando algo. */
  window.hayModalAbierto = function () { return pila.length > 0; };

  var SONDEO_MIN = 5000;
  var SONDEO_MAX = 30000;

  /* ═══════════════════════════════════════════════════════════
     FIRMAS: el canvas y su caja deben medir lo mismo
     Un <canvas> no se redimensiona solo: su resolución interna (width/height)
     es independiente del tamaño que le da el CSS. Si la caja cambia de ancho
     —al abrir el modal, al girar el teléfono, al cambiar de ventana— el
     trazo se dibuja desplazado respecto al puntero.
     Aquí se sincronizan las dos medidas, conservando lo ya dibujado.
     ═══════════════════════════════════════════════════════════ */
  function sincronizarFirma(canvas) {
    var caja = canvas.getBoundingClientRect();
    var w = Math.round(caja.width), h = Math.round(caja.height);
    if (!w || !h) return;
    if (canvas.width === w && canvas.height === h) return;
    var previo = null;
    if (canvas.width && canvas.height) {
      try { previo = document.createElement('canvas');
            previo.width = canvas.width; previo.height = canvas.height;
            previo.getContext('2d').drawImage(canvas, 0, 0);
      } catch (e) { previo = null; }
    }
    canvas.width = w; canvas.height = h;
    var ctx = canvas.getContext('2d');
    ctx.lineWidth = 2; ctx.lineCap = 'round'; ctx.strokeStyle = '#111';
    if (previo) { try { ctx.drawImage(previo, 0, 0, w, h); } catch (e) {} }
  }

  window.sincronizarFirmas = function (raiz) {
    (raiz || document).querySelectorAll('.firma-wrap canvas').forEach(sincronizarFirma);
  };

  var tResize = null;
  window.addEventListener('resize', function () {
    if (!pila.length) return;                 // solo importa con un modal abierto
    clearTimeout(tResize);
    tResize = setTimeout(function () {
      window.sincronizarFirmas(pila[pila.length - 1]);
    }, 120);
  });

  window.crearSondeo = function (tarea, opciones) {
    opciones = opciones || {};
    var min = opciones.min || SONDEO_MIN;
    var max = opciones.max || SONDEO_MAX;
    var espera = min;
    var timer = null;
    var corriendo = false;

    function programar() {
      clearTimeout(timer);
      timer = setTimeout(ejecutar, espera);
    }

    function ejecutar() {
      if (corriendo) { programar(); return; }
      if (document.hidden) { programar(); return; }
      if (window.hayModalAbierto && window.hayModalAbierto()) { programar(); return; }

      corriendo = true;
      Promise.resolve()
        .then(function () { return tarea(); })
        .then(function (huboCambio) {
          espera = huboCambio ? min : Math.min(Math.round(espera * 1.6), max);
        })
        .catch(function () {
          espera = Math.min(Math.round(espera * 2), max);
        })
        .then(function () {
          corriendo = false;
          programar();
        });
    }

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) return;
      espera = min;
      programar();
    });

    programar();

    return {
      reiniciar: function () { espera = min; programar(); },
      detener: function () { clearTimeout(timer); }
    };
  };

  /* ═══════════════════════════════════════════════════════════
     BOTONES CON ESTADO DE CARGA
     Al enviar un formulario el botón se bloquea y muestra spinner,
     lo que además evita el doble envío.
     ═══════════════════════════════════════════════════════════ */
  function cargando(btn, activo) {
    if (!btn) return;
    btn.classList.toggle('is-loading', activo !== false);
    if (activo === false) btn.removeAttribute('aria-busy');
    else btn.setAttribute('aria-busy', 'true');
  }
  window.btnCargando = cargando;

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute('data-sin-loading')) return;
    var btn = form.querySelector('button[type="submit"], input[type="submit"]');
    if (btn) setTimeout(function () { cargando(btn, true); }, 0);
  }, true);

  /* ═══════════════════════════════════════════════════════════
     ORDENAMIENTO DE TABLAS
     Se activa en cualquier <th data-sort="texto|num|fecha">.
     Ordena solo las filas ya renderizadas: no toca la consulta.
     ═══════════════════════════════════════════════════════════ */
  function valorCelda(fila, idx, tipo) {
    var td = fila.children[idx];
    if (!td) return '';
    var txt = (td.getAttribute('data-sort-val') || td.textContent || '').trim();
    if (tipo === 'num') return parseFloat(txt.replace(/[^\d.-]/g, '')) || 0;
    if (tipo === 'fecha') {
      var m = txt.match(/(\d{2})\/(\d{2})\/(\d{4})(?:\s+(\d{2}):(\d{2}))?/);
      if (m) return new Date(+m[3], +m[2] - 1, +m[1], +(m[4] || 0), +(m[5] || 0)).getTime();
      return 0;
    }
    return txt.toLowerCase();
  }

  function ordenarTabla(th) {
    var tabla = th.closest('table');
    var tbody = tabla && tabla.tBodies[0];
    if (!tbody) return;

    var idx  = Array.prototype.indexOf.call(th.parentNode.children, th);
    var tipo = th.getAttribute('data-sort') || 'texto';
    var asc  = !th.classList.contains('sort-asc');

    th.parentNode.querySelectorAll('th').forEach(function (o) {
      o.classList.remove('sort-asc', 'sort-desc');
      o.removeAttribute('aria-sort');
    });
    th.classList.add(asc ? 'sort-asc' : 'sort-desc');
    th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');

    var filas = Array.prototype.slice.call(tbody.rows)
      .filter(function (f) { return !f.querySelector('.empty-state'); });

    filas.sort(function (a, b) {
      var va = valorCelda(a, idx, tipo), vb = valorCelda(b, idx, tipo);
      if (va < vb) return asc ? -1 : 1;
      if (va > vb) return asc ? 1 : -1;
      return 0;
    });
    filas.forEach(function (f) { tbody.appendChild(f); });
  }

  function prepararOrdenamiento() {
    document.querySelectorAll('th[data-sort]').forEach(function (th) {
      if (th.dataset.sortReady === '1') return;
      th.dataset.sortReady = '1';
      th.setAttribute('tabindex', '0');
      th.setAttribute('role', 'columnheader');
      th.addEventListener('click', function () { ordenarTabla(th); });
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ordenarTabla(th); }
      });
    });
  }

  /* ═══════════════════════════════════════════════════════════
     MENÚS DESPLEGABLES DE ACCIONES (⋮)
     ═══════════════════════════════════════════════════════════ */
  document.addEventListener('click', function (e) {
    var disparador = e.target.closest('[data-dropdown]');
    var abiertos = document.querySelectorAll('.dropdown.open');

    if (disparador) {
      var dd = disparador.closest('.dropdown');
      abiertos.forEach(function (d) { if (d !== dd) d.classList.remove('open'); });
      if (dd) {
        var ahora = dd.classList.toggle('open');
        disparador.setAttribute('aria-expanded', ahora ? 'true' : 'false');
      }
      return;
    }
    abiertos.forEach(function (d) {
      d.classList.remove('open');
      var t = d.querySelector('[data-dropdown]');
      if (t) t.setAttribute('aria-expanded', 'false');
    });
  });

  /* ═══════════════════════════════════════════════════════════
     PESTAÑAS
     ═══════════════════════════════════════════════════════════ */
  function prepararTabs() {
    document.querySelectorAll('.tabs').forEach(function (grupo) {
      if (grupo.dataset.ready === '1') return;
      grupo.dataset.ready = '1';
      grupo.setAttribute('role', 'tablist');

      var tabs = grupo.querySelectorAll('.tab');
      tabs.forEach(function (tab) {
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', tab.classList.contains('active') ? 'true' : 'false');

        tab.addEventListener('click', function () {
          var destino = tab.getAttribute('data-tab');
          tabs.forEach(function (t) {
            var act = t === tab;
            t.classList.toggle('active', act);
            t.setAttribute('aria-selected', act ? 'true' : 'false');
          });
          document.querySelectorAll('.tab-panel').forEach(function (p) {
            p.classList.toggle('active', p.id === destino);
          });
          try { localStorage.setItem('tab:' + location.pathname, destino); } catch (e) {}
        });
      });

      // Restaura la última pestaña vista en esta pantalla
      var guardada;
      try { guardada = localStorage.getItem('tab:' + location.pathname); } catch (e) {}
      if (guardada) {
        var t = grupo.querySelector('.tab[data-tab="' + guardada + '"]');
        if (t) t.click();
      }
    });
  }

  /* ═══════════════════════════════════════════════════════════
     INICIO
     ═══════════════════════════════════════════════════════════ */
  function init() {
    prepararTodosLosCsel();
    prepararOrdenamiento();
    prepararTabs();
    mensajesAToast();

    vigilarTodos();

    // Solo se vigilan los contenedores que de verdad se repintan por fetch.
    // Antes se observaba todo el <body> con subtree: cada sondeo de 5 s
    // disparaba un barrido completo del documento.
    var zonas = document.querySelectorAll('tbody[id], .tab-panel, #calGrid');
    if (zonas.length) {
      var obs = new MutationObserver(function () {
        prepararTodosLosCsel();
        prepararOrdenamiento();
      });
      zonas.forEach(function (z) { obs.observe(z, { childList: true }); });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
