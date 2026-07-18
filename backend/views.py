from functools import wraps
from datetime import datetime, date as date_type
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.db import connection

def login_requerido(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.session.get('usuario_id'):
            return redirect('login')
        return view_func(request, *args, **kwargs)
    return wrapper

def rol_requerido(*roles_permitidos):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.session.get('usuario_rol_id') not in roles_permitidos:
                messages.error(request, 'No tienes permiso para acceder a esta sección.')
                return redirect('tickets_lista')
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator

def _ticket_es_propio(request, id_ticket):
    with connection.cursor() as cursor:
        cursor.execute("SELECT fk_usuario FROM ticket_usuario WHERE id_ticket = %s", [id_ticket])
        row = cursor.fetchone()
    return row is not None and row[0] == request.session.get('usuario_id')

def _niveles_sesion(request):
    """Niveles de acceso del usuario, leídos de la BD en cada petición para
    que los cambios hechos en Configuración apliquen al momento, sin tener
    que cerrar sesión. Se consulta una sola vez por petición (se memoriza
    en el request); si la cadena rol-permiso-nivel está rota o inactiva,
    por seguridad queda en solo consultar."""
    memoria = getattr(request, '_niveles_cache', None)
    if memoria is not None:
        return memoria
    niveles = {'consultar': 1, 'crear': 0, 'modificar': 0,
               'eliminar': 0, 'exportar': 0, 'importar': 0}
    rol_id = request.session.get('usuario_rol_id')
    if rol_id:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT n.consultar, n.crear, n.modificar,
                       n.eliminar, n.exportar, n.importar
                FROM cat_roles r
                JOIN cat_permisos p ON p.id_permiso = r.fk_permisos AND p.estatus = 'ACT'
                JOIN cat_nivel n ON n.id_nivel = p.id_nivel AND n.estatus = 'ACT'
                WHERE r.id_rol = %s AND r.estatus = 'ACT'
            """, [rol_id])
            row = cursor.fetchone()
        if row:
            claves = ('consultar', 'crear', 'modificar', 'eliminar', 'exportar', 'importar')
            niveles = {c: int(v or 0) for c, v in zip(claves, row)}
    request._niveles_cache = niveles
    return niveles

def nivel_requerido(nivel):
    """Candado del servidor: la acción solo corre si el nivel lo permite."""
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if _niveles_sesion(request).get(nivel):
                return view_func(request, *args, **kwargs)
            if 'text/html' in request.headers.get('Accept', ''):
                messages.error(request, 'Sin nivel de acceso.')
                return redirect(request.META.get('HTTP_REFERER') or 'dashboard')
            return JsonResponse({'error': 'Sin nivel de acceso.'}, status=403)
        return wrapper
    return decorator

def _crear_notificacion(cursor, fk_usuario, fk_ticket, tipo, mensaje):
    cursor.execute("""
        INSERT INTO notificaciones (fk_usuario, fk_ticket, tipo, mensaje, leida, fecha_creacion)
        VALUES (%s, %s, %s, %s, FALSE, %s)
    """, [fk_usuario, fk_ticket, tipo, mensaje, datetime.now()])

def login_view(request):
    if request.session.get('usuario_id'):
        return redirect('dashboard')
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT u.id_usuario, u.nombre, u.apellido_paterno, u.apellido_materno,
                       u.fk_rol, u.estatus, r.descripcion, u.area
                FROM usuarios u
                LEFT JOIN cat_roles r ON u.fk_rol = r.id_rol
                WHERE u.username = %s AND u.password = %s
            """, [username, password])
            row = cursor.fetchone()
        if row:
            if row[5] == 'INA':
                messages.error(request, 'Usuario deshabilitado. Contacta al administrador.')
            else:
                request.session['usuario_id']       = row[0]
                request.session['usuario_nombre']   = row[1]
                request.session['usuario_paterno']  = row[2]
                request.session['usuario_materno']  = row[3]
                request.session['usuario_rol_id']   = row[4]
                request.session['usuario_rol']      = row[6] or 'Usuario'
                request.session['usuario_username'] = username
                request.session['usuario_area']     = row[7] or ''
                return redirect('dashboard')
        else:
            messages.error(request, 'Usuario o contraseña incorrectos.')
    return render(request, 'login.html')

@login_requerido
def dashboard_view(request):
    context = {
        'username':  request.session.get('usuario_username', ''),
        'nombre':    request.session.get('usuario_nombre', ''),
        'apellidoPaterno':  request.session.get('usuario_paterno', ''),
        'apellidoMaterno':  request.session.get('usuario_materno', ''),
        'rol':       request.session.get('usuario_rol', 'Usuario'),
        'rol_id':    request.session.get('usuario_rol_id', ''),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'dashboard.html', context)

def logout_view(request):
    request.session.flush()
    return redirect('login')

def _tickets_y_stats(request):
    if not _niveles_sesion(request).get('consultar'):
        return [], {'total': 0, 'pendiente': 0, 'proceso': 0, 'finalizado': 0}
    usuario_id = request.session.get('usuario_id')
    filtro_where = "WHERE t.fk_usuario = %s"
    filtro_params = [usuario_id]
    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT t.id_ticket, t.titulo, t.area, t.dirigido_personal,
                   t.estatus, t.fecha_creacion, t.fk_equipo, t.fk_nombre_equipo,
                   t.estatus_usuario, t.descripcion,
                   u.nombre, u.apellido_paterno, u.apellido_materno
            FROM ticket_usuario t
            LEFT JOIN usuarios u ON u.id_usuario = t.fk_usuario
            {filtro_where}
            ORDER BY CASE t.estatus WHEN 'PEN' THEN 1 WHEN 'PRO' THEN 2 WHEN 'FIN' THEN 3 ELSE 4 END,
                     t.fecha_creacion DESC
        """, filtro_params)
        cols = ['id_ticket','titulo','area','dirigido_personal',
                'estatus','fecha_creacion','fk_equipo','fk_nombre_equipo','estatus_usuario','descripcion',
                'creador_nombre','creador_apellido','creador_materno']
        tickets = [dict(zip(cols, r)) for r in cursor.fetchall()]
        cursor.execute(f"""
            SELECT COUNT(*),
                   SUM(CASE WHEN t.estatus='PEN' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN t.estatus='PRO' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN t.estatus='FIN' THEN 1 ELSE 0 END)
            FROM ticket_usuario t
            LEFT JOIN usuarios u ON u.id_usuario = t.fk_usuario
            {filtro_where}
        """, filtro_params)
        s = cursor.fetchone()
        stats = {'total': s[0] or 0, 'pendiente': s[1] or 0, 'proceso': s[2] or 0, 'finalizado': s[3] or 0}
    return tickets, stats

@login_requerido
def tickets_lista(request):
    tickets, stats = _tickets_y_stats(request)
    with connection.cursor() as cursor:
        cursor.execute("SELECT id_equipo, nombre_equipo FROM cat_equipos ORDER BY nombre_equipo")
        equipos = [{'id_equipo': r[0], 'nombre_equipo': r[1]} for r in cursor.fetchall()]
    ctx = {
        'tickets': tickets,
        'equipos': equipos,
        'stats':   stats,
        'username': request.session.get('usuario_username', ''),
        'nombre':   request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'rol':      request.session.get('usuario_rol', 'Usuario'),
        'rol_id':   request.session.get('usuario_rol_id'),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'tickets/lista.html', ctx)

@login_requerido
@nivel_requerido('consultar')
def tickets_actualizacion(request):
    from django.template.loader import render_to_string
    tickets, stats = _tickets_y_stats(request)
    tbody_html = render_to_string('tickets/_filas.html', {'tickets': tickets}, request=request)
    return JsonResponse({'tbody_html': tbody_html, 'stats': stats})

@login_requerido
@nivel_requerido('crear')
def ticket_guardar(request):
    if request.method != 'POST':
        return redirect('tickets_lista')
    titulo            = request.POST.get('titulo', '').strip()
    area              = request.POST.get('id_area', '').strip()
    dirigido_personal = request.POST.get('id_dirigido', '').strip()
    fk_equipo         = request.POST.get('fk_equipo', '').strip() or None
    fk_nombre_equipo  = request.POST.get('fk_nombre_equipo', '').strip() or None
    descripcion       = request.POST.get('descripcion', '').strip()
    observacion       = request.POST.get('observaciones', '').strip() or None
    fk_usuario        = request.session.get('usuario_id')
    if not titulo or not area or not dirigido_personal or not fk_equipo or not descripcion:
        messages.error(request, 'Faltan campos obligatorios.')
        return redirect('tickets_lista')
    fotos = []
    for i in range(1, 5):
        f = request.FILES.get(f'foto{i}')
        fotos.append(f.read() if f else None)
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO ticket_usuario
              (titulo, area, dirigido_personal, descripcion, observacion_opcional,
               estatus_usuario, fk_usuario, fecha_creacion,
               fk_equipo, fk_nombre_equipo, estatus,
               foto1, foto2, foto3, foto4)
            VALUES (%s,%s,%s,%s,%s, FALSE,%s,%s, %s,%s,'PEN', %s,%s,%s,%s)
        """, [
            titulo, area, dirigido_personal, descripcion, observacion,
            fk_usuario, datetime.now(),
            fk_equipo, fk_nombre_equipo,
            fotos[0], fotos[1], fotos[2], fotos[3],
        ])
    messages.success(request, f'Ticket "{titulo}" creado correctamente.')
    return redirect('tickets_lista')

