/* ─────────────────────────────────────────────────────────────
   Navegación lateral
   Dos comportamientos según el ancho de pantalla:
     ≥1024px  colapsar a iconos (se recuerda en localStorage)
     <1024px  off-canvas con overlay (no se recuerda: cada visita
              arranca con el menú cerrado para no tapar contenido)
   Se conserva window.toggleSidebar() que ya usan las plantillas.
   ───────────────────────────────────────────────────────────── */
(function () {
  var ESCRITORIO = window.matchMedia('(min-width: 1024px)');

  function layout() { return document.querySelector('.app-layout'); }

  function esEscritorio() { return ESCRITORIO.matches; }

  function guardarColapso(valor) {
    try { localStorage.setItem('sidebarColapsado', valor ? '1' : '0'); } catch (e) {}
  }

  function leerColapso() {
    try { return localStorage.getItem('sidebarColapsado') === '1'; } catch (e) { return false; }
  }

  function cerrarMovil() {
    var el = layout();
    if (!el) return;
    el.classList.remove('sidebar-open');
    document.body.style.overflow = '';
    var btn = document.querySelector('.sidebar-toggle-btn');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  function abrirMovil() {
    var el = layout();
    if (!el) return;
    el.classList.add('sidebar-open');
    document.body.style.overflow = 'hidden';
    var btn = document.querySelector('.sidebar-toggle-btn');
    if (btn) btn.setAttribute('aria-expanded', 'true');
    // El foco entra al menú para que el teclado no quede detrás del overlay
    var primero = document.querySelector('.sidebar-nav .nav-item');
    if (primero) primero.focus({ preventScroll: true });
  }

  window.toggleSidebar = function () {
    var el = layout();
    if (!el) return;

    if (esEscritorio()) {
      el.classList.toggle('sidebar-collapsed');
      guardarColapso(el.classList.contains('sidebar-collapsed'));
    } else {
      if (el.classList.contains('sidebar-open')) cerrarMovil();
      else abrirMovil();
    }
  };

  function init() {
    var el = layout();
    if (!el) return;

    if (esEscritorio() && leerColapso()) el.classList.add('sidebar-collapsed');

    // Cerrar tocando el overlay
    var overlay = el.querySelector('.sidebar-overlay');
    if (overlay) overlay.addEventListener('click', cerrarMovil);

    // Cerrar con Escape
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && el.classList.contains('sidebar-open')) cerrarMovil();
    });

    // Cerrar al navegar (en móvil el menú tapa el contenido)
    el.querySelectorAll('.sidebar-nav .nav-item').forEach(function (a) {
      a.addEventListener('click', function () {
        if (!esEscritorio()) cerrarMovil();
      });
    });

    // Al pasar de móvil a escritorio, el estado off-canvas deja de aplicar
    var onCambio = function () {
      if (esEscritorio()) {
        cerrarMovil();
        if (leerColapso()) el.classList.add('sidebar-collapsed');
      } else {
        el.classList.remove('sidebar-collapsed');
      }
    };
    if (ESCRITORIO.addEventListener) ESCRITORIO.addEventListener('change', onCambio);
    else if (ESCRITORIO.addListener) ESCRITORIO.addListener(onCambio);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
