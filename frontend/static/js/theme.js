/* ─────────────────────────────────────────────────────────────
   Tema claro / oscuro
   Se carga en el <head> de forma bloqueante para que el atributo
   data-theme quede aplicado ANTES del primer pintado. Sin esto la
   página se dibuja en oscuro y salta a claro (destello / FOUC).
   Contrato heredado: localStorage 'tema' = 'claro' | 'oscuro'.
   ───────────────────────────────────────────────────────────── */
(function () {
  var tema;
  try {
    tema = localStorage.getItem('tema');
  } catch (e) {
    tema = null;
  }

  // Sin preferencia guardada, se respeta la del sistema operativo.
  if (!tema) {
    var prefiereClaro = window.matchMedia &&
                        window.matchMedia('(prefers-color-scheme: light)').matches;
    tema = prefiereClaro ? 'claro' : 'oscuro';
  }

  aplicar(tema);

  function aplicar(t) {
    if (t === 'claro') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
  }

  window.toggleTema = function () {
    var actual = document.documentElement.getAttribute('data-theme') === 'light' ? 'claro' : 'oscuro';
    var nuevo  = actual === 'claro' ? 'oscuro' : 'claro';
    aplicar(nuevo);
    try { localStorage.setItem('tema', nuevo); } catch (e) {}

    var btn = document.getElementById('themeToggleBtn');
    if (btn) {
      btn.setAttribute('aria-label', nuevo === 'claro' ? 'Cambiar a modo oscuro' : 'Cambiar a modo claro');
      btn.setAttribute('title', nuevo === 'claro' ? 'Cambiar a modo oscuro' : 'Cambiar a modo claro');
    }
  };
})();