@login_requerido
@nivel_requerido('consultar')
def ticket_datos(request, id_ticket):
    if not _ticket_es_propio(request, id_ticket):
        return JsonResponse({'error': 'No encontrado'}, status=404)
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id_ticket, titulo, area, dirigido_personal, descripcion,
                   observacion_opcional, estatus, fk_equipo, fk_nombre_equipo,
                   estatus_usuario,
                   foto1 IS NOT NULL, foto2 IS NOT NULL, foto3 IS NOT NULL, foto4 IS NOT NULL
            FROM ticket_usuario WHERE id_ticket = %s
        """, [id_ticket])
        row = cursor.fetchone()
    if not row:
        return JsonResponse({'error': 'No encontrado'}, status=404)
    return JsonResponse({
        'id_ticket':         str(row[0]),
        'titulo':            row[1] or '',
        'area':              row[2] or '',
        'dirigido_personal': row[3] or '',
        'descripcion':       row[4] or '',
        'observacion':       row[5] or '',
        'estatus':           row[6] or '',
        'fk_equipo':         row[7] or '',
        'fk_nombre_equipo':  row[8] or '',
        'estatus_usuario':   row[9],
        'tiene_foto1':       bool(row[10]),
        'tiene_foto2':       bool(row[11]),
        'tiene_foto3':       bool(row[12]),
        'tiene_foto4':       bool(row[13]),
    })

@login_requerido
@nivel_requerido('consultar')
def ticket_foto(request, id_ticket, n):
    from django.http import HttpResponse, Http404
    col = f'foto{n}'
    if col not in ('foto1', 'foto2', 'foto3', 'foto4'):
        raise Http404
    if not _ticket_es_propio(request, id_ticket):
        raise Http404
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {col} FROM ticket_usuario WHERE id_ticket = %s", [id_ticket])
        row = cursor.fetchone()
    if not row or row[0] is None:
        raise Http404
    return HttpResponse(bytes(row[0]), content_type='image/jpeg')

@login_requerido
@nivel_requerido('modificar')
def ticket_actualizar(request, id_ticket):
    if request.method != 'POST':
        return redirect('tickets_lista')
    if not _ticket_es_propio(request, id_ticket):
        messages.error(request, 'No puedes modificar un ticket que no es tuyo.')
        return redirect('tickets_lista')
    titulo      = request.POST.get('titulo', '').strip()
    area        = request.POST.get('id_area', '').strip()
    dirigido    = request.POST.get('id_dirigido', '').strip()
    fk_equipo   = request.POST.get('fk_equipo', '').strip() or None
    fk_nombre   = request.POST.get('fk_nombre_equipo', '').strip() or None
    descripcion = request.POST.get('descripcion', '').strip()
    observacion = request.POST.get('observaciones', '').strip() or None
    foto_sets, foto_vals = [], []
    for i in range(1, 5):
        f = request.FILES.get(f'foto{i}')
        if f:
            foto_sets.append(f'foto{i}=%s')
            foto_vals.append(f.read())
    extra = (', ' + ', '.join(foto_sets)) if foto_sets else ''
    with connection.cursor() as cursor:
        cursor.execute(f"""
            UPDATE ticket_usuario
            SET titulo=%s, area=%s, dirigido_personal=%s,
                descripcion=%s, observacion_opcional=%s,
                fk_equipo=%s, fk_nombre_equipo=%s{extra}
            WHERE id_ticket=%s AND estatus_usuario IS NOT TRUE
        """, [titulo, area, dirigido, descripcion, observacion,
               fk_equipo, fk_nombre] + foto_vals + [id_ticket])
        if cursor.rowcount == 0:
            messages.error(request, 'No se pudo actualizar el ticket (ya cerrado o no encontrado).')
            return redirect('tickets_lista')
    messages.success(request, 'Ticket actualizado correctamente.')
    return redirect('tickets_lista')

@login_requerido
@nivel_requerido('modificar')
def ticket_cerrar(request, id_ticket):
    if request.method != 'POST':
        return redirect('tickets_lista')
    if not _ticket_es_propio(request, id_ticket):
        messages.error(request, 'No puedes cerrar un ticket que no es tuyo.')
        return redirect('tickets_lista')
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE ticket_usuario
            SET estatus_usuario=TRUE, fecha_cierre=%s
            WHERE id_ticket=%s AND estatus_usuario=FALSE
        """, [datetime.now(), id_ticket])
        if cursor.rowcount:
            cursor.execute("SELECT titulo FROM ticket_usuario WHERE id_ticket = %s", [id_ticket])
            titulo = cursor.fetchone()[0]
            cursor.execute("SELECT id_usuario FROM usuarios WHERE fk_rol = 'ti1' AND estatus = 'ACT'")
            for (ti_id,) in cursor.fetchall():
                _crear_notificacion(cursor, ti_id, id_ticket, 'NUEVO_TICKET',
                                     f'Nuevo ticket #{id_ticket} enviado: "{titulo}".')
    messages.success(request, 'Ticket cerrado y enviado a T.I correctamente.')
    return redirect('tickets_lista')

def _seguimiento_tickets(request):
    if not _niveles_sesion(request).get('consultar'):
        return []
    rol_id = request.session.get('usuario_rol_id')
    usuario_id = request.session.get('usuario_id')
    filtro_rol = "" if rol_id == 'ti1' else "AND u.fk_rol != 'ti1'"
    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT t.id_ticket, t.titulo, t.area, t.dirigido_personal,
                   t.estatus, t.fecha_creacion, t.fecha_cierre, t.descripcion,
                   u.nombre, u.apellido_paterno, u.apellido_materno,
                   s.estatus_firmas, (t.fk_usuario = %s) AS es_propio
            FROM ticket_usuario t
            LEFT JOIN usuarios u ON u.id_usuario = t.fk_usuario
            LEFT JOIN seguimiento_ticket s ON s.fk_ticket = t.id_ticket
            WHERE t.estatus_usuario = TRUE
            {filtro_rol}
            ORDER BY CASE t.estatus WHEN 'PEN' THEN 1 WHEN 'PRO' THEN 2 WHEN 'FIN' THEN 3 ELSE 4 END,
                     t.fecha_cierre DESC
        """, [usuario_id])
        cols = ['id_ticket','titulo','area','dirigido_personal',
                'estatus','fecha_creacion','fecha_cierre','descripcion',
                'creador_nombre','creador_apellido','creador_materno','estatus_firmas','es_propio']
        return [dict(zip(cols, r)) for r in cursor.fetchall()]

@login_requerido
def tickets_seguimiento(request):
    ctx = {
        'tickets': _seguimiento_tickets(request),
        'username': request.session.get('usuario_username', ''),
        'nombre':   request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'rol':      request.session.get('usuario_rol', 'Usuario'),
        'rol_id':   request.session.get('usuario_rol_id'),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'tickets/seguimiento.html', ctx)

@login_requerido
@nivel_requerido('consultar')
def tickets_seguimiento_actualizacion(request):
    from django.template.loader import render_to_string
    tickets = _seguimiento_tickets(request)
    tbody_html = render_to_string('tickets/_filas_seguimiento.html', {
        'tickets': tickets,
        'rol_id': request.session.get('usuario_rol_id'),
    }, request=request)
    return JsonResponse({'tbody_html': tbody_html})

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('modificar')
def ticket_atender(request, id_ticket):
    if request.method != 'POST':
        return redirect('tickets_seguimiento')
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE ticket_usuario SET estatus='PRO'
            WHERE id_ticket=%s AND estatus_usuario=TRUE AND estatus='PEN'
        """, [id_ticket])
        if cursor.rowcount:
            cursor.execute("""
                INSERT INTO seguimiento_ticket
                  (fk_ticket, fk_usuario_ti, estatus_firmas, fecha_creacion_atencion,
                   fk_equipo, fk_nombre_equipo)
                SELECT %s, %s, 'PEN', %s, fk_equipo, fk_nombre_equipo
                FROM ticket_usuario WHERE id_ticket = %s
            """, [id_ticket, request.session.get('usuario_id'), datetime.now(), id_ticket])
            cursor.execute("SELECT fk_usuario FROM ticket_usuario WHERE id_ticket = %s", [id_ticket])
            creador_id = cursor.fetchone()[0]
            _crear_notificacion(cursor, creador_id, id_ticket, 'PROCESO',
                                 f'Tu ticket #{id_ticket} pasó a En Proceso.')
    messages.success(request, f'Ticket #{id_ticket} marcado como en atención.')
    return redirect('tickets_seguimiento')

