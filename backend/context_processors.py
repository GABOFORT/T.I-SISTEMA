"""Pone los niveles de acceso del usuario en TODAS las plantillas.

Cada plantilla puede usar {{ niveles.consultar }}, {{ niveles.crear }},
{{ niveles.modificar }}, {{ niveles.eliminar }}, {{ niveles.exportar }}
y {{ niveles.importar }} sin que cada vista tenga que pasarlos a mano.
Los niveles se leen de la BD en cada petición (ver _niveles_sesion), así
los cambios de Configuración aplican al momento sin cerrar sesión.
"""


def niveles_usuario(request):
    from backend.views import _niveles_sesion
    return {'niveles': _niveles_sesion(request)}