def _puede_ver_atencion(request, id_ticket):
    if request.session.get('usuario_rol_id') == 'ti1':
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT fk_usuario FROM ticket_usuario WHERE id_ticket = %s", [id_ticket])
        row = cursor.fetchone()
    return row is not None and row[0] == request.session.get('usuario_id')

@login_requerido
@nivel_requerido('consultar')
def atencion_datos(request, id_ticket):
    if not _puede_ver_atencion(request, id_ticket):
        return JsonResponse({'error': 'No encontrado'}, status=404)
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT s.diagnostico, s.solucion, s.observacion, s.tipo_ticket, s.lugar_actividad,
                   s.estatus_firmas,
                   s.imagen_1 IS NOT NULL, s.imagen_2 IS NOT NULL,
                   s.imagen_3 IS NOT NULL, s.imagen_4 IS NOT NULL,
                   s.firma_ti IS NOT NULL,
                   t.titulo, t.descripcion, t.fk_nombre_equipo,
                   u.nombre, u.apellido_paterno, u.apellido_materno
            FROM seguimiento_ticket s
            JOIN ticket_usuario t ON t.id_ticket = s.fk_ticket
            LEFT JOIN usuarios u ON u.id_usuario = t.fk_usuario
            WHERE s.fk_ticket = %s
        """, [id_ticket])
        row = cursor.fetchone()
    if not row:
        return JsonResponse({'error': 'No encontrado'}, status=404)
    return JsonResponse({
        'diagnostico':     row[0] or '',
        'solucion':        row[1] or '',
        'observacion':     row[2] or '',
        'tipo_ticket':     row[3] or '',
        'lugar_actividad': row[4] or '',
        'estatus_firmas':  row[5] or '',
        'tiene_imagen_1':  bool(row[6]),
        'tiene_imagen_2':  bool(row[7]),
        'tiene_imagen_3':  bool(row[8]),
        'tiene_imagen_4':  bool(row[9]),
        'tiene_firma_ti':  bool(row[10]),
        'ticket_titulo':      row[11] or '',
        'ticket_descripcion': row[12] or '',
        'ticket_equipo':      row[13] or '',
        'ticket_creador':     ' '.join(p for p in row[14:17] if p),
    })

@login_requerido
@nivel_requerido('consultar')
def atencion_imagen(request, id_ticket, n):
    from django.http import HttpResponse, Http404
    if not _puede_ver_atencion(request, id_ticket):
        raise Http404
    col = f'imagen_{n}'
    if col not in ('imagen_1', 'imagen_2', 'imagen_3', 'imagen_4'):
        raise Http404
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {col} FROM seguimiento_ticket WHERE fk_ticket = %s", [id_ticket])
        row = cursor.fetchone()
    if not row or row[0] is None:
        raise Http404
    return HttpResponse(bytes(row[0]), content_type='image/jpeg')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('modificar')
def atencion_guardar(request, id_ticket):
    import json as json_lib
    if request.method != 'POST':
        return redirect('tickets_seguimiento')
    diagnostico     = request.POST.get('diagnostico', '').strip()
    solucion        = request.POST.get('solucion', '').strip()
    observacion     = request.POST.get('observacion', '').strip() or None
    tipo_ticket     = request.POST.get('tipo_ticket', '').strip()
    lugar_actividad = request.POST.get('lugar_actividad', '').strip()
    firma_ti_json   = request.POST.get('firma_ti_json', '').strip()
    if not diagnostico or not solucion or not tipo_ticket or not lugar_actividad:
        messages.error(request, 'Diagnóstico, Solución, Tipo de Ticket y Lugar de Actividad son obligatorios.')
        return redirect('tickets_seguimiento')
    sets, vals = [
        'diagnostico=%s', 'solucion=%s', 'observacion=%s',
        'tipo_ticket=%s', 'lugar_actividad=%s',
    ], [diagnostico, solucion, observacion, tipo_ticket, lugar_actividad]
    for i in range(1, 5):
        f = request.FILES.get(f'imagen_{i}')
        if f:
            sets.append(f'imagen_{i}=%s')
            vals.append(f.read())
    with connection.cursor() as cursor:
        if firma_ti_json:
            usuario_ti_id = request.session.get('usuario_id')
            cursor.execute("""
                SELECT u.nombre, u.apellido_paterno, u.apellido_materno,
                       r.descripcion, p.descripcion, n.descripcion
                FROM usuarios u
                LEFT JOIN cat_roles r ON r.id_rol = u.fk_rol
                LEFT JOIN cat_permisos p ON p.id_permiso = r.fk_permisos
                LEFT JOIN cat_nivel n ON n.id_nivel = p.id_nivel
                WHERE u.id_usuario = %s
            """, [usuario_ti_id])
            firmante = cursor.fetchone()
            firma_obj = {
                'fecha':      datetime.now().isoformat(),
                'usuario_id': usuario_ti_id,
                'nombre':     ' '.join(p for p in firmante[0:3] if p) if firmante else '',
                'rol':        firmante[3] if firmante else None,
                'permiso':    firmante[4] if firmante else None,
                'nivel':      firmante[5] if firmante else None,
                'firma':      json_lib.loads(firma_ti_json),
            }
            sets.append('firma_ti=%s')
            vals.append(json_lib.dumps(firma_obj))
            sets.append("estatus_firmas='PTI'")
        cursor.execute(f"""
            UPDATE seguimiento_ticket SET {', '.join(sets)}
            WHERE fk_ticket=%s AND estatus_firmas='PEN'
        """, vals + [id_ticket])
        if cursor.rowcount == 0:
            messages.error(request, 'No se pudo guardar (el registro ya fue firmado o no existe).')
            return redirect('tickets_seguimiento')
        if firma_ti_json:
            cursor.execute("SELECT fk_usuario FROM ticket_usuario WHERE id_ticket = %s", [id_ticket])
            creador_id = cursor.fetchone()[0]
            _crear_notificacion(cursor, creador_id, id_ticket, 'FIRMA_PENDIENTE',
                                 f'T.I ya firmó el ticket #{id_ticket} — Falta tu firma de conformidad.')
    messages.success(request, f'Atención del ticket #{id_ticket} guardada correctamente.')
    return redirect('tickets_seguimiento')

@login_requerido
@nivel_requerido('modificar')
def firma_usuario_guardar(request, id_ticket):
    import json as json_lib
    if request.method != 'POST':
        return redirect('tickets_seguimiento')
    with connection.cursor() as cursor:
        cursor.execute("SELECT fk_usuario FROM ticket_usuario WHERE id_ticket = %s", [id_ticket])
        row = cursor.fetchone()
        if not row or row[0] != request.session.get('usuario_id'):
            messages.error(request, 'No puedes firmar un ticket que no es tuyo.')
            return redirect('tickets_seguimiento')
        firma_usuario_json = request.POST.get('firma_usuario_json', '').strip()
        if not firma_usuario_json:
            messages.error(request, 'Debes dibujar tu firma para dar conformidad.')
            return redirect('tickets_seguimiento')
        usuario_id = request.session.get('usuario_id')
        cursor.execute("""
            SELECT u.nombre, u.apellido_paterno, u.apellido_materno,
                   r.descripcion, p.descripcion, n.descripcion
            FROM usuarios u
            LEFT JOIN cat_roles r ON r.id_rol = u.fk_rol
            LEFT JOIN cat_permisos p ON p.id_permiso = r.fk_permisos
            LEFT JOIN cat_nivel n ON n.id_nivel = p.id_nivel
            WHERE u.id_usuario = %s
        """, [usuario_id])
        firmante = cursor.fetchone()
        firma_obj = {
            'fecha':      datetime.now().isoformat(),
            'usuario_id': usuario_id,
            'nombre':     ' '.join(p for p in firmante[0:3] if p) if firmante else '',
            'rol':        firmante[3] if firmante else None,
            'permiso':    firmante[4] if firmante else None,
            'nivel':      firmante[5] if firmante else None,
            'firma':      json_lib.loads(firma_usuario_json),
        }
        cursor.execute("""
            UPDATE seguimiento_ticket
            SET firma_usuario=%s, estatus_firmas='FIN', fecha_cierre_atencion=%s
            WHERE fk_ticket=%s AND estatus_firmas='PTI'
            RETURNING fk_usuario_ti
        """, [json_lib.dumps(firma_obj), datetime.now(), id_ticket])
        resultado = cursor.fetchone()
        if not resultado:
            messages.error(request, 'No se pudo firmar (el ticket no está listo para tu firma).')
            return redirect('tickets_seguimiento')
        fk_usuario_ti = resultado[0]
        cursor.execute("UPDATE ticket_usuario SET estatus='FIN' WHERE id_ticket=%s", [id_ticket])
        _crear_notificacion(cursor, usuario_id, id_ticket, 'FINALIZADO',
                             f'Tu ticket #{id_ticket} quedó cerrado y firmado.')
        if fk_usuario_ti:
            _crear_notificacion(cursor, fk_usuario_ti, id_ticket, 'FINALIZADO',
                                 f'El usuario firmó de conformidad el ticket #{id_ticket}. Quedó cerrado.')
    messages.success(request, f'Firmaste de conformidad el ticket #{id_ticket}.')
    return redirect('tickets_seguimiento')

@login_requerido
@nivel_requerido('consultar')
def reporte_mantenimiento(request, id_ticket):
    import json as json_lib
    if not _puede_ver_atencion(request, id_ticket):
        messages.error(request, 'No tienes permiso para ver este reporte.')
        return redirect('tickets_seguimiento')
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT s.lugar_actividad, s.fecha_cierre_atencion, s.tipo_ticket,
                   s.fk_nombre_equipo, s.diagnostico, s.solucion, s.observacion,
                   s.imagen_1 IS NOT NULL, s.imagen_2 IS NOT NULL,
                   s.imagen_3 IS NOT NULL, s.imagen_4 IS NOT NULL,
                   s.estatus_firmas,
                   t.descripcion,
                   uc.nombre, uc.apellido_paterno, uc.apellido_materno,
                   ti.nombre, ti.apellido_paterno, ti.apellido_materno,
                   s.firma_ti
            FROM seguimiento_ticket s
            JOIN ticket_usuario t ON t.id_ticket = s.fk_ticket
            LEFT JOIN usuarios uc ON uc.id_usuario = t.fk_usuario
            LEFT JOIN usuarios ti ON ti.id_usuario = s.fk_usuario_ti
            WHERE s.fk_ticket = %s
        """, [id_ticket])
        row = cursor.fetchone()
    if not row or row[11] != 'FIN':
        messages.error(request, 'El reporte solo está disponible cuando el ticket ya fue firmado por ambas partes.')
        return redirect('tickets_seguimiento')
    firma_ti_data = row[19]
    if isinstance(firma_ti_data, str):
        firma_ti_data = json_lib.loads(firma_ti_data)
    firma_ti_trazos = firma_ti_data.get('firma', []) if isinstance(firma_ti_data, dict) else []
    ctx = {
        'id_ticket': id_ticket,
        'lugar_actividad': row[0],
        'fecha_cierre': row[1],
        'tipo_ticket': row[2],
        'nombre_equipo': row[3],
        'diagnostico': row[4],
        'solucion': row[5],
        'observacion': row[6],
        'tiene_imagen_1': bool(row[7]),
        'tiene_imagen_2': bool(row[8]),
        'tiene_imagen_3': bool(row[9]),
        'tiene_imagen_4': bool(row[10]),
        'cantidad_imagenes': sum(bool(row[i]) for i in (7, 8, 9, 10)),
        'descripcion_trabajo': row[12],
        'creador_nombre': ' '.join(p for p in row[13:16] if p),
        'firma_ti_json': json_lib.dumps(firma_ti_trazos),
        'tecnico_nombre': ' '.join(p for p in row[16:19] if p),
        'es_ti': request.session.get('usuario_rol_id') == 'ti1',
    }
    return render(request, 'tickets/reporte.html', ctx)

_pdf_cola = None
_pdf_cola_lock = None
_pdf_cache = {} 

def _pdf_trabajador(cola):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    navegador = pw.chromium.launch()
    while True:
        trabajo = cola.get()
        try:
            if not navegador.is_connected():
                navegador = pw.chromium.launch()
            contexto = navegador.new_context()
            if trabajo['cookie_valor']:
                contexto.add_cookies([{
                    'name': trabajo['cookie_nombre'],
                    'value': trabajo['cookie_valor'],
                    'url': trabajo['origen'],
                }])
            pagina = contexto.new_page()
            pagina.goto(trabajo['url'], wait_until='load')
            trabajo['pdf'] = pagina.pdf(
                format='Letter', print_background=True,
                margin={'top': '6mm', 'bottom': '12mm', 'left': '15mm', 'right': '15mm'},
            )
            contexto.close()
        except Exception as exc:
            trabajo['error'] = exc
        finally:
            trabajo['listo'].set()

def _generar_pdf(url, cookie_nombre, cookie_valor, origen):
    import threading, queue
    global _pdf_cola, _pdf_cola_lock
    if _pdf_cola_lock is None:
        _pdf_cola_lock = threading.Lock()
    with _pdf_cola_lock:
        if _pdf_cola is None:
            _pdf_cola = queue.Queue()
            threading.Thread(target=_pdf_trabajador, args=(_pdf_cola,), daemon=True).start()
    trabajo = {
        'url': url, 'cookie_nombre': cookie_nombre, 'cookie_valor': cookie_valor,
        'origen': origen, 'pdf': None, 'error': None, 'listo': threading.Event(),
    }
    _pdf_cola.put(trabajo)
    if not trabajo['listo'].wait(timeout=90):
        raise TimeoutError('La generación del PDF tardó demasiado.')
    if trabajo['error']:
        raise trabajo['error']
    return trabajo['pdf']

@login_requerido
def reporte_mantenimiento_pdf(request, id_ticket):
    import os
    from django.conf import settings
    from django.urls import reverse
    from django.http import HttpResponse
    from django.template.loader import get_template
    if not _puede_ver_atencion(request, id_ticket):
        messages.error(request, 'No tienes permiso para ver este reporte.')
        return redirect('tickets_seguimiento')
    if not _niveles_sesion(request).get('exportar'):
        messages.error(request, 'Sin nivel de acceso para descargar o imprimir el reporte.')
        return redirect('reporte_mantenimiento', id_ticket=id_ticket)
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        messages.error(request, 'Playwright no está instalado en el servidor.')
        return redirect('reporte_mantenimiento', id_ticket=id_ticket)
    mtime_plantilla = os.path.getmtime(get_template('tickets/reporte.html').origin.name)
    clave = (str(id_ticket), mtime_plantilla)
    pdf_bytes = _pdf_cache.get(clave)
    if pdf_bytes is None:
        origen = request.build_absolute_uri('/')
        url = request.build_absolute_uri(reverse('reporte_mantenimiento', args=[id_ticket]))
        cookie_nombre = settings.SESSION_COOKIE_NAME
        cookie_valor = request.COOKIES.get(cookie_nombre)
        try:
            pdf_bytes = _generar_pdf(url, cookie_nombre, cookie_valor, origen)
        except Exception:
            messages.error(request, 'No se pudo generar el PDF, inténtalo de nuevo.')
            return redirect('reporte_mantenimiento', id_ticket=id_ticket)
        for vieja in [k for k in _pdf_cache if k[0] == str(id_ticket) and k != clave]:
            _pdf_cache.pop(vieja, None)
        _pdf_cache[clave] = pdf_bytes
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="reporte-mantenimiento-ticket-{id_ticket}.pdf"'
    return response

@login_requerido
def notificaciones_lista(request):
    usuario_id = request.session.get('usuario_id')
    niveles = _niveles_sesion(request)
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id_notificacion, fk_ticket, mensaje, leida, fecha_creacion
            FROM notificaciones
            WHERE fk_usuario = %s
            ORDER BY fecha_creacion DESC
            LIMIT 30
        """, [usuario_id])
        notis = [{
            'id_notificacion': r[0], 'fk_ticket': r[1], 'mensaje': r[2],
            'leida': bool(r[3]), 'fecha': r[4].strftime('%d/%m/%Y %H:%M') if r[4] else '',
        } for r in cursor.fetchall()]
        cursor.execute("SELECT COUNT(*) FROM notificaciones WHERE fk_usuario = %s AND leida = FALSE", [usuario_id])
        no_leidas = cursor.fetchone()[0]
    return JsonResponse({'notificaciones': notis, 'no_leidas': no_leidas, 'niveles': niveles})

@login_requerido
def notificaciones_marcar_leida(request, id_notificacion):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido'}, status=405)
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE notificaciones SET leida = TRUE
            WHERE id_notificacion = %s AND fk_usuario = %s
        """, [id_notificacion, request.session.get('usuario_id')])
    return JsonResponse({'ok': True})

@login_requerido
def notificaciones_marcar_todas(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido'}, status=405)
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE notificaciones SET leida = TRUE
            WHERE fk_usuario = %s AND leida = FALSE
        """, [request.session.get('usuario_id')])
    return JsonResponse({'ok': True})

RECURSOS_RESERVA = {'proyector': 'PROYECTOR', 'sala': 'SALA'}
NOMBRES_RECURSO = {'PROYECTOR': 'Proyector', 'SALA': 'Sala de Juntas'}
TIPOS_RECURSO = {
    'proyector': ['Proyector', 'Sala de usos múltiples'],
    'sala': ['Sala de juntas', 'Sala de usos múltiples', 'Gerencia'],
}

def _horario_del_dia(fecha):
    """Horario permitido: L-V 8:00-17:00, sábado 9:00-13:00, domingo cerrado."""
    from datetime import time as time_type
    dia = fecha.weekday() 
    if dia == 6:
        return None
    if dia == 5:
        return (time_type(9, 0), time_type(13, 0))
    return (time_type(8, 0), time_type(17, 0))

@login_requerido
def reservaciones_vista(request, recurso):
    if recurso not in RECURSOS_RESERVA:
        return redirect('dashboard')
    codigo = RECURSOS_RESERVA[recurso]
    ctx = {
        'recurso_slug': recurso,
        'recurso_codigo': codigo,
        'recurso_nombre': NOMBRES_RECURSO[codigo],
        'rol_id': request.session.get('usuario_rol_id', ''),
        'username': request.session.get('usuario_username', ''),
        'nombre': request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'reservaciones/calendario.html', ctx)

@login_requerido
@nivel_requerido('consultar')
def reservaciones_datos(request, recurso):
    if recurso not in RECURSOS_RESERVA:
        return JsonResponse({'error': 'Recurso inválido'}, status=404)
    try:
        anio = int(request.GET.get('anio', ''))
        mes = int(request.GET.get('mes', ''))
    except ValueError:
        return JsonResponse({'error': 'Mes inválido'}, status=400)
    usuario_id = request.session.get('usuario_id')
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT r.id_reservacion, r.fecha, r.hora_inicio, r.hora_fin, r.estatus,
                   u.nombre, u.apellido_paterno, u.area, r.fk_usuario, r.duracion_min,
                   r.tipo_recurso, r.motivo
            FROM reservaciones r
            JOIN usuarios u ON u.id_usuario = r.fk_usuario
            WHERE r.recurso = %s AND r.estatus IN ('PEN', 'APR')
              AND EXTRACT(YEAR FROM r.fecha) = %s AND EXTRACT(MONTH FROM r.fecha) = %s
            ORDER BY r.fecha, r.hora_inicio
        """, [RECURSOS_RESERVA[recurso], anio, mes])
        reservas = [{
            'id': r[0],
            'fecha': r[1].strftime('%Y-%m-%d'),
            'hora_inicio': r[2].strftime('%H:%M'),
            'hora_fin': r[3].strftime('%H:%M'),
            'estatus': r[4],
            'solicitante': ' '.join(p for p in (r[5], r[6]) if p),
            'area': r[7] or '',
            'es_propia': r[8] == usuario_id,
            'duracion_min': r[9],
            'tipo_recurso': r[10] or '',
            'motivo': r[11] or '',
        } for r in cursor.fetchall()]
    return JsonResponse({'reservas': reservas})

@login_requerido
@nivel_requerido('crear')
def reservacion_guardar(request):
    from datetime import timedelta
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido'}, status=405)
    recurso = request.POST.get('recurso', '')
    if recurso not in RECURSOS_RESERVA:
        return JsonResponse({'error': 'Recurso inválido'}, status=400)
    codigo = RECURSOS_RESERVA[recurso]
    try:
        fecha = datetime.strptime(request.POST.get('fecha', ''), '%Y-%m-%d').date()
        hora_inicio = datetime.strptime(request.POST.get('hora_inicio', ''), '%H:%M').time()
        duracion = int(request.POST.get('duracion_min', ''))
    except ValueError:
        return JsonResponse({'error': 'Datos incompletos o inválidos.'}, status=400)
    tipo_recurso = request.POST.get('tipo_recurso', '').strip()
    motivo = request.POST.get('motivo', '').strip()
    if tipo_recurso not in TIPOS_RECURSO[recurso]:
        return JsonResponse({'error': 'Selecciona el tipo de recurso.'}, status=400)
    if not motivo:
        return JsonResponse({'error': 'Escribe el motivo de tu solicitud.'}, status=400)
    horario = _horario_del_dia(fecha)
    if horario is None:
        return JsonResponse({'error': 'Los domingos no hay servicio.'}, status=400)
    if duracion < 30 or duracion % 30 != 0:
        return JsonResponse({'error': 'La duración debe ser en bloques de 30 minutos.'}, status=400)
    inicio_dt = datetime.combine(fecha, hora_inicio)
    fin_dt = inicio_dt + timedelta(minutes=duracion)
    hora_fin = fin_dt.time()
    if fin_dt.date() != fecha:
        return JsonResponse({'error': 'La reservación no puede terminar en otro día.'}, status=400)
    if hora_inicio < horario[0] or hora_fin > horario[1]:
        h0 = horario[0].strftime('%H:%M')
        h1 = horario[1].strftime('%H:%M')
        return JsonResponse({'error': f'El horario de ese día es de {h0} a {h1}.'}, status=400)
    if inicio_dt <= datetime.now():
        return JsonResponse({'error': 'No puedes apartar una fecha u hora que ya pasó.'}, status=400)
    usuario_id = request.session.get('usuario_id')
    with connection.cursor() as cursor:
        filtro_tipo = " AND tipo_recurso = %s" if codigo == 'SALA' else ""
        params = [codigo, fecha, hora_fin, hora_inicio]
        if codigo == 'SALA':
            params.append(tipo_recurso)
        cursor.execute(f"""
            SELECT COUNT(*) FROM reservaciones
            WHERE recurso = %s AND fecha = %s AND estatus IN ('PEN', 'APR')
              AND hora_inicio < %s AND hora_fin > %s{filtro_tipo}
        """, params)
        if cursor.fetchone()[0] > 0:
            return JsonResponse({'error': 'Ese horario ya está apartado o en espera de autorización.'}, status=400)
        cursor.execute("""
            INSERT INTO reservaciones (recurso, fk_usuario, fecha, hora_inicio, hora_fin,
                                       duracion_min, estatus, fecha_creacion, tipo_recurso, motivo)
            VALUES (%s, %s, %s, %s, %s, %s, 'PEN', %s, %s, %s)
            RETURNING id_reservacion
        """, [codigo, usuario_id, fecha, hora_inicio, hora_fin, duracion,
              datetime.now(), tipo_recurso, motivo])
        id_reservacion = cursor.fetchone()[0]
        solicitante = request.session.get('usuario_nombre', '')
        mensaje = (f"{solicitante} solicita {tipo_recurso} el "
                   f"{fecha.strftime('%d/%m/%Y')} de {hora_inicio.strftime('%H:%M')} "
                   f"a {hora_fin.strftime('%H:%M')}")
        cursor.execute("SELECT id_usuario FROM usuarios WHERE fk_rol = 'ti1' AND estatus != 'INA'")
        for (ti_id,) in cursor.fetchall():
            _crear_notificacion(cursor, ti_id, id_reservacion, 'RESERVA', mensaje[:200])
    return JsonResponse({'ok': True, 'id': id_reservacion})

@login_requerido
@nivel_requerido('modificar')
def reservacion_responder(request, id_reservacion):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido'}, status=405)
    if request.session.get('usuario_rol_id') != 'ti1':
        return JsonResponse({'error': 'Solo T.I. puede autorizar o rechazar.'}, status=403)
    accion = request.POST.get('accion', '')
    motivo = request.POST.get('motivo', '').strip()
    if accion not in ('APR', 'REC', 'CAN'):
        return JsonResponse({'error': 'Acción inválida.'}, status=400)
    if accion in ('REC', 'CAN') and not motivo:
        return JsonResponse({'error': 'Escribe el motivo.'}, status=400)
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT recurso, fk_usuario, fecha, hora_inicio, hora_fin, estatus, tipo_recurso
            FROM reservaciones WHERE id_reservacion = %s
        """, [id_reservacion])
        row = cursor.fetchone()
        if not row:
            return JsonResponse({'error': 'La reservación no existe.'}, status=404)
        if accion == 'CAN' and row[5] != 'APR':
            return JsonResponse({'error': 'Solo se puede cancelar una reservación ya autorizada.'}, status=400)
        if accion in ('APR', 'REC') and row[5] != 'PEN':
            return JsonResponse({'error': 'Esta reservación ya fue respondida.'}, status=400)
        cursor.execute("""
            UPDATE reservaciones
            SET estatus = %s, motivo_rechazo = %s, fk_ti_respondio = %s, fecha_respuesta = %s
            WHERE id_reservacion = %s
        """, [accion, motivo or None, request.session.get('usuario_id'),
              datetime.now(), id_reservacion])
        nombre_recurso = row[6] or NOMBRES_RECURSO.get(row[0], row[0])
        rango = (f"el {row[2].strftime('%d/%m/%Y')} de {row[3].strftime('%H:%M')} "
                 f"a {row[4].strftime('%H:%M')}")
        if accion == 'APR':
            mensaje = f"Tu reservación de {nombre_recurso} {rango} fue AUTORIZADA."
        elif accion == 'CAN':
            mensaje = f"T.I. canceló tu reservación de {nombre_recurso} {rango}: {motivo}"
        else:
            mensaje = f"Tu reservación de {nombre_recurso} {rango} fue rechazada: {motivo}"
        _crear_notificacion(cursor, row[1], id_reservacion, 'RESERVA', mensaje[:200])
    return JsonResponse({'ok': True})

@login_requerido
@rol_requerido('ti1')
def equipos_lista(request):
    if not _niveles_sesion(request).get('consultar'):
        return render(request, 'equipos/lista.html', {
            'username': request.session.get('usuario_username', ''),
            'nombre':   request.session.get('usuario_nombre', ''),
            'apellido': request.session.get('usuario_paterno', ''),
            'rol':      request.session.get('usuario_rol', 'Usuario'),
            'usuario_area': request.session.get('usuario_area', ''),
            'stats': {}, 'equipos': [],
        })
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN estatus = 'ACT' THEN 1 ELSE 0 END) AS activos,
                SUM(CASE WHEN estatus = 'INA' THEN 1 ELSE 0 END) AS inactivos,
                SUM(CASE WHEN tipo_equipo = 'Laptop'    THEN 1 ELSE 0 END) AS laptops,
                SUM(CASE WHEN tipo_equipo = 'PC'        THEN 1 ELSE 0 END) AS pcs,
                SUM(CASE WHEN tipo_equipo = 'Servidor'  THEN 1 ELSE 0 END) AS servidores,
                SUM(CASE WHEN tipo_equipo = 'Impresora' THEN 1 ELSE 0 END) AS impresoras
            FROM cat_equipos
        """)
        s = cursor.fetchone()
        cursor.execute("""
            SELECT id_equipo, nombre_equipo, tipo_equipo, marca, modelo,
                   numero_serie_equipo, usuario_asignado, estatus
            FROM cat_equipos
            ORDER BY fecha_creacion DESC
        """)
        equipos = cursor.fetchall()
    context = {
        'username': request.session.get('usuario_username', ''),
        'nombre':   request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'rol':      request.session.get('usuario_rol', 'Usuario'),
        'usuario_area': request.session.get('usuario_area', ''),
        'stats': {
            'total':      s[0] or 0,
            'activos':    s[1] or 0,
            'inactivos':  s[2] or 0,
            'laptops':    s[3] or 0,
            'pcs':        s[4] or 0,
            'servidores': s[5] or 0,
            'impresoras': s[6] or 0,
        },
        'equipos': equipos,
    }
    return render(request, 'equipos/lista.html', context)

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('crear')
def equipo_guardar(request):
    if request.method != 'POST':
        return redirect('equipos_lista')
    nombre_equipo       = request.POST.get('nombre_equipo', '').strip()
    tipo_equipo         = request.POST.get('tipo_equipo', '').strip()
    marca               = request.POST.get('marca', '').strip() or None
    modelo              = request.POST.get('modelo', '').strip() or None
    numero_serie_equipo = request.POST.get('numero_serie_equipo', '').strip() or None
    estatus             = request.POST.get('estatus', 'ACT')
    observacion_equipo  = request.POST.get('observacion_equipo', '').strip() or None
    nota_equipo         = request.POST.get('nota_equipo', '').strip() or None
    bat_str             = request.POST.get('bateria_integrada', '')
    bateria_integrada   = True if bat_str == 'true' else (False if bat_str == 'false' else None)
    numero_serie_bateria  = request.POST.get('numero_serie_bateria', '').strip() or None
    numero_serie_cargador = request.POST.get('numero_serie_cargador', '').strip() or None
    procesador       = request.POST.get('procesador', '').strip() or None
    ram              = request.POST.get('ram', '').strip() or None
    almacenamiento   = request.POST.get('almacenamiento', '').strip() or None
    sistema_operativo = request.POST.get('sistema_operativo', '').strip() or None
    version_office   = request.POST.get('version_office', '').strip() or None
    usuario_asignado = request.POST.get('usuario_asignado', '').strip() or None
    area_asignada    = request.POST.get('area_asignada', '').strip() or None
    now                   = datetime.now()
    fecha_asignacion_form = request.POST.get('fecha_asignacion', '').strip() or None
    if fecha_asignacion_form:
        fecha_asignacion = fecha_asignacion_form
    elif usuario_asignado:
        fecha_asignacion = now
    else:
        fecha_asignacion = None
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO cat_equipos (
                nombre_equipo, tipo_equipo, marca, modelo,
                numero_serie_equipo, estatus, observacion_equipo, nota_equipo,
                fecha_creacion, bateria_integrada, numero_serie_bateria,
                numero_serie_cargador, procesador, ram, almacenamiento,
                sistema_operativo, version_office, usuario_asignado,
                area_asignada, fecha_asignacion
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            ) RETURNING id_equipo
        """, [
            nombre_equipo, tipo_equipo, marca, modelo,
            numero_serie_equipo, estatus, observacion_equipo, nota_equipo,
            now, bateria_integrada, numero_serie_bateria, numero_serie_cargador,
            procesador, ram, almacenamiento, sistema_operativo, version_office,
            usuario_asignado, area_asignada, fecha_asignacion,
        ])
        new_id = cursor.fetchone()[0]
    messages.success(request, f'Equipo {nombre_equipo} dado de alta correctamente (ID: {new_id}).')
    return redirect('equipos_lista')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def equipo_datos(request, id_equipo):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id_equipo, nombre_equipo, tipo_equipo, marca, modelo,
                   numero_serie_equipo, estatus, observacion_equipo, nota_equipo,
                   bateria_integrada, numero_serie_bateria, numero_serie_cargador,
                   procesador, ram, almacenamiento, sistema_operativo, version_office,
                   usuario_asignado, area_asignada, fecha_asignacion
            FROM cat_equipos WHERE id_equipo = %s
        """, [id_equipo])
        row = cursor.fetchone()
    if not row:
        return JsonResponse({'error': 'No encontrado'}, status=404)
    cols = ['id_equipo','nombre_equipo','tipo_equipo','marca','modelo',
            'numero_serie_equipo','estatus','observacion_equipo','nota_equipo',
            'bateria_integrada','numero_serie_bateria','numero_serie_cargador',
            'procesador','ram','almacenamiento','sistema_operativo','version_office',
            'usuario_asignado','area_asignada','fecha_asignacion']
    data = {}
    for k, v in zip(cols, row):
        if isinstance(v, bool):
            data[k] = 'true' if v else 'false'
        elif isinstance(v, datetime):
            data[k] = v.strftime('%Y-%m-%dT%H:%M')
        elif isinstance(v, date_type):
            data[k] = v.strftime('%Y-%m-%dT00:00')
        else:
            data[k] = v if v is not None else ''
    return JsonResponse(data)

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('modificar')
def equipo_actualizar(request, id_equipo):
    if request.method != 'POST':
        return redirect('equipos_lista')
    nombre_equipo       = request.POST.get('nombre_equipo', '').strip()
    tipo_equipo         = request.POST.get('tipo_equipo', '').strip()
    marca               = request.POST.get('marca', '').strip() or None
    modelo              = request.POST.get('modelo', '').strip() or None
    numero_serie_equipo = request.POST.get('numero_serie_equipo', '').strip() or None
    estatus             = request.POST.get('estatus', 'ACT')
    observacion_equipo  = request.POST.get('observacion_equipo', '').strip() or None
    nota_equipo         = request.POST.get('nota_equipo', '').strip() or None
    bat_str           = request.POST.get('bateria_integrada', '')
    bateria_integrada = True if bat_str == 'true' else (False if bat_str == 'false' else None)
    numero_serie_bateria  = request.POST.get('numero_serie_bateria', '').strip() or None
    numero_serie_cargador = request.POST.get('numero_serie_cargador', '').strip() or None
    procesador        = request.POST.get('procesador', '').strip() or None
    ram               = request.POST.get('ram', '').strip() or None
    almacenamiento    = request.POST.get('almacenamiento', '').strip() or None
    sistema_operativo = request.POST.get('sistema_operativo', '').strip() or None
    version_office    = request.POST.get('version_office', '').strip() or None
    usuario_asignado  = request.POST.get('usuario_asignado', '').strip() or None
    area_asignada         = request.POST.get('area_asignada', '').strip() or None
    fecha_asignacion_form = request.POST.get('fecha_asignacion', '').strip() or None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT usuario_asignado, fecha_asignacion FROM cat_equipos WHERE id_equipo = %s",
            [id_equipo]
        )
        existing = cursor.fetchone()
        prev_usuario = existing[0] if existing else None
        prev_fecha   = existing[1] if existing else None
        if fecha_asignacion_form:
            fecha_asignacion = fecha_asignacion_form
        elif usuario_asignado and not prev_usuario:
            fecha_asignacion = datetime.now()
        elif not usuario_asignado:
            fecha_asignacion = None
        else:
            fecha_asignacion = prev_fecha
        cursor.execute("""
            UPDATE cat_equipos SET
                nombre_equipo=%s, tipo_equipo=%s, marca=%s, modelo=%s,
                numero_serie_equipo=%s, estatus=%s, observacion_equipo=%s, nota_equipo=%s,
                bateria_integrada=%s, numero_serie_bateria=%s, numero_serie_cargador=%s,
                procesador=%s, ram=%s, almacenamiento=%s, sistema_operativo=%s,
                version_office=%s, usuario_asignado=%s, area_asignada=%s,
                fecha_asignacion=%s, fecha_modificacion=%s
            WHERE id_equipo = %s
        """, [
            nombre_equipo, tipo_equipo, marca, modelo,
            numero_serie_equipo, estatus, observacion_equipo, nota_equipo,
            bateria_integrada, numero_serie_bateria, numero_serie_cargador,
            procesador, ram, almacenamiento, sistema_operativo, version_office,
            usuario_asignado, area_asignada, fecha_asignacion, datetime.now(),
            id_equipo,
        ])
    messages.success(request, f'Equipo {nombre_equipo} actualizado correctamente.')
    return redirect('equipos_lista')

@login_requerido
@rol_requerido('ti1')
def equipo_form(request, id_equipo=None):
    nivel_necesario = 'modificar' if id_equipo else 'crear'
    if not _niveles_sesion(request).get(nivel_necesario):
        messages.error(request, 'Sin nivel de acceso.')
        return redirect('equipos_lista')
    context = {
        'username':  request.session.get('usuario_username', ''),
        'nombre':    request.session.get('usuario_nombre', ''),
        'apellido':  request.session.get('usuario_paterno', ''),
        'rol':       request.session.get('usuario_rol', 'Usuario'),
        'usuario_area': request.session.get('usuario_area', ''),
        'editando':  id_equipo is not None,
        'id_equipo': id_equipo,
    }
    return render(request, 'equipos/form.html', context)

@login_requerido
@rol_requerido('ti1')
def usuarios_lista(request):
    if not _niveles_sesion(request).get('consultar'):
        return render(request, 'usuarios/lista.html', {
            'username': request.session.get('usuario_username', ''),
            'nombre':   request.session.get('usuario_nombre', ''),
            'apellido': request.session.get('usuario_paterno', ''),
            'rol':      request.session.get('usuario_rol', 'Usuario'),
            'usuario_area': request.session.get('usuario_area', ''),
            'stats': {'por_rol': []}, 'usuarios': [], 'roles': [],
        })
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN u.estatus = 'ACT' THEN 1 ELSE 0 END) AS activos,
                SUM(CASE WHEN u.estatus = 'INA' THEN 1 ELSE 0 END) AS inactivos
            FROM usuarios u
        """)
        s = cursor.fetchone()
        cursor.execute("""
            SELECT r.id_rol, r.descripcion, COUNT(u.id_usuario) AS cantidad
            FROM cat_roles r
            LEFT JOIN usuarios u ON u.fk_rol = r.id_rol
            GROUP BY r.id_rol, r.descripcion
            ORDER BY r.descripcion
        """)
        roles_stats = [{'id_rol': r[0], 'descripcion': r[1], 'cantidad': r[2]} for r in cursor.fetchall()]
        cursor.execute("""
            SELECT u.id_usuario, u.username, u.password, u.email,
                   u.nombre, u.apellido_paterno, u.apellido_materno,
                   u.telefono, u.estatus, u.fecha_creacion,
                   u.fecha_modificacion, u.fk_rol, r.descripcion
            FROM usuarios u
            LEFT JOIN cat_roles r ON u.fk_rol = r.id_rol
            ORDER BY u.fecha_creacion DESC
        """)
        usuarios = cursor.fetchall()
        cursor.execute("SELECT id_rol, descripcion FROM cat_roles ORDER BY descripcion")
        roles = [{'id_rol': r[0], 'descripcion': r[1]} for r in cursor.fetchall()]
    context = {
        'username': request.session.get('usuario_username', ''),
        'nombre':   request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'rol':      request.session.get('usuario_rol', 'Usuario'),
        'usuario_area': request.session.get('usuario_area', ''),
        'stats': {
            'total':    s[0] or 0,
            'activos':  s[1] or 0,
            'inactivos': s[2] or 0,
            'por_rol':  roles_stats,
        },
        'usuarios': usuarios,
        'roles':    roles,
    }
    return render(request, 'usuarios/lista.html', context)

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('crear')
def usuario_guardar(request):
    if request.method != 'POST':
        return redirect('usuarios_lista')
    id_usuario       = request.POST.get('id_usuario', '').strip()
    username         = request.POST.get('username', '').strip()
    password         = request.POST.get('password', '').strip()
    email            = request.POST.get('email', '').strip() or None
    nombre           = request.POST.get('nombre', '').strip()
    apellido_paterno = request.POST.get('apellido_paterno', '').strip()
    apellido_materno = request.POST.get('apellido_materno', '').strip() or None
    telefono         = request.POST.get('telefono', '').strip() or None
    estatus          = request.POST.get('estatus', 'ACT')
    fk_rol           = request.POST.get('fk_rol', '').strip() or None
    area             = request.POST.get('area', '').strip() or None
    if not id_usuario or len(id_usuario) != 5:
        messages.error(request, 'El ID Usuario es obligatorio y debe tener exactamente 5 caracteres.')
        return redirect('usuarios_lista')
    if not password:
        messages.error(request, 'La Contraseña es obligatoria para dar de alta un usuario.')
        return redirect('usuarios_lista')
    if not username or not nombre or not apellido_paterno:
        messages.error(request, 'Faltan campos obligatorios (Username, Nombre, Apellido Paterno).')
        return redirect('usuarios_lista')
    now              = datetime.now()
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO usuarios (
                id_usuario, username, password, email,
                nombre, apellido_paterno, apellido_materno,
                telefono, estatus, fecha_creacion, fk_rol, area
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, [
            id_usuario, username, password, email,
            nombre, apellido_paterno, apellido_materno,
            telefono, estatus, now, fk_rol, area,
        ])
    messages.success(request, f'Usuario {nombre} {apellido_paterno} dado de alta correctamente (ID: {id_usuario}).')
    return redirect('usuarios_lista')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def usuario_datos(request, id_usuario):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id_usuario, username, email,
                   nombre, apellido_paterno, apellido_materno,
                   telefono, estatus, fk_rol, password, area
            FROM usuarios WHERE id_usuario = %s
        """, [id_usuario])
        row = cursor.fetchone()
    if not row:
        return JsonResponse({'error': 'No encontrado'}, status=404)
    cols = ['id_usuario','username','email',
            'nombre','apellido_paterno','apellido_materno',
            'telefono','estatus','fk_rol','password','area']
    data = {k: (v if v is not None else '') for k, v in zip(cols, row)}
    return JsonResponse(data)

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('modificar')
def usuario_actualizar(request, id_usuario):
    if request.method != 'POST':
        return redirect('usuarios_lista')
    username         = request.POST.get('username', '').strip()
    password         = request.POST.get('password', '').strip()
    email            = request.POST.get('email', '').strip() or None
    nombre           = request.POST.get('nombre', '').strip()
    apellido_paterno = request.POST.get('apellido_paterno', '').strip()
    apellido_materno = request.POST.get('apellido_materno', '').strip() or None
    telefono         = request.POST.get('telefono', '').strip() or None
    estatus          = request.POST.get('estatus', 'ACT')
    fk_rol           = request.POST.get('fk_rol', '').strip() or None
    area             = request.POST.get('area', '').strip() or None
    now              = datetime.now()
    with connection.cursor() as cursor:
        if password:
            cursor.execute("""
                UPDATE usuarios SET
                    username=%s, password=%s, email=%s,
                    nombre=%s, apellido_paterno=%s, apellido_materno=%s,
                    telefono=%s, estatus=%s, fk_rol=%s, area=%s,
                    fecha_modificacion=%s
                WHERE id_usuario = %s
            """, [username, password, email, nombre, apellido_paterno,
                  apellido_materno, telefono, estatus, fk_rol, area, now, id_usuario])
        else:
            cursor.execute("""
                UPDATE usuarios SET
                    username=%s, email=%s,
                    nombre=%s, apellido_paterno=%s, apellido_materno=%s,
                    telefono=%s, estatus=%s, fk_rol=%s, area=%s,
                    fecha_modificacion=%s
                WHERE id_usuario = %s
            """, [username, email, nombre, apellido_paterno,
                  apellido_materno, telefono, estatus, fk_rol, area, now, id_usuario])
    messages.success(request, f'Usuario {nombre} {apellido_paterno} actualizado correctamente.')
    return redirect('usuarios_lista')

@login_requerido
@rol_requerido('ti1')
def configuracion_vista(request):
    if not _niveles_sesion(request).get('consultar'):
        return render(request, 'configuracion/lista.html', {
            'lista_niveles': [], 'permisos': [], 'roles': [],
            'username': request.session.get('usuario_username', ''),
            'nombre':   request.session.get('usuario_nombre', ''),
            'apellido': request.session.get('usuario_paterno', ''),
            'rol':      request.session.get('usuario_rol', 'Usuario'),
            'usuario_area': request.session.get('usuario_area', ''),
        })
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id_nivel, descripcion, estatus,
                   consultar, crear, modificar, eliminar, exportar, importar
            FROM cat_nivel ORDER BY id_nivel
        """)
        niveles = [{'id_nivel':r[0],'descripcion':r[1],'estatus':r[2],
                    'consultar':r[3],'crear':r[4],'modificar':r[5],
                    'eliminar':r[6],'exportar':r[7],'importar':r[8]}
                   for r in cursor.fetchall()]
        cursor.execute("""
            SELECT p.id_permiso, p.descripcion, p.estatus, p.id_nivel, n.descripcion
            FROM cat_permisos p
            LEFT JOIN cat_nivel n ON n.id_nivel = p.id_nivel
            ORDER BY p.id_permiso
        """)
        permisos = [{'id_permiso':r[0],'descripcion':r[1],'estatus':r[2],
                     'id_nivel':r[3],'nivel_desc':r[4]}
                    for r in cursor.fetchall()]
        cursor.execute("""
            SELECT r.id_rol, r.descripcion, r.estatus, r.fk_permisos,
                   p.descripcion, COUNT(u.id_usuario)
            FROM cat_roles r
            LEFT JOIN cat_permisos p ON p.id_permiso = r.fk_permisos
            LEFT JOIN usuarios u ON u.fk_rol = r.id_rol
            GROUP BY r.id_rol, r.descripcion, r.estatus, r.fk_permisos, p.descripcion
            ORDER BY r.id_rol
        """)
        roles = [{'id_rol':r[0],'descripcion':r[1],'estatus':r[2],
                  'id_permiso':r[3],'permiso_desc':r[4],'total_usuarios':r[5]}
                 for r in cursor.fetchall()]
    ctx = {
        'lista_niveles': niveles, 'permisos': permisos, 'roles': roles,
        'username': request.session.get('usuario_username', ''),
        'nombre':   request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'rol':      request.session.get('usuario_rol', 'Usuario'),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'configuracion/lista.html', ctx)

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('crear')
def nivel_guardar(request):
    if request.method != 'POST':
        return redirect('configuracion')
    id_nivel    = request.POST.get('id_nivel', '').strip()
    descripcion = request.POST.get('descripcion', '').strip()
    estatus     = request.POST.get('estatus', 'ACT')
    consultar   = 1 if request.POST.get('consultar') else 0
    crear       = 1 if request.POST.get('crear')     else 0
    modificar   = 1 if request.POST.get('modificar') else 0
    eliminar    = 1 if request.POST.get('eliminar')  else 0
    exportar    = 1 if request.POST.get('exportar')  else 0
    importar    = 1 if request.POST.get('importar')  else 0
    if not id_nivel or not descripcion:
        messages.error(request, 'ID Nivel y Descripción son obligatorios.')
        return redirect('configuracion')
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO cat_nivel
              (id_nivel, descripcion, estatus, consultar, crear, modificar, eliminar, exportar, importar)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, [id_nivel, descripcion, estatus, consultar, crear, modificar, eliminar, exportar, importar])
    messages.success(request, f'Nivel "{descripcion}" creado correctamente.')
    return redirect('configuracion')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('modificar')
def nivel_actualizar(request, id_nivel):
    if request.method != 'POST':
        return redirect('configuracion')
    descripcion = request.POST.get('descripcion', '').strip()
    estatus     = request.POST.get('estatus', 'ACT')
    consultar   = 1 if request.POST.get('consultar') else 0
    crear       = 1 if request.POST.get('crear')     else 0
    modificar   = 1 if request.POST.get('modificar') else 0
    eliminar    = 1 if request.POST.get('eliminar')  else 0
    exportar    = 1 if request.POST.get('exportar')  else 0
    importar    = 1 if request.POST.get('importar')  else 0
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE cat_nivel SET
              descripcion=%s, estatus=%s, consultar=%s, crear=%s,
              modificar=%s, eliminar=%s, exportar=%s, importar=%s
            WHERE id_nivel=%s
        """, [descripcion, estatus, consultar, crear, modificar, eliminar, exportar, importar, id_nivel])
    messages.success(request, f'Nivel "{descripcion}" actualizado correctamente.')
    return redirect('configuracion')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('crear')
def permiso_guardar(request):
    if request.method != 'POST':
        return redirect('configuracion')
    id_permiso  = request.POST.get('id_permiso', '').strip()
    descripcion = request.POST.get('descripcion', '').strip()
    estatus     = request.POST.get('estatus', 'ACT')
    id_nivel    = request.POST.get('id_nivel', '').strip() or None
    if not id_permiso or not descripcion:
        messages.error(request, 'ID Permiso y Descripción son obligatorios.')
        return redirect('configuracion')
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO cat_permisos (id_permiso, descripcion, estatus, id_nivel)
            VALUES (%s,%s,%s,%s)
        """, [id_permiso, descripcion, estatus, id_nivel])
    messages.success(request, f'Permiso "{descripcion}" creado correctamente.')
    return redirect('configuracion')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('modificar')
def permiso_actualizar(request, id_permiso):
    if request.method != 'POST':
        return redirect('configuracion')
    descripcion = request.POST.get('descripcion', '').strip()
    estatus     = request.POST.get('estatus', 'ACT')
    id_nivel    = request.POST.get('id_nivel', '').strip() or None
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE cat_permisos SET descripcion=%s, estatus=%s, id_nivel=%s
            WHERE id_permiso=%s
        """, [descripcion, estatus, id_nivel, id_permiso])
    messages.success(request, f'Permiso "{descripcion}" actualizado correctamente.')
    return redirect('configuracion')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('crear')
def rol_guardar(request):
    if request.method != 'POST':
        return redirect('configuracion')
    id_rol      = request.POST.get('id_rol', '').strip()
    descripcion = request.POST.get('descripcion', '').strip()
    estatus     = request.POST.get('estatus', 'ACT')
    id_permiso  = request.POST.get('id_permiso', '').strip() or None
    if not id_rol or not descripcion:
        messages.error(request, 'ID Rol y Descripción son obligatorios.')
        return redirect('configuracion')
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO cat_roles (id_rol, descripcion, estatus, fk_permisos)
            VALUES (%s,%s,%s,%s)
        """, [id_rol, descripcion, estatus, id_permiso])
    messages.success(request, f'Rol "{descripcion}" creado correctamente.')
    return redirect('configuracion')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('modificar')
def rol_actualizar(request, id_rol):
    if request.method != 'POST':
        return redirect('configuracion')
    descripcion = request.POST.get('descripcion', '').strip()
    estatus     = request.POST.get('estatus', 'ACT')
    id_permiso  = request.POST.get('id_permiso', '').strip() or None
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE cat_roles SET descripcion=%s, estatus=%s, fk_permisos=%s
            WHERE id_rol=%s
        """, [descripcion, estatus, id_permiso, id_rol])
    messages.success(request, f'Rol "{descripcion}" actualizado correctamente.')
    return redirect('configuracion')
