from functools import wraps
from datetime import datetime, date as date_type
import json as json_lib
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
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

def _firma_obj(cursor, usuario_id, trazos_json):
    """Arma el objeto de firma enriquecido: trazos + identidad/rol/permiso/nivel
    de quien firmó en ese momento — igual para T.I y para el usuario."""
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
    return {
        'fecha':      datetime.now().isoformat(),
        'usuario_id': usuario_id,
        'nombre':     ' '.join(p for p in firmante[0:3] if p) if firmante else '',
        'rol':        firmante[3] if firmante else None,
        'permiso':    firmante[4] if firmante else None,
        'nivel':      firmante[5] if firmante else None,
        'firma':      json_lib.loads(trazos_json),
    }

def _registrar_actividad(cursor, actividad, descripcion, fecha_actividad, observaciones,
                          fk_usuario_ti, origen_tipo, origen_id):
    """Bitácora automática (F-CECSA-TI-05): una fila por cada servicio que se
    completa, sin captura manual aparte — alimenta el Reporte de Actividades."""
    cursor.execute("""
        INSERT INTO reporte_actividades
          (actividad, descripcion, fecha_actividad, observaciones, fk_usuario_ti,
           origen_tipo, origen_id, fecha_creacion)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, [actividad[:200], (descripcion or '')[:500], fecha_actividad, (observaciones or '')[:300],
          fk_usuario_ti, origen_tipo, origen_id, datetime.now()])

def _piezas_del_cambio(cursor, id_cambio):
    """Piezas de un refaccionamiento, ya separadas en dañadas y nuevas.
    Mismo bloque que antes estaba repetido en atencion_datos,
    historial_mi_equipo_datos y refaccionamiento_datos."""
    cursor.execute("""
        SELECT tipo, cantidad, descripcion, marca, modelo, numero_serie
        FROM atencion_cambio_pieza_detalle WHERE fk_cambio = %s
    """, [id_cambio])
    danadas, nuevas = [], []
    for tipo, cantidad, desc, marca, modelo, serie in cursor.fetchall():
        item = {'cantidad': cantidad, 'descripcion': desc,
                'marca': marca, 'modelo': modelo, 'numero_serie': serie}
        (danadas if tipo == 'DANADA' else nuevas).append(item)
    return danadas, nuevas


def _guardar_cambio_pieza(cursor, fk_ticket, fk_usuario_ti, fk_equipo, fk_nombre_equipo, fecha_cambio,
                           origen, piezas_danadas, piezas_nuevas, firma_ti_obj=None, estatus_inicial=None):
    """Crea la cabecera + el detalle de piezas de un Cambio de Pieza (F-CECSA-TI-08).
    Puede nacer de un ticket, de forma independiente, o automático desde un
    hallazgo de Mantenimiento (en ese caso queda 'ABIERTO' sin piezas nuevas)."""
    estatus = estatus_inicial or ('PTI' if firma_ti_obj else 'PEN')
    cursor.execute("""
        INSERT INTO atencion_cambio_pieza
          (fk_ticket, fk_usuario_ti, fk_equipo, fk_nombre_equipo, fecha_cambio,
           origen, estatus, firma_ti, fecha_creacion)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id_cambio
    """, [fk_ticket, fk_usuario_ti, fk_equipo, fk_nombre_equipo, fecha_cambio, origen, estatus,
          json_lib.dumps(firma_ti_obj) if firma_ti_obj else None, datetime.now()])
    id_cambio = cursor.fetchone()[0]
    for tipo, lista in (('DANADA', piezas_danadas or []), ('NUEVA', piezas_nuevas or [])):
        for pieza in lista:
            cursor.execute("""
                INSERT INTO atencion_cambio_pieza_detalle
                  (fk_cambio, tipo, cantidad, descripcion, marca, modelo, numero_serie)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, [id_cambio, tipo, pieza.get('cantidad') or 1, (pieza.get('descripcion') or '')[:200],
                  (pieza.get('marca') or '')[:100], (pieza.get('modelo') or '')[:100],
                  (pieza.get('numero_serie') or '')[:100]])
    return id_cambio

def _equipos_de_usuario(cursor, usuario_id):
    """Lo inverso de _equipo_usuario_asignado_id: qué equipos tiene asignados
    este usuario, comparando por pedazos del nombre (mismo motivo: texto libre)."""
    cursor.execute("SELECT nombre, apellido_paterno, apellido_materno FROM usuarios WHERE id_usuario = %s", [usuario_id])
    row = cursor.fetchone()
    partes = [p.upper() for p in row if p] if row else []
    if not partes:
        return []
    cursor.execute("SELECT id_equipo, nombre_equipo, usuario_asignado FROM cat_equipos WHERE usuario_asignado IS NOT NULL")
    return [(id_eq, nombre) for id_eq, nombre, asignado in cursor.fetchall()
            if all(parte in asignado.upper() for parte in partes)]

def _puede_ver_servicio_independiente(request, fk_equipo):
    """T.I siempre puede; el usuario solo si ese equipo está asignado a él."""
    if request.session.get('usuario_rol_id') == 'ti1':
        return True
    with connection.cursor() as cursor:
        return _equipo_usuario_asignado_id(cursor, fk_equipo) == request.session.get('usuario_id')

def _equipo_usuario_asignado_id(cursor, fk_equipo):
    """Busca qué usuario tiene asignado un equipo, comparando por pedazos del
    nombre (usuario_asignado es texto libre y no siempre viene en el mismo
    orden de nombre/apellidos)."""
    if not fk_equipo:
        return None
    cursor.execute("SELECT usuario_asignado FROM cat_equipos WHERE id_equipo = %s", [fk_equipo])
    row = cursor.fetchone()
    if not row or not row[0]:
        return None
    asignado = row[0].upper()
    cursor.execute("SELECT id_usuario, nombre, apellido_paterno, apellido_materno FROM usuarios WHERE estatus != 'INA'")
    for uid, nombre, paterno, materno in cursor.fetchall():
        partes = [p for p in (nombre, paterno, materno) if p]
        if partes and all(parte.upper() in asignado for parte in partes):
            return uid
    return None

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
        # Sin JOIN a usuarios: no se usa ninguna de sus columnas y, al ser
        # LEFT JOIN, tampoco filtraba filas. El resultado es idéntico.
        cursor.execute(f"""
            SELECT COUNT(*),
                   SUM(CASE WHEN t.estatus='PEN' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN t.estatus='PRO' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN t.estatus='FIN' THEN 1 ELSE 0 END)
            FROM ticket_usuario t
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
                   s.estatus_firmas, (t.fk_usuario = %s) AS es_propio,
                   s.es_mantenimiento, s.es_respaldo, s.es_cambio_pieza,
                   (SELECT id_respaldo FROM atencion_respaldo r WHERE r.fk_ticket = t.id_ticket
                      ORDER BY r.fecha_creacion DESC LIMIT 1) AS id_respaldo,
                   (SELECT id_cambio FROM atencion_cambio_pieza c WHERE c.fk_ticket = t.id_ticket
                      ORDER BY c.fecha_creacion DESC LIMIT 1) AS id_cambio
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
                'creador_nombre','creador_apellido','creador_materno','estatus_firmas','es_propio',
                'es_mantenimiento','es_respaldo','es_cambio_pieza','id_respaldo','id_cambio']
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
                   u.nombre, u.apellido_paterno, u.apellido_materno,
                   s.es_mantenimiento, s.es_respaldo, s.es_cambio_pieza
            FROM seguimiento_ticket s
            JOIN ticket_usuario t ON t.id_ticket = s.fk_ticket
            LEFT JOIN usuarios u ON u.id_usuario = t.fk_usuario
            WHERE s.fk_ticket = %s
        """, [id_ticket])
        row = cursor.fetchone()
        if not row:
            return JsonResponse({'error': 'No encontrado'}, status=404)
        data = {
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
            'es_mantenimiento':   bool(row[17]),
            'es_respaldo':        bool(row[18]),
            'es_cambio_pieza':    bool(row[19]),
        }
        if data['es_respaldo']:
            cursor.execute("""
                SELECT tipo_solicitud, tipo_fuente_datos, ruta_unidad_compartida, archivos_respaldar,
                       nombre_servidor, sistema_operativo, ip_servidor, tipo_backup,
                       observaciones_politica, total_gb_respaldar, total_gb_crecimiento,
                       periodicidad, nivel_backup, agenda, horario, retencion_dias
                FROM atencion_respaldo WHERE fk_ticket = %s ORDER BY fecha_creacion DESC LIMIT 1
            """, [id_ticket])
            r = cursor.fetchone()
            if r:
                cols = ['tipo_solicitud','tipo_fuente_datos','ruta_unidad_compartida','archivos_respaldar',
                        'nombre_servidor','sistema_operativo','ip_servidor','tipo_backup',
                        'observaciones_politica','total_gb_respaldar','total_gb_crecimiento',
                        'periodicidad','nivel_backup','agenda','horario','retencion_dias']
                data['respaldo'] = dict(zip(cols, r))
        if data['es_cambio_pieza']:
            cursor.execute("""
                SELECT id_cambio, fecha_cambio FROM atencion_cambio_pieza
                WHERE fk_ticket = %s AND origen = 'TICKET' ORDER BY fecha_creacion DESC LIMIT 1
            """, [id_ticket])
            c = cursor.fetchone()
            if c:
                id_cambio, fecha_cambio = c
                danadas, nuevas = _piezas_del_cambio(cursor, id_cambio)
                data['cambio_pieza'] = {
                    'fecha_cambio': fecha_cambio.strftime('%Y-%m-%d') if fecha_cambio else '',
                    'danadas': danadas, 'nuevas': nuevas,
                }
    return JsonResponse(data)

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
    if request.method != 'POST':
        return redirect('tickets_seguimiento')
    diagnostico     = request.POST.get('diagnostico', '').strip()
    solucion        = request.POST.get('solucion', '').strip()
    observacion     = request.POST.get('observacion', '').strip() or None
    tipo_ticket     = request.POST.get('tipo_ticket', '').strip()
    lugar_actividad = request.POST.get('lugar_actividad', '').strip()
    firma_ti_json   = request.POST.get('firma_ti_json', '').strip()
    es_mantenimiento = request.POST.get('es_mantenimiento') == '1'
    es_respaldo      = request.POST.get('es_respaldo') == '1'
    es_cambio_pieza  = request.POST.get('es_cambio_pieza') == '1'
    if not diagnostico or not solucion or not tipo_ticket or not lugar_actividad:
        messages.error(request, 'Diagnóstico, Solución, Tipo de Ticket y Lugar de Actividad son obligatorios.')
        return redirect('tickets_seguimiento')
    sets, vals = [
        'diagnostico=%s', 'solucion=%s', 'observacion=%s',
        'tipo_ticket=%s', 'lugar_actividad=%s',
        'es_mantenimiento=%s', 'es_respaldo=%s', 'es_cambio_pieza=%s',
    ], [diagnostico, solucion, observacion, tipo_ticket, lugar_actividad,
        es_mantenimiento, es_respaldo, es_cambio_pieza]
    for i in range(1, 5):
        f = request.FILES.get(f'imagen_{i}')
        if f:
            sets.append(f'imagen_{i}=%s')
            vals.append(f.read())
    with connection.cursor() as cursor:
        usuario_ti_id = request.session.get('usuario_id')
        firma_obj = None
        if firma_ti_json:
            firma_obj = _firma_obj(cursor, usuario_ti_id, firma_ti_json)
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
        cursor.execute("""
            SELECT fk_usuario, fk_equipo, fk_nombre_equipo, titulo FROM ticket_usuario WHERE id_ticket = %s
        """, [id_ticket])
        creador_id, fk_equipo, fk_nombre_equipo, titulo_ticket = cursor.fetchone()

        # --- Respaldo, si se marcó: solo crea el registro pendiente (una sola vez;
        # si ya existe no se duplica). El detalle se completa después desde el
        # acceso de Respaldos. Si se desmarca y el registro sigue vacío, se borra ---
        if es_respaldo:
            cursor.execute("SELECT id_respaldo FROM atencion_respaldo WHERE fk_ticket=%s", [id_ticket])
            if not cursor.fetchone():
                cursor.execute("""
                    INSERT INTO atencion_respaldo
                      (fk_ticket, fk_usuario_ti, fk_equipo, fk_nombre_equipo, tipo_solicitud,
                       estatus_firmas, fecha_creacion)
                    VALUES (%s, %s, %s, %s, %s, 'ABIERTO', %s)
                """, [id_ticket, usuario_ti_id, fk_equipo, fk_nombre_equipo, 'ESPORADICO', datetime.now()])
        else:
            cursor.execute("""
                DELETE FROM atencion_respaldo
                WHERE fk_ticket=%s AND ruta_unidad_compartida IS NULL AND archivos_respaldar IS NULL
            """, [id_ticket])

        # --- Cambio de Pieza, si se marcó: solo crea el registro pendiente (una
        # sola vez; si ya existe no se duplica); las piezas se completan después
        # desde el acceso de Cambios de Pieza. Si se desmarca y sigue vacío, se borra ---
        if es_cambio_pieza:
            cursor.execute("SELECT id_cambio FROM atencion_cambio_pieza WHERE fk_ticket=%s AND origen='TICKET'", [id_ticket])
            if not cursor.fetchone():
                _guardar_cambio_pieza(cursor, id_ticket, usuario_ti_id, fk_equipo, fk_nombre_equipo,
                                       date_type.today(), 'TICKET', piezas_danadas=[], piezas_nuevas=[],
                                       estatus_inicial='ABIERTO')
        else:
            cursor.execute("""
                DELETE FROM atencion_cambio_pieza
                WHERE fk_ticket=%s AND origen='TICKET'
                  AND id_cambio NOT IN (SELECT fk_cambio FROM atencion_cambio_pieza_detalle)
            """, [id_ticket])

        if firma_ti_json:
            _crear_notificacion(cursor, creador_id, id_ticket, 'FIRMA_PENDIENTE',
                                 f'T.I ya firmó el ticket #{id_ticket} — Falta tu firma de conformidad.')
    messages.success(request, f'Atención del ticket #{id_ticket} guardada correctamente.')
    return redirect('tickets_seguimiento')

@login_requerido
@nivel_requerido('modificar')
def firma_usuario_guardar(request, id_ticket):
    if request.method != 'POST':
        return redirect('tickets_seguimiento')
    with connection.cursor() as cursor:
        cursor.execute("SELECT fk_usuario, titulo FROM ticket_usuario WHERE id_ticket = %s", [id_ticket])
        row = cursor.fetchone()
        if not row or row[0] != request.session.get('usuario_id'):
            messages.error(request, 'No puedes firmar un ticket que no es tuyo.')
            return redirect('tickets_seguimiento')
        titulo_ticket = row[1]
        firma_usuario_json = request.POST.get('firma_usuario_json', '').strip()
        if not firma_usuario_json:
            messages.error(request, 'Debes dibujar tu firma para dar conformidad.')
            return redirect('tickets_seguimiento')
        usuario_id = request.session.get('usuario_id')
        firma_obj = _firma_obj(cursor, usuario_id, firma_usuario_json)
        firma_json = json_lib.dumps(firma_obj)
        cursor.execute("""
            UPDATE seguimiento_ticket
            SET firma_usuario=%s, estatus_firmas='FIN', fecha_cierre_atencion=%s
            WHERE fk_ticket=%s AND estatus_firmas='PTI'
            RETURNING fk_usuario_ti, es_mantenimiento, es_respaldo, es_cambio_pieza,
                      diagnostico, solucion, lugar_actividad
        """, [firma_json, datetime.now(), id_ticket])
        resultado = cursor.fetchone()
        if not resultado:
            messages.error(request, 'No se pudo firmar (el ticket no está listo para tu firma).')
            return redirect('tickets_seguimiento')
        (fk_usuario_ti, es_mantenimiento, es_respaldo, es_cambio_pieza,
         diagnostico, solucion, lugar_actividad) = resultado
        cursor.execute("UPDATE ticket_usuario SET estatus='FIN' WHERE id_ticket=%s", [id_ticket])

        # las tablas hermanas (si aplican) también cierran con la misma firma del usuario
        cursor.execute("""
            UPDATE atencion_respaldo SET firma_usuario=%s, estatus_firmas='FIN', fecha_cierre=%s
            WHERE fk_ticket=%s AND estatus_firmas='PTI'
        """, [firma_json, datetime.now(), id_ticket])
        cursor.execute("""
            UPDATE atencion_cambio_pieza SET firma_usuario=%s, estatus='FIN', fecha_cierre=%s
            WHERE fk_ticket=%s AND estatus='PTI'
        """, [firma_json, datetime.now(), id_ticket])

        hoy = date_type.today()
        if es_mantenimiento:
            _registrar_actividad(cursor, f'Mantenimiento — Ticket #{id_ticket}', diagnostico, hoy,
                                  solucion, fk_usuario_ti, 'MANTENIMIENTO', id_ticket)
        if es_respaldo:
            _registrar_actividad(cursor, f'Respaldo — Ticket #{id_ticket}', titulo_ticket, hoy,
                                  None, fk_usuario_ti, 'RESPALDO', id_ticket)
        if es_cambio_pieza:
            _registrar_actividad(cursor, f'Cambio de Pieza — Ticket #{id_ticket}', titulo_ticket, hoy,
                                  None, fk_usuario_ti, 'CAMBIO_PIEZA', id_ticket)
        if not (es_mantenimiento or es_respaldo or es_cambio_pieza):
            _registrar_actividad(cursor, titulo_ticket or f'Ticket #{id_ticket}', diagnostico, hoy,
                                  solucion, fk_usuario_ti, 'TICKET', id_ticket)

        _crear_notificacion(cursor, usuario_id, id_ticket, 'FINALIZADO',
                             f'Tu ticket #{id_ticket} quedó cerrado y firmado.')
        if fk_usuario_ti:
            _crear_notificacion(cursor, fk_usuario_ti, id_ticket, 'FINALIZADO',
                                 f'El usuario firmó de conformidad el ticket #{id_ticket}. Quedó cerrado.')
    messages.success(request, f'Firmaste de conformidad el ticket #{id_ticket}.')
    return redirect('tickets_seguimiento')

# --- Servicios independientes (sin ticket): Mantenimiento / Respaldo / Cambio de Pieza ---
# Cada uno se crea desde su propio modal, dentro de su propio acceso/lista
# (mantenimientos_lista, respaldos_lista, cambios_pieza_lista) — no hay una
# página "Registrar Servicio" separada.

def _equipos_activos(cursor):
    cursor.execute("SELECT id_equipo, nombre_equipo FROM cat_equipos WHERE estatus != 'INA' ORDER BY nombre_equipo")
    return [{'id_equipo': r[0], 'nombre_equipo': r[1]} for r in cursor.fetchall()]

def _usuarios_activos(cursor):
    """Para elegir al solicitante de un respaldo independiente. El formato
    F-CECSA-TI-03 pide cargo, area, extension y correo, y eso solo se obtiene
    ligando a un usuario real: el 'usuario asignado' del equipo es texto libre."""
    cursor.execute("""
        SELECT u.id_usuario, u.nombre, u.apellido_paterno, u.apellido_materno, p.nombre_puesto
        FROM usuarios u
        LEFT JOIN cat_puestos p ON p.id_puesto = u.fk_puesto
        WHERE u.estatus = 'ACT'
        ORDER BY u.nombre, u.apellido_paterno
    """)
    return [{
        'id_usuario': r[0],
        'nombre_completo': ' '.join(x for x in r[1:4] if x),
        'puesto': r[4] or '',
    } for r in cursor.fetchall()]

# Correo corporativo que sale impreso en el formato F-CECSA-TI-03. Es fijo
# por decision del area: no se toma del usuario. Para cambiarlo, es aqui.
CORREO_CORPORATIVO_TI = 'auxiliar.ti@cecsa.mx'

def _sin_no_aplica(valor):
    """Los campos opcionales del formato se dejan en blanco, no con un 'N/A'
    escrito: en el papel una casilla vacia ya significa que no aplica."""
    if not valor:
        return ''
    return '' if valor.strip().upper().replace('.', '') in ('NA', 'N/A') else valor

def _datos_solicitante(cursor, fk_ticket, fk_usuario_solicitante):
    """Bloque SOLICITANTE del formato. Si el respaldo nace de un ticket manda
    quien lo levanto; si es independiente, el usuario elegido en el modal."""
    id_usuario = None
    area_ticket = None
    if fk_ticket:
        cursor.execute("SELECT fk_usuario, area FROM ticket_usuario WHERE id_ticket = %s", [fk_ticket])
        fila = cursor.fetchone()
        if fila:
            id_usuario, area_ticket = fila
    if not id_usuario:
        id_usuario = fk_usuario_solicitante
    if not id_usuario:
        return {}
    cursor.execute("""
        SELECT u.nombre, u.apellido_paterno, u.apellido_materno, u.email, u.telefono,
               u.area, p.nombre_puesto
        FROM usuarios u
        LEFT JOIN cat_puestos p ON p.id_puesto = u.fk_puesto
        WHERE u.id_usuario = %s
    """, [id_usuario])
    fila = cursor.fetchone()
    if not fila:
        return {}
    return {
        'nombre': ' '.join(x for x in fila[0:3] if x),
        'correo': CORREO_CORPORATIVO_TI,
        'extension': fila[4] or '',
        'lugar': area_ticket or fila[5] or '',
        'cargo': fila[6] or '',
    }

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('crear')
def servicio_mantenimiento_guardar(request):
    if request.method != 'POST':
        return redirect('mantenimientos_lista')
    fk_equipo        = request.POST.get('fk_equipo', '').strip() or None
    fk_nombre_equipo = request.POST.get('fk_nombre_equipo', '').strip() or None
    lugar_actividad  = request.POST.get('lugar_actividad', '').strip() or None
    descripcion      = request.POST.get('descripcion', '').strip()
    observaciones    = request.POST.get('observaciones', '').strip() or None
    firma_ti_json    = request.POST.get('firma_ti_json', '').strip()
    hallazgo         = request.POST.get('hallazgo_pieza_pendiente', '').strip() or None
    if not descripcion or not firma_ti_json:
        messages.error(request, 'La descripción y tu firma son obligatorias.')
        return redirect('mantenimientos_lista')
    usuario_ti_id = request.session.get('usuario_id')
    with connection.cursor() as cursor:
        firma_obj = _firma_obj(cursor, usuario_ti_id, firma_ti_json)
        destinatario = _equipo_usuario_asignado_id(cursor, fk_equipo)
        estatus = 'PTI' if destinatario else 'FIN'
        fk_cambio_generado = None
        if hallazgo:
            fk_cambio_generado = _guardar_cambio_pieza(
                cursor, None, usuario_ti_id, fk_equipo, fk_nombre_equipo, date_type.today(),
                'HALLAZGO_MANTENIMIENTO',
                piezas_danadas=[{
                    'descripcion': request.POST.get('hallazgo_descripcion', ''),
                    'marca': request.POST.get('hallazgo_marca', ''),
                    'modelo': request.POST.get('hallazgo_modelo', ''),
                    'numero_serie': request.POST.get('hallazgo_serie', ''),
                }], piezas_nuevas=[], estatus_inicial='ABIERTO')
        cursor.execute("""
            INSERT INTO atencion_mantenimiento_independiente
              (fk_usuario_ti, fk_equipo, fk_nombre_equipo, lugar_actividad, descripcion, observaciones,
               hallazgo_pieza_pendiente, fk_cambio_generado, firma_ti, estatus_firmas,
               fecha_creacion, fecha_cierre)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id_mantenimiento
        """, [usuario_ti_id, fk_equipo, fk_nombre_equipo, lugar_actividad, descripcion, observaciones,
              hallazgo, fk_cambio_generado, json_lib.dumps(firma_obj), estatus, datetime.now(),
              datetime.now() if estatus == 'FIN' else None])
        id_mant = cursor.fetchone()[0]
        _registrar_actividad(cursor, f'Mantenimiento independiente — {fk_nombre_equipo or "Equipo"}',
                              descripcion, date_type.today(), observaciones, usuario_ti_id,
                              'MANTENIMIENTO', id_mant)
        if destinatario:
            _crear_notificacion(cursor, destinatario, id_mant, 'SERVICIO_PENDIENTE',
                                 f'Se registró un Mantenimiento a tu equipo {fk_nombre_equipo or ""} — pendiente tu firma.')
    messages.success(request, 'Mantenimiento registrado correctamente.')
    return redirect('mantenimientos_lista')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('crear')
def servicio_respaldo_guardar(request):
    if request.method != 'POST':
        return redirect('respaldos_lista')
    fk_equipo        = request.POST.get('fk_equipo', '').strip() or None
    fk_nombre_equipo = request.POST.get('fk_nombre_equipo', '').strip() or None
    tipo_solicitud   = request.POST.get('tipo_solicitud', '').strip()
    firma_ti_json    = request.POST.get('firma_ti_json', '').strip()
    ruta             = request.POST.get('ruta_unidad_compartida', '').strip()
    archivos         = request.POST.get('archivos_respaldar', '').strip()
    sistema_operativo = request.POST.get('sistema_operativo', '').strip()
    tipo_backup      = request.POST.get('tipo_backup', '').strip()
    total_gb         = request.POST.get('total_gb_respaldar', '').strip()
    periodicidad     = request.POST.get('periodicidad', '').strip()
    nivel_backup     = request.POST.get('nivel_backup', '').strip()
    agenda           = request.POST.get('agenda', '').strip()
    horario          = request.POST.get('horario', '').strip()
    if not all([fk_equipo, tipo_solicitud, firma_ti_json, ruta, archivos, sistema_operativo,
                tipo_backup, total_gb, periodicidad, nivel_backup, agenda, horario]):
        messages.error(request, 'Faltan campos obligatorios para registrar el respaldo.')
        return redirect('respaldos_lista')
    usuario_ti_id = request.session.get('usuario_id')
    with connection.cursor() as cursor:
        firma_obj = _firma_obj(cursor, usuario_ti_id, firma_ti_json)
        destinatario = _equipo_usuario_asignado_id(cursor, fk_equipo)
        estatus = 'PTI' if destinatario else 'FIN'
        cursor.execute("""
            INSERT INTO atencion_respaldo
              (fk_ticket, fk_usuario_ti, fk_equipo, fk_nombre_equipo, tipo_solicitud,
               tipo_fuente_datos, ruta_unidad_compartida, archivos_respaldar,
               nombre_servidor, sistema_operativo, ip_servidor, tipo_backup,
               observaciones_politica, total_gb_respaldar, total_gb_crecimiento,
               periodicidad, nivel_backup, agenda, horario, retencion_dias,
               firma_ti, estatus_firmas, fecha_creacion, fecha_cierre,
               fk_usuario_solicitante)
            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id_respaldo
        """, [
            usuario_ti_id, fk_equipo, fk_nombre_equipo, tipo_solicitud,
            request.POST.get('tipo_fuente_datos', '').strip() or None,
            request.POST.get('ruta_unidad_compartida', '').strip() or None,
            request.POST.get('archivos_respaldar', '').strip() or None,
            fk_nombre_equipo,
            request.POST.get('sistema_operativo', '').strip() or None,
            request.POST.get('ip_servidor', '').strip() or None,
            request.POST.get('tipo_backup', '').strip() or None,
            request.POST.get('observaciones_politica', '').strip() or None,
            request.POST.get('total_gb_respaldar', '').strip() or None,
            request.POST.get('total_gb_crecimiento', '').strip() or None,
            request.POST.get('periodicidad', '').strip() or None,
            request.POST.get('nivel_backup', '').strip() or None,
            request.POST.get('agenda', '').strip() or None,
            request.POST.get('horario', '').strip() or None,
            request.POST.get('retencion_dias', '').strip() or None,
            json_lib.dumps(firma_obj), estatus, datetime.now(),
            datetime.now() if estatus == 'FIN' else None,
            request.POST.get('fk_usuario_solicitante', '').strip() or None,
        ])
        id_resp = cursor.fetchone()[0]
        _registrar_actividad(cursor, f'Respaldo independiente — {fk_nombre_equipo or "Equipo"}',
                              request.POST.get('tipo_fuente_datos', ''), date_type.today(), None,
                              usuario_ti_id, 'RESPALDO', id_resp)
        if destinatario:
            _crear_notificacion(cursor, destinatario, id_resp, 'SERVICIO_PENDIENTE',
                                 f'Se registró un Respaldo a tu equipo {fk_nombre_equipo or ""} — pendiente tu firma.')
    messages.success(request, 'Respaldo registrado correctamente.')
    return redirect('respaldos_lista')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('crear')
def servicio_cambio_pieza_guardar(request):
    if request.method != 'POST':
        return redirect('cambios_pieza_lista')
    fk_equipo         = request.POST.get('fk_equipo', '').strip() or None
    fk_nombre_equipo  = request.POST.get('fk_nombre_equipo', '').strip() or None
    fecha_cambio_str  = request.POST.get('fecha_cambio', '').strip()
    firma_ti_json     = request.POST.get('firma_ti_json', '').strip()
    try:
        piezas_danadas = json_lib.loads(request.POST.get('piezas_danadas_json', '[]'))
        piezas_nuevas  = json_lib.loads(request.POST.get('piezas_nuevas_json', '[]'))
    except (json_lib.JSONDecodeError, TypeError):
        piezas_danadas, piezas_nuevas = [], []
    if not fecha_cambio_str or not firma_ti_json or (not piezas_danadas and not piezas_nuevas):
        messages.error(request, 'La fecha, tu firma y al menos una pieza son obligatorias.')
        return redirect('cambios_pieza_lista')
    fecha_cambio = datetime.strptime(fecha_cambio_str, '%Y-%m-%d').date()
    usuario_ti_id = request.session.get('usuario_id')
    with connection.cursor() as cursor:
        firma_obj = _firma_obj(cursor, usuario_ti_id, firma_ti_json)
        destinatario = _equipo_usuario_asignado_id(cursor, fk_equipo)
        estatus = 'PTI' if destinatario else 'FIN'
        id_cambio = _guardar_cambio_pieza(cursor, None, usuario_ti_id, fk_equipo, fk_nombre_equipo,
                                           fecha_cambio, 'INDEPENDIENTE', piezas_danadas, piezas_nuevas,
                                           firma_obj, estatus_inicial=estatus)
        if estatus == 'FIN':
            cursor.execute("UPDATE atencion_cambio_pieza SET fecha_cierre=%s WHERE id_cambio=%s",
                           [datetime.now(), id_cambio])
        _registrar_actividad(cursor, f'Cambio de Pieza independiente — {fk_nombre_equipo or "Equipo"}',
                              None, fecha_cambio, None, usuario_ti_id, 'CAMBIO_PIEZA', id_cambio)
        if destinatario:
            _crear_notificacion(cursor, destinatario, id_cambio, 'SERVICIO_PENDIENTE',
                                 f'Se registró un Cambio de Pieza a tu equipo {fk_nombre_equipo or ""} — pendiente tu firma.')
    messages.success(request, 'Cambio de Pieza registrado correctamente.')
    return redirect('cambios_pieza_lista')

# --- Historial de mi Equipo de Cómputo (Usuario) ---

_TABLA_SERVICIO = {
    'mantenimiento': ('atencion_mantenimiento_independiente', 'id_mantenimiento', 'estatus_firmas'),
    'respaldo':       ('atencion_respaldo', 'id_respaldo', 'estatus_firmas'),
    'cambio_pieza':   ('atencion_cambio_pieza', 'id_cambio', 'estatus'),
}
_ETIQUETA_SERVICIO = {'mantenimiento': 'Mantenimiento', 'respaldo': 'Respaldo', 'cambio_pieza': 'Cambio de Pieza'}

@login_requerido
@nivel_requerido('consultar')
def historial_mi_equipo(request):
    usuario_id = request.session.get('usuario_id')
    registros = []
    with connection.cursor() as cursor:
        equipos = _equipos_de_usuario(cursor, usuario_id)
        ids_equipo = [e[0] for e in equipos]
        if ids_equipo:
            cursor.execute("""
                SELECT fk_ticket, fk_nombre_equipo, diagnostico, estatus_firmas, fecha_creacion_atencion
                FROM seguimiento_ticket WHERE es_mantenimiento = TRUE AND fk_equipo = ANY(%s)
            """, [ids_equipo])
            for fk_ticket, equipo, resumen, estatus, fecha in cursor.fetchall():
                registros.append({'tipo': 'mantenimiento', 'id': fk_ticket, 'equipo': equipo, 'resumen': resumen,
                                   'estatus': estatus, 'fecha': fecha, 'firmable_aqui': False, 'fk_ticket': fk_ticket})
            cursor.execute("""
                SELECT id_mantenimiento, fk_nombre_equipo, descripcion, estatus_firmas, fecha_creacion
                FROM atencion_mantenimiento_independiente WHERE fk_equipo = ANY(%s)
            """, [ids_equipo])
            for id_m, equipo, resumen, estatus, fecha in cursor.fetchall():
                registros.append({'tipo': 'mantenimiento', 'id': id_m, 'equipo': equipo, 'resumen': resumen,
                                   'estatus': estatus, 'fecha': fecha, 'firmable_aqui': True, 'fk_ticket': None})
            cursor.execute("""
                SELECT id_respaldo, fk_nombre_equipo, tipo_fuente_datos, estatus_firmas, fecha_creacion, fk_ticket
                FROM atencion_respaldo WHERE fk_equipo = ANY(%s)
            """, [ids_equipo])
            for id_r, equipo, resumen, estatus, fecha, fk_ticket in cursor.fetchall():
                registros.append({'tipo': 'respaldo', 'id': id_r, 'equipo': equipo,
                                   'resumen': resumen or 'Respaldo de información', 'estatus': estatus,
                                   'fecha': fecha, 'firmable_aqui': fk_ticket is None, 'fk_ticket': fk_ticket})
            cursor.execute("""
                SELECT id_cambio, fk_nombre_equipo, estatus, fecha_creacion, fk_ticket
                FROM atencion_cambio_pieza WHERE fk_equipo = ANY(%s) AND estatus != 'ABIERTO'
            """, [ids_equipo])
            for id_c, equipo, estatus, fecha, fk_ticket in cursor.fetchall():
                registros.append({'tipo': 'cambio_pieza', 'id': id_c, 'equipo': equipo, 'resumen': 'Cambio de pieza',
                                   'estatus': estatus, 'fecha': fecha, 'firmable_aqui': fk_ticket is None,
                                   'fk_ticket': fk_ticket})
        registros.sort(key=lambda r: r['fecha'] or datetime.min, reverse=True)
    ctx = {
        'registros': registros,
        'sin_equipos': not ids_equipo if 'ids_equipo' in locals() else True,
        'rol_id': request.session.get('usuario_rol_id', ''),
        'username': request.session.get('usuario_username', ''),
        'nombre': request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'servicios/historial_equipo.html', ctx)

@login_requerido
@nivel_requerido('consultar')
def historial_mi_equipo_datos(request, tipo, id_registro):
    if tipo not in _TABLA_SERVICIO:
        return JsonResponse({'error': 'Tipo inválido'}, status=404)
    tabla, campo_id, campo_estatus = _TABLA_SERVICIO[tipo]
    with connection.cursor() as cursor:
        if tipo == 'mantenimiento':
            cursor.execute(f"""
                SELECT descripcion, observaciones, hallazgo_pieza_pendiente, fk_nombre_equipo, {campo_estatus}
                FROM {tabla} WHERE {campo_id} = %s
            """, [id_registro])
            row = cursor.fetchone()
            if not row:
                return JsonResponse({'error': 'No encontrado'}, status=404)
            data = {'descripcion': row[0], 'observaciones': row[1], 'hallazgo': row[2],
                    'equipo': row[3], 'estatus': row[4]}
        elif tipo == 'respaldo':
            cursor.execute(f"""
                SELECT tipo_solicitud, tipo_fuente_datos, archivos_respaldar, fk_nombre_equipo, {campo_estatus}
                FROM {tabla} WHERE {campo_id} = %s
            """, [id_registro])
            row = cursor.fetchone()
            if not row:
                return JsonResponse({'error': 'No encontrado'}, status=404)
            data = {'tipo_solicitud': row[0], 'tipo_fuente_datos': row[1], 'archivos': row[2],
                    'equipo': row[3], 'estatus': row[4]}
        else:
            cursor.execute(f"SELECT fk_nombre_equipo, {campo_estatus} FROM {tabla} WHERE {campo_id} = %s", [id_registro])
            row = cursor.fetchone()
            if not row:
                return JsonResponse({'error': 'No encontrado'}, status=404)
            danadas, nuevas = _piezas_del_cambio(cursor, id_registro)
            data = {'equipo': row[0], 'estatus': row[1], 'danadas': danadas, 'nuevas': nuevas}
    return JsonResponse(data)

@login_requerido
@nivel_requerido('modificar')
def historial_mi_equipo_firmar(request, tipo, id_registro):
    if request.method != 'POST':
        return redirect('historial_mi_equipo')
    if tipo not in _TABLA_SERVICIO:
        messages.error(request, 'Tipo inválido.')
        return redirect('historial_mi_equipo')
    firma_json = request.POST.get('firma_usuario_json', '').strip()
    if not firma_json:
        messages.error(request, 'Debes dibujar tu firma para dar conformidad.')
        return redirect('historial_mi_equipo')
    usuario_id = request.session.get('usuario_id')
    tabla, campo_id, campo_estatus = _TABLA_SERVICIO[tipo]
    filtro_ticket = '' if tipo == 'mantenimiento' else 'AND fk_ticket IS NULL'
    with connection.cursor() as cursor:
        # el registro debe pertenecer a un equipo que sí está asignado a este usuario
        cursor.execute(f"SELECT fk_equipo FROM {tabla} WHERE {campo_id} = %s {filtro_ticket}", [id_registro])
        fila = cursor.fetchone()
        if not fila or _equipo_usuario_asignado_id(cursor, fila[0]) != usuario_id:
            messages.error(request, 'No puedes firmar un registro que no es de tu equipo.')
            return redirect('historial_mi_equipo')
        firma_obj = _firma_obj(cursor, usuario_id, firma_json)
        cursor.execute(f"""
            UPDATE {tabla} SET firma_usuario=%s, {campo_estatus}='FIN', fecha_cierre=%s
            WHERE {campo_id}=%s AND {campo_estatus}='PTI' {filtro_ticket}
            RETURNING fk_usuario_ti, fk_nombre_equipo
        """, [json_lib.dumps(firma_obj), datetime.now(), id_registro])
        row = cursor.fetchone()
        if not row:
            messages.error(request, 'No se pudo firmar (no está listo para tu firma).')
            return redirect('historial_mi_equipo')
        fk_usuario_ti, nombre_equipo = row
        etiqueta = _ETIQUETA_SERVICIO[tipo]
        _registrar_actividad(cursor, f'{etiqueta} — {nombre_equipo or "Equipo"} (conformidad)', None,
                              date_type.today(), None, fk_usuario_ti, tipo.upper(), id_registro)
        if fk_usuario_ti:
            _crear_notificacion(cursor, fk_usuario_ti, id_registro, 'FINALIZADO',
                                 f'El usuario firmó de conformidad el {etiqueta} de {nombre_equipo or "su equipo"}.')
    messages.success(request, 'Firmaste de conformidad correctamente.')
    return redirect('historial_mi_equipo')

# --- Refaccionamientos Pendientes (T.I): cambios de pieza abiertos desde un hallazgo ---

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def mantenimientos_lista(request):
    """Todos los mantenimientos de todos los equipos: los que nacieron de un
    ticket (seguimiento_ticket.es_mantenimiento) y los independientes."""
    registros = []
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT s.fk_ticket, s.fk_nombre_equipo, s.diagnostico, s.estatus_firmas, s.fecha_creacion_atencion,
                   ti.nombre, ti.apellido_paterno, ti.apellido_materno
            FROM seguimiento_ticket s
            LEFT JOIN usuarios ti ON ti.id_usuario = s.fk_usuario_ti
            WHERE s.es_mantenimiento = TRUE
        """)
        for fk_ticket, equipo, resumen, estatus, fecha, *tec in cursor.fetchall():
            registros.append({'origen': 'ticket', 'id': fk_ticket, 'equipo': equipo, 'resumen': resumen,
                               'estatus': estatus, 'fecha': fecha, 'tecnico': ' '.join(p for p in tec if p)})
        cursor.execute("""
            SELECT m.id_mantenimiento, m.fk_nombre_equipo, m.descripcion, m.estatus_firmas, m.fecha_creacion,
                   ti.nombre, ti.apellido_paterno, ti.apellido_materno
            FROM atencion_mantenimiento_independiente m
            LEFT JOIN usuarios ti ON ti.id_usuario = m.fk_usuario_ti
        """)
        for id_m, equipo, resumen, estatus, fecha, *tec in cursor.fetchall():
            registros.append({'origen': 'independiente', 'id': id_m, 'equipo': equipo, 'resumen': resumen,
                               'estatus': estatus, 'fecha': fecha, 'tecnico': ' '.join(p for p in tec if p)})
        equipos = _equipos_activos(cursor)
    registros.sort(key=lambda r: r['fecha'] or datetime.min, reverse=True)
    ctx = {
        'registros': registros,
        'equipos': equipos,
        'rol_id': request.session.get('usuario_rol_id', ''),
        'username': request.session.get('usuario_username', ''),
        'nombre': request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'servicios/mantenimientos.html', ctx)

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def mantenimiento_consultar_datos(request, origen, id_registro):
    with connection.cursor() as cursor:
        if origen == 'ticket':
            cursor.execute("""
                SELECT s.fk_nombre_equipo, s.lugar_actividad, s.diagnostico, s.solucion, s.observacion,
                       ti.nombre, ti.apellido_paterno, ti.apellido_materno
                FROM seguimiento_ticket s
                LEFT JOIN usuarios ti ON ti.id_usuario = s.fk_usuario_ti
                WHERE s.fk_ticket = %s
            """, [id_registro])
            row = cursor.fetchone()
            if not row:
                return JsonResponse({'error': 'No encontrado'}, status=404)
            return JsonResponse({
                'equipo': row[0], 'lugar_actividad': row[1],
                'diagnostico': row[2], 'solucion': row[3], 'observacion': row[4],
                'tecnico': ' '.join(p for p in row[5:8] if p),
            })
        cursor.execute("""
            SELECT m.fk_nombre_equipo, m.lugar_actividad, m.descripcion, m.observaciones,
                   m.hallazgo_pieza_pendiente, ti.nombre, ti.apellido_paterno, ti.apellido_materno
            FROM atencion_mantenimiento_independiente m
            LEFT JOIN usuarios ti ON ti.id_usuario = m.fk_usuario_ti
            WHERE m.id_mantenimiento = %s
        """, [id_registro])
        row = cursor.fetchone()
        if not row:
            return JsonResponse({'error': 'No encontrado'}, status=404)
        return JsonResponse({
            'equipo': row[0], 'lugar_actividad': row[1],
            'descripcion': row[2], 'observaciones': row[3], 'hallazgo': row[4],
            'tecnico': ' '.join(p for p in row[5:8] if p),
        })

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def respaldos_lista(request):
    """Todos los respaldos de todos los equipos (de ticket o independientes:
    ambos viven en la misma tabla, fk_ticket queda NULL en los independientes)."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT r.id_respaldo, r.fk_ticket, r.fk_nombre_equipo, r.tipo_solicitud,
                   r.tipo_fuente_datos, r.estatus_firmas, r.fecha_creacion,
                   ti.nombre, ti.apellido_paterno, ti.apellido_materno
            FROM atencion_respaldo r
            LEFT JOIN usuarios ti ON ti.id_usuario = r.fk_usuario_ti
            ORDER BY r.fecha_creacion DESC
        """)
        registros = [{
            'id': r[0], 'fk_ticket': r[1], 'equipo': r[2], 'tipo_solicitud': r[3],
            'resumen': r[4] or 'Respaldo de información', 'estatus': r[5], 'fecha': r[6],
            'tecnico': ' '.join(p for p in r[7:10] if p),
        } for r in cursor.fetchall()]
        equipos = _equipos_activos(cursor)
        usuarios = _usuarios_activos(cursor)
    ctx = {
        'registros': registros,
        'equipos': equipos,
        'usuarios': usuarios,
        'rol_id': request.session.get('usuario_rol_id', ''),
        'username': request.session.get('usuario_username', ''),
        'nombre': request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'servicios/respaldos.html', ctx)

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def cambios_pieza_lista(request):
    """Todos los cambios de pieza de todos los equipos — incluye los que
    quedaron ABIERTOS por un hallazgo de mantenimiento, pendientes de refacción."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT c.id_cambio, c.fk_ticket, c.fk_nombre_equipo, c.origen, c.estatus, c.fecha_creacion,
                   ti.nombre, ti.apellido_paterno, ti.apellido_materno
            FROM atencion_cambio_pieza c
            LEFT JOIN usuarios ti ON ti.id_usuario = c.fk_usuario_ti
            ORDER BY c.fecha_creacion DESC
        """)
        registros = [{
            'id': r[0], 'fk_ticket': r[1], 'equipo': r[2], 'origen': r[3], 'estatus': r[4], 'fecha': r[5],
            'tecnico': ' '.join(p for p in r[6:9] if p),
        } for r in cursor.fetchall()]
        equipos = _equipos_activos(cursor)
    ctx = {
        'registros': registros,
        'equipos': equipos,
        'rol_id': request.session.get('usuario_rol_id', ''),
        'username': request.session.get('usuario_username', ''),
        'nombre': request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'servicios/cambios_pieza.html', ctx)

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def respaldo_datos(request, id_respaldo):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT fk_ticket, fk_nombre_equipo, estatus_firmas, tipo_solicitud, tipo_fuente_datos,
                   ruta_unidad_compartida, archivos_respaldar, nombre_servidor, sistema_operativo,
                   ip_servidor, tipo_backup, observaciones_politica, total_gb_respaldar,
                   total_gb_crecimiento, periodicidad, nivel_backup, agenda, horario, retencion_dias
            FROM atencion_respaldo WHERE id_respaldo = %s
        """, [id_respaldo])
        row = cursor.fetchone()
        if not row:
            return JsonResponse({'error': 'No encontrado'}, status=404)
    return JsonResponse({
        'fk_ticket': row[0], 'equipo': row[1], 'estatus': row[2],
        'tipo_solicitud': row[3], 'tipo_fuente_datos': row[4], 'ruta_unidad_compartida': row[5],
        'archivos_respaldar': row[6], 'nombre_servidor': row[7], 'sistema_operativo': row[8],
        'ip_servidor': row[9], 'tipo_backup': row[10], 'observaciones_politica': row[11],
        'total_gb_respaldar': row[12], 'total_gb_crecimiento': row[13], 'periodicidad': row[14],
        'nivel_backup': row[15], 'agenda': row[16], 'horario': row[17], 'retencion_dias': row[18],
    })

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('modificar')
def respaldo_completar(request, id_respaldo):
    if request.method != 'POST':
        return redirect('respaldos_lista')
    tipo_solicitud   = request.POST.get('tipo_solicitud', '').strip()
    firma_ti_json    = request.POST.get('firma_ti_json', '').strip()
    ruta             = request.POST.get('ruta_unidad_compartida', '').strip()
    archivos         = request.POST.get('archivos_respaldar', '').strip()
    nombre_servidor  = request.POST.get('nombre_servidor', '').strip()
    sistema_operativo = request.POST.get('sistema_operativo', '').strip()
    tipo_backup      = request.POST.get('tipo_backup', '').strip()
    total_gb         = request.POST.get('total_gb_respaldar', '').strip()
    horario          = request.POST.get('horario', '').strip()
    periodicidad     = request.POST.get('periodicidad', '').strip()
    nivel_backup     = request.POST.get('nivel_backup', '').strip()
    agenda           = request.POST.get('agenda', '').strip()
    if not all([tipo_solicitud, ruta, archivos, nombre_servidor,
                sistema_operativo, tipo_backup, total_gb, horario,
                periodicidad, nivel_backup, agenda]):
        messages.error(request, 'Faltan campos obligatorios para completar el respaldo.')
        return redirect('respaldos_lista')
    usuario_ti_id = request.session.get('usuario_id')
    with connection.cursor() as cursor:
        cursor.execute("SELECT fk_equipo, fk_nombre_equipo, estatus_firmas, fk_ticket FROM atencion_respaldo WHERE id_respaldo = %s", [id_respaldo])
        row = cursor.fetchone()
        if not row or row[2] != 'ABIERTO':
            messages.error(request, 'Este respaldo ya no está pendiente.')
            return redirect('respaldos_lista')
        fk_equipo, fk_nombre_equipo, _, fk_ticket = row
        destinatario = None
        if firma_ti_json:
            firma_obj = _firma_obj(cursor, usuario_ti_id, firma_ti_json)
            destinatario = _equipo_usuario_asignado_id(cursor, fk_equipo)
            estatus = 'PTI' if destinatario else 'FIN'
            firma_val = json_lib.dumps(firma_obj)
            fecha_cierre_val = datetime.now() if estatus == 'FIN' else None
        else:
            estatus = 'ABIERTO'
            firma_val = None
            fecha_cierre_val = None
        cursor.execute("""
            UPDATE atencion_respaldo SET
              tipo_solicitud=%s, tipo_fuente_datos=%s, ruta_unidad_compartida=%s, archivos_respaldar=%s,
              nombre_servidor=%s, sistema_operativo=%s, ip_servidor=%s, tipo_backup=%s,
              observaciones_politica=%s, total_gb_respaldar=%s, total_gb_crecimiento=%s,
              periodicidad=%s, nivel_backup=%s, agenda=%s, horario=%s, retencion_dias=%s,
              firma_ti=%s, estatus_firmas=%s, fecha_cierre=%s
            WHERE id_respaldo=%s
        """, [
            tipo_solicitud,
            request.POST.get('tipo_fuente_datos', '').strip() or None,
            request.POST.get('ruta_unidad_compartida', '').strip() or None,
            request.POST.get('archivos_respaldar', '').strip() or None,
            request.POST.get('nombre_servidor', '').strip() or None,
            request.POST.get('sistema_operativo', '').strip() or None,
            request.POST.get('ip_servidor', '').strip() or None,
            request.POST.get('tipo_backup', '').strip() or None,
            request.POST.get('observaciones_politica', '').strip() or None,
            request.POST.get('total_gb_respaldar', '').strip() or None,
            request.POST.get('total_gb_crecimiento', '').strip() or None,
            request.POST.get('periodicidad', '').strip() or None,
            request.POST.get('nivel_backup', '').strip() or None,
            request.POST.get('agenda', '').strip() or None,
            request.POST.get('horario', '').strip() or None,
            request.POST.get('retencion_dias', '').strip() or None,
            firma_val, estatus, fecha_cierre_val,
            id_respaldo,
        ])
        if firma_ti_json:
            _registrar_actividad(cursor, f'Respaldo completado — {fk_nombre_equipo or "Equipo"}',
                                  request.POST.get('tipo_fuente_datos', ''), date_type.today(), None,
                                  usuario_ti_id, 'RESPALDO', id_respaldo)
            if destinatario and fk_ticket is None:
                _crear_notificacion(cursor, destinatario, id_respaldo, 'SERVICIO_PENDIENTE',
                                     f'Se completó el Respaldo pendiente de tu equipo {fk_nombre_equipo or ""} — pendiente tu firma.')
            messages.success(request, 'Respaldo completado correctamente.')
        else:
            messages.success(request, 'Respaldo guardado. Puedes firmar más tarde para completarlo.')
    return redirect('respaldos_lista')

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def refaccionamiento_datos(request, id_cambio):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT fk_ticket, fk_nombre_equipo, fecha_cambio, estatus FROM atencion_cambio_pieza WHERE id_cambio = %s
        """, [id_cambio])
        row = cursor.fetchone()
        if not row:
            return JsonResponse({'error': 'No encontrado'}, status=404)
        danadas, nuevas = _piezas_del_cambio(cursor, id_cambio)
    return JsonResponse({
        'fk_ticket': row[0], 'equipo': row[1],
        'fecha_cambio': row[2].strftime('%Y-%m-%d') if row[2] else '',
        'estatus': row[3], 'danadas': danadas, 'nuevas': nuevas,
    })

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('modificar')
def refaccionamiento_completar(request, id_cambio):
    if request.method != 'POST':
        return redirect('cambios_pieza_lista')
    firma_ti_json = request.POST.get('firma_ti_json', '').strip()
    try:
        piezas_danadas = json_lib.loads(request.POST.get('piezas_danadas_json', '[]'))
    except (json_lib.JSONDecodeError, TypeError):
        piezas_danadas = []
    try:
        piezas_nuevas = json_lib.loads(request.POST.get('piezas_nuevas_json', '[]'))
    except (json_lib.JSONDecodeError, TypeError):
        piezas_nuevas = []
    if not (piezas_danadas or piezas_nuevas):
        messages.error(request, 'Indica al menos una pieza (dañada o nueva) para guardar.')
        return redirect('cambios_pieza_lista')
    usuario_ti_id = request.session.get('usuario_id')
    with connection.cursor() as cursor:
        cursor.execute("SELECT fk_equipo, fk_nombre_equipo, estatus, fk_ticket FROM atencion_cambio_pieza WHERE id_cambio = %s", [id_cambio])
        row = cursor.fetchone()
        if not row or row[2] != 'ABIERTO':
            messages.error(request, 'Este refaccionamiento ya no está pendiente.')
            return redirect('cambios_pieza_lista')
        fk_equipo, fk_nombre_equipo, _, fk_ticket = row
        cursor.execute("DELETE FROM atencion_cambio_pieza_detalle WHERE fk_cambio = %s", [id_cambio])
        for tipo, piezas in (('DANADA', piezas_danadas), ('NUEVA', piezas_nuevas)):
            for pieza in piezas:
                cursor.execute("""
                    INSERT INTO atencion_cambio_pieza_detalle (fk_cambio, tipo, cantidad, descripcion, marca, modelo, numero_serie)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, [id_cambio, tipo, pieza.get('cantidad') or 1, (pieza.get('descripcion') or '')[:200],
                      (pieza.get('marca') or '')[:100], (pieza.get('modelo') or '')[:100],
                      (pieza.get('numero_serie') or '')[:100]])
        if firma_ti_json:
            firma_obj = _firma_obj(cursor, usuario_ti_id, firma_ti_json)
            destinatario = _equipo_usuario_asignado_id(cursor, fk_equipo)
            estatus = 'PTI' if destinatario else 'FIN'
            cursor.execute("""
                UPDATE atencion_cambio_pieza SET estatus=%s, firma_ti=%s, fecha_cierre=%s WHERE id_cambio=%s
            """, [estatus, json_lib.dumps(firma_obj), datetime.now() if estatus == 'FIN' else None, id_cambio])
            _registrar_actividad(cursor, f'Cambio de Pieza completado — {fk_nombre_equipo or "Equipo"}', None,
                                  date_type.today(), None, usuario_ti_id, 'CAMBIO_PIEZA', id_cambio)
            if destinatario and fk_ticket is None:
                _crear_notificacion(cursor, destinatario, id_cambio, 'SERVICIO_PENDIENTE',
                                     f'Se completó el Cambio de Pieza pendiente de tu equipo {fk_nombre_equipo or ""} — pendiente tu firma.')
            messages.success(request, 'Refaccionamiento completado correctamente.')
        else:
            messages.success(request, 'Piezas guardadas. Puedes firmar más tarde para completar el refaccionamiento.')
    return redirect('cambios_pieza_lista')

# --- Reporte de Actividades (F-CECSA-TI-05): bitácora por rango de fechas ---

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def reporte_actividades_vista(request):
    ctx = {
        'rol_id': request.session.get('usuario_rol_id', ''),
        'username': request.session.get('usuario_username', ''),
        'nombre': request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'servicios/reporte_actividades.html', ctx)

def _actividades_del_rango(desde, hasta):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT a.actividad, a.descripcion, a.fecha_actividad, a.observaciones,
                   u.nombre, u.apellido_paterno, u.apellido_materno
            FROM reporte_actividades a
            LEFT JOIN usuarios u ON u.id_usuario = a.fk_usuario_ti
            WHERE a.fecha_actividad BETWEEN %s AND %s
            ORDER BY a.fecha_actividad, a.fecha_creacion
        """, [desde, hasta])
        return [{
            'actividad': r[0], 'descripcion': r[1] or '—', 'fecha': r[2].strftime('%d/%b/%Y'),
            'observaciones': r[3] or '—', 'tecnico': ' '.join(p for p in r[4:7] if p) or '—',
        } for r in cursor.fetchall()]

def _config_valor(cursor, clave, default=''):
    cursor.execute("SELECT valor FROM config_general WHERE clave = %s", [clave])
    row = cursor.fetchone()
    return row[0] if row and row[0] else default

@login_requerido
@rol_requerido('ti1')
@nivel_requerido('consultar')
def reporte_actividades_documento(request):
    desde = request.GET.get('desde', '')
    hasta = request.GET.get('hasta', '')
    try:
        f_desde = datetime.strptime(desde, '%Y-%m-%d').date()
        f_hasta = datetime.strptime(hasta, '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, 'Rango de fechas inválido.')
        return redirect('reporte_actividades')
    with connection.cursor() as cursor:
        numero_obra = _config_valor(cursor, 'numero_obra')
        descripcion_obra = _config_valor(cursor, 'descripcion_obra')
    ctx = {
        'desde': desde, 'hasta': hasta,
        'desde_fmt': f_desde.strftime('%d/%b/%Y'), 'hasta_fmt': f_hasta.strftime('%d/%b/%Y'),
        'numero_obra': numero_obra, 'descripcion_obra': descripcion_obra,
        'actividades': _actividades_del_rango(f_desde, f_hasta),
        'niveles': _niveles_sesion(request),
    }
    return render(request, 'servicios/reporte_actividades_doc.html', ctx)

@login_requerido
@nivel_requerido('exportar')
def reporte_actividades_documento_pdf(request):
    from django.conf import settings
    desde = request.GET.get('desde', '')
    hasta = request.GET.get('hasta', '')
    origen = request.build_absolute_uri('/')
    url = request.build_absolute_uri(f"/reporte-actividades/documento/?desde={desde}&hasta={hasta}")
    cookie_nombre = settings.SESSION_COOKIE_NAME
    cookie_valor = request.COOKIES.get(cookie_nombre)
    try:
        pdf_bytes = _generar_pdf(url, cookie_nombre, cookie_valor, origen)
    except Exception:
        messages.error(request, 'No se pudo generar el PDF, inténtalo de nuevo.')
        return redirect('reporte_actividades')
    from django.http import HttpResponse
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="reporte-actividades-{desde}-a-{hasta}.pdf"'
    response['Cache-Control'] = 'no-store'
    return response

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
                   s.firma_ti, s.es_mantenimiento
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
    if not row[20]:
        messages.error(request, 'Este ticket no tiene marcado Mantenimiento, no genera este reporte.')
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
    response['Cache-Control'] = 'no-store'
    return response

# --- Reportes de los servicios independientes (Mantenimiento / Respaldo / Cambio de Pieza) ---

@login_requerido
@nivel_requerido('consultar')
def reporte_mantenimiento_independiente(request, id_mantenimiento):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT m.fk_equipo, m.fk_nombre_equipo, m.lugar_actividad, m.descripcion, m.observaciones,
                   m.hallazgo_pieza_pendiente, m.estatus_firmas, m.fecha_creacion, m.fecha_cierre,
                   m.firma_ti, ti.nombre, ti.apellido_paterno, ti.apellido_materno
            FROM atencion_mantenimiento_independiente m
            LEFT JOIN usuarios ti ON ti.id_usuario = m.fk_usuario_ti
            WHERE m.id_mantenimiento = %s
        """, [id_mantenimiento])
        row = cursor.fetchone()
    if not row:
        messages.error(request, 'Registro no encontrado.')
        return redirect('dashboard')
    if not _puede_ver_servicio_independiente(request, row[0]):
        messages.error(request, 'No tienes permiso para ver este reporte.')
        return redirect('dashboard')
    firma_ti_data = row[9]
    if isinstance(firma_ti_data, str):
        firma_ti_data = json_lib.loads(firma_ti_data)
    firma_trazos = firma_ti_data.get('firma', []) if isinstance(firma_ti_data, dict) else []
    ctx = {
        'id_mantenimiento': id_mantenimiento,
        'nombre_equipo': row[1], 'lugar_actividad': row[2], 'descripcion': row[3],
        'observaciones': row[4], 'hallazgo': row[5], 'fecha': row[8] or row[7],
        'firma_ti_json': json_lib.dumps(firma_trazos),
        'tecnico_nombre': ' '.join(p for p in row[10:13] if p),
        'niveles': _niveles_sesion(request),
    }
    return render(request, 'servicios/reporte_mantenimiento_indep.html', ctx)

@login_requerido
@nivel_requerido('exportar')
def reporte_mantenimiento_independiente_pdf(request, id_mantenimiento):
    import os
    from django.conf import settings
    from django.urls import reverse
    from django.template.loader import get_template
    with connection.cursor() as cursor:
        cursor.execute("SELECT fk_equipo FROM atencion_mantenimiento_independiente WHERE id_mantenimiento = %s", [id_mantenimiento])
        row = cursor.fetchone()
    if not row or not _puede_ver_servicio_independiente(request, row[0]):
        messages.error(request, 'No tienes permiso para descargar este reporte.')
        return redirect('dashboard')
    mtime = os.path.getmtime(get_template('servicios/reporte_mantenimiento_indep.html').origin.name)
    clave = ('mant_indep', str(id_mantenimiento), mtime)
    pdf_bytes = _pdf_cache.get(clave)
    if pdf_bytes is None:
        origen = request.build_absolute_uri('/')
        url = request.build_absolute_uri(reverse('reporte_mantenimiento_independiente', args=[id_mantenimiento]))
        try:
            pdf_bytes = _generar_pdf(url, settings.SESSION_COOKIE_NAME,
                                      request.COOKIES.get(settings.SESSION_COOKIE_NAME), origen)
        except Exception:
            messages.error(request, 'No se pudo generar el PDF, inténtalo de nuevo.')
            return redirect('reporte_mantenimiento_independiente', id_mantenimiento=id_mantenimiento)
        _pdf_cache[clave] = pdf_bytes
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="reporte-mantenimiento-{id_mantenimiento}.pdf"'
    response['Cache-Control'] = 'no-store'
    return response

@login_requerido
@nivel_requerido('consultar')
def reporte_respaldo(request, id_respaldo):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT r.fk_equipo, r.fk_nombre_equipo, r.tipo_solicitud, r.tipo_fuente_datos,
                   r.ruta_unidad_compartida, r.archivos_respaldar, r.nombre_servidor, r.sistema_operativo,
                   r.ip_servidor, r.tipo_backup, r.observaciones_politica, r.total_gb_respaldar,
                   r.total_gb_crecimiento, r.periodicidad, r.nivel_backup, r.agenda, r.horario,
                   r.retencion_dias, r.firma_ti, r.fecha_creacion, r.fecha_cierre,
                   ti.nombre, ti.apellido_paterno, ti.apellido_materno,
                   r.fk_ticket, r.fk_usuario_solicitante, r.firma_usuario
            FROM atencion_respaldo r
            LEFT JOIN usuarios ti ON ti.id_usuario = r.fk_usuario_ti
            WHERE r.id_respaldo = %s
        """, [id_respaldo])
        row = cursor.fetchone()
        solicitante = _datos_solicitante(cursor, row[24], row[25]) if row else {}
    if not row:
        messages.error(request, 'Registro no encontrado.')
        return redirect('dashboard')
    if not _puede_ver_servicio_independiente(request, row[0]):
        messages.error(request, 'No tienes permiso para ver este reporte.')
        return redirect('dashboard')
    def _trazos(dato):
        if isinstance(dato, str):
            dato = json_lib.loads(dato)
        return dato.get('firma', []) if isinstance(dato, dict) else []
    firma_trazos = _trazos(row[18])
    # El formato lo firma quien solicita el respaldo, no el area de T.I
    firma_solicitante = _trazos(row[26]) if row[26] else []
    ctx = {
        'id_respaldo': id_respaldo, 'nombre_equipo': row[1], 'tipo_solicitud': row[2],
        'tipo_fuente_datos': row[3], 'ruta_unidad_compartida': row[4], 'archivos_respaldar': row[5],
        'nombre_servidor': row[6], 'sistema_operativo': row[7],
        'ip_servidor': _sin_no_aplica(row[8]),
        'tipo_backup': row[9], 'observaciones_politica': row[10], 'total_gb_respaldar': row[11],
        'total_gb_crecimiento': row[12], 'periodicidad': row[13], 'nivel_backup': row[14],
        'agenda': row[15], 'horario': row[16], 'retencion_dias': row[17],
        'fecha': row[20] or row[19],
        'firma_ti_json': json_lib.dumps(firma_trazos),
        'firma_solicitante_json': json_lib.dumps(firma_solicitante),
        'tecnico_nombre': ' '.join(p for p in row[21:24] if p),
        'solicitante': solicitante,
        'niveles': _niveles_sesion(request),
    }
    return render(request, 'servicios/reporte_respaldo.html', ctx)

@login_requerido
@nivel_requerido('exportar')
def reporte_respaldo_pdf(request, id_respaldo):
    import os
    from django.conf import settings
    from django.urls import reverse
    from django.template.loader import get_template
    with connection.cursor() as cursor:
        cursor.execute("SELECT fk_equipo FROM atencion_respaldo WHERE id_respaldo = %s", [id_respaldo])
        row = cursor.fetchone()
    if not row or not _puede_ver_servicio_independiente(request, row[0]):
        messages.error(request, 'No tienes permiso para descargar este reporte.')
        return redirect('dashboard')
    mtime = os.path.getmtime(get_template('servicios/reporte_respaldo.html').origin.name)
    clave = ('respaldo', str(id_respaldo), mtime)
    pdf_bytes = _pdf_cache.get(clave)
    if pdf_bytes is None:
        origen = request.build_absolute_uri('/')
        url = request.build_absolute_uri(reverse('reporte_respaldo', args=[id_respaldo]))
        try:
            pdf_bytes = _generar_pdf(url, settings.SESSION_COOKIE_NAME,
                                      request.COOKIES.get(settings.SESSION_COOKIE_NAME), origen)
        except Exception:
            messages.error(request, 'No se pudo generar el PDF, inténtalo de nuevo.')
            return redirect('reporte_respaldo', id_respaldo=id_respaldo)
        _pdf_cache[clave] = pdf_bytes
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="reporte-respaldo-{id_respaldo}.pdf"'
    response['Cache-Control'] = 'no-store'
    return response

@login_requerido
@nivel_requerido('consultar')
def reporte_cambio_pieza(request, id_cambio):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT c.fk_equipo, c.fk_nombre_equipo, c.fecha_cambio, c.firma_ti, c.firma_usuario,
                   c.fecha_creacion, c.fecha_cierre,
                   ti.nombre, ti.apellido_paterno, ti.apellido_materno, tip.nombre_puesto
            FROM atencion_cambio_pieza c
            LEFT JOIN usuarios ti ON ti.id_usuario = c.fk_usuario_ti
            LEFT JOIN cat_puestos tip ON tip.id_puesto = ti.fk_puesto
            WHERE c.id_cambio = %s
        """, [id_cambio])
        row = cursor.fetchone()
        if not row:
            messages.error(request, 'Registro no encontrado.')
            return redirect('dashboard')
        if not _puede_ver_servicio_independiente(request, row[0]):
            messages.error(request, 'No tienes permiso para ver este reporte.')
            return redirect('dashboard')
        cursor.execute("""
            SELECT tipo, cantidad, descripcion, marca, modelo, numero_serie
            FROM atencion_cambio_pieza_detalle WHERE fk_cambio = %s
        """, [id_cambio])
        danadas, nuevas = [], []
        for t, cant, desc, marca, modelo, serie in cursor.fetchall():
            item = {'cantidad': cant, 'descripcion': desc, 'marca': marca, 'modelo': modelo, 'numero_serie': serie}
            (danadas if t == 'DANADA' else nuevas).append(item)
        cursor.execute("SELECT usuario_asignado FROM cat_equipos WHERE id_equipo = %s", [row[0]])
        eq = cursor.fetchone()
        resguardante_puesto = ''
        id_resguardante = _equipo_usuario_asignado_id(cursor, row[0])
        if id_resguardante:
            cursor.execute("""
                SELECT p.nombre_puesto FROM usuarios u
                LEFT JOIN cat_puestos p ON p.id_puesto = u.fk_puesto
                WHERE u.id_usuario = %s
            """, [id_resguardante])
            r = cursor.fetchone()
            resguardante_puesto = r[0] if r and r[0] else ''
    firma_ti_data = row[3]
    if isinstance(firma_ti_data, str):
        firma_ti_data = json_lib.loads(firma_ti_data)
    firma_ti_trazos = firma_ti_data.get('firma', []) if isinstance(firma_ti_data, dict) else []
    firma_us_data = row[4]
    if isinstance(firma_us_data, str):
        firma_us_data = json_lib.loads(firma_us_data)
    firma_us_trazos = firma_us_data.get('firma', []) if isinstance(firma_us_data, dict) else []
    ctx = {
        'id_cambio': id_cambio, 'nombre_equipo': row[1], 'fecha': row[2] or row[5],
        'danadas': danadas, 'nuevas': nuevas,
        'firma_ti_json': json_lib.dumps(firma_ti_trazos),
        'firma_usuario_json': json_lib.dumps(firma_us_trazos),
        'tecnico_nombre': ' '.join(p for p in row[7:10] if p),
        'tecnico_puesto': row[10] or '',
        'resguardante_nombre': eq[0] if eq else '',
        'resguardante_puesto': resguardante_puesto,
        'niveles': _niveles_sesion(request),
    }
    return render(request, 'servicios/reporte_cambio_pieza.html', ctx)

@login_requerido
@nivel_requerido('exportar')
def reporte_cambio_pieza_pdf(request, id_cambio):
    import os
    from django.conf import settings
    from django.urls import reverse
    from django.template.loader import get_template
    with connection.cursor() as cursor:
        cursor.execute("SELECT fk_equipo FROM atencion_cambio_pieza WHERE id_cambio = %s", [id_cambio])
        row = cursor.fetchone()
    if not row or not _puede_ver_servicio_independiente(request, row[0]):
        messages.error(request, 'No tienes permiso para descargar este reporte.')
        return redirect('dashboard')
    mtime = os.path.getmtime(get_template('servicios/reporte_cambio_pieza.html').origin.name)
    clave = ('cambio_pieza', str(id_cambio), mtime)
    pdf_bytes = _pdf_cache.get(clave)
    if pdf_bytes is None:
        origen = request.build_absolute_uri('/')
        url = request.build_absolute_uri(reverse('reporte_cambio_pieza', args=[id_cambio]))
        try:
            pdf_bytes = _generar_pdf(url, settings.SESSION_COOKIE_NAME,
                                      request.COOKIES.get(settings.SESSION_COOKIE_NAME), origen)
        except Exception:
            messages.error(request, 'No se pudo generar el PDF, inténtalo de nuevo.')
            return redirect('reporte_cambio_pieza', id_cambio=id_cambio)
        _pdf_cache[clave] = pdf_bytes
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="reporte-cambio-pieza-{id_cambio}.pdf"'
    response['Cache-Control'] = 'no-store'
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
            'stats': {'por_rol': []}, 'usuarios': [], 'roles': [], 'puestos': [],
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
                   u.fecha_modificacion, u.fk_rol, r.descripcion,
                   u.fk_puesto, p.nombre_puesto
            FROM usuarios u
            LEFT JOIN cat_roles r ON u.fk_rol = r.id_rol
            LEFT JOIN cat_puestos p ON u.fk_puesto = p.id_puesto
            ORDER BY u.fecha_creacion DESC
        """)
        usuarios = cursor.fetchall()
        cursor.execute("SELECT id_rol, descripcion FROM cat_roles ORDER BY descripcion")
        roles = [{'id_rol': r[0], 'descripcion': r[1]} for r in cursor.fetchall()]
        cursor.execute("SELECT id_puesto, nombre_puesto FROM cat_puestos WHERE estatus = 'ACT' ORDER BY nombre_puesto")
        puestos = [{'id_puesto': r[0], 'nombre_puesto': r[1]} for r in cursor.fetchall()]
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
        'puestos':  puestos,
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
    fk_puesto        = request.POST.get('fk_puesto', '').strip() or None
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
                telefono, estatus, fecha_creacion, fk_rol, area, fk_puesto
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, [
            id_usuario, username, password, email,
            nombre, apellido_paterno, apellido_materno,
            telefono, estatus, now, fk_rol, area, fk_puesto,
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
                   telefono, estatus, fk_rol, password, area, fk_puesto
            FROM usuarios WHERE id_usuario = %s
        """, [id_usuario])
        row = cursor.fetchone()
    if not row:
        return JsonResponse({'error': 'No encontrado'}, status=404)
    cols = ['id_usuario','username','email',
            'nombre','apellido_paterno','apellido_materno',
            'telefono','estatus','fk_rol','password','area','fk_puesto']
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
    fk_puesto        = request.POST.get('fk_puesto', '').strip() or None
    area             = request.POST.get('area', '').strip() or None
    now              = datetime.now()
    with connection.cursor() as cursor:
        if password:
            cursor.execute("""
                UPDATE usuarios SET
                    username=%s, password=%s, email=%s,
                    nombre=%s, apellido_paterno=%s, apellido_materno=%s,
                    telefono=%s, estatus=%s, fk_rol=%s, area=%s, fk_puesto=%s,
                    fecha_modificacion=%s
                WHERE id_usuario = %s
            """, [username, password, email, nombre, apellido_paterno,
                  apellido_materno, telefono, estatus, fk_rol, area, fk_puesto, now, id_usuario])
        else:
            cursor.execute("""
                UPDATE usuarios SET
                    username=%s, email=%s,
                    nombre=%s, apellido_paterno=%s, apellido_materno=%s,
                    telefono=%s, estatus=%s, fk_rol=%s, area=%s, fk_puesto=%s,
                    fecha_modificacion=%s
                WHERE id_usuario = %s
            """, [username, email, nombre, apellido_paterno,
                  apellido_materno, telefono, estatus, fk_rol, area, fk_puesto, now, id_usuario])
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

# --- Puestos (niveles de empresa que se imprimen en los reportes) — solo T.I ---

@login_requerido
@rol_requerido('ti1')
def puestos_lista(request):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id_puesto, nombre_puesto, estatus, fecha_creacion
            FROM cat_puestos ORDER BY nombre_puesto
        """)
        puestos = [{'id_puesto': r[0], 'nombre_puesto': r[1], 'estatus': r[2], 'fecha_creacion': r[3]}
                   for r in cursor.fetchall()]
    ctx = {
        'puestos': puestos,
        'rol_id':   request.session.get('usuario_rol_id', ''),
        'username': request.session.get('usuario_username', ''),
        'nombre':   request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'usuario_area': request.session.get('usuario_area', ''),
    }
    return render(request, 'configuracion/puestos.html', ctx)

@login_requerido
@rol_requerido('ti1')
def puesto_guardar(request):
    if request.method != 'POST':
        return redirect('puestos_lista')
    nombre_puesto = request.POST.get('nombre_puesto', '').strip()
    if not nombre_puesto:
        messages.error(request, 'El nombre del puesto es obligatorio.')
        return redirect('puestos_lista')
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM cat_puestos WHERE UPPER(nombre_puesto) = UPPER(%s)", [nombre_puesto])
        if cursor.fetchone():
            messages.error(request, f'Ya existe un puesto llamado "{nombre_puesto}".')
            return redirect('puestos_lista')
        cursor.execute("INSERT INTO cat_puestos (nombre_puesto) VALUES (%s)", [nombre_puesto])
    messages.success(request, f'Puesto "{nombre_puesto}" creado correctamente.')
    return redirect('puestos_lista')

@login_requerido
@rol_requerido('ti1')
def puesto_actualizar(request, id_puesto):
    if request.method != 'POST':
        return redirect('puestos_lista')
    nombre_puesto = request.POST.get('nombre_puesto', '').strip()
    estatus = request.POST.get('estatus', 'ACT')
    if not nombre_puesto:
        messages.error(request, 'El nombre del puesto es obligatorio.')
        return redirect('puestos_lista')
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM cat_puestos WHERE UPPER(nombre_puesto) = UPPER(%s) AND id_puesto != %s",
                       [nombre_puesto, id_puesto])
        if cursor.fetchone():
            messages.error(request, f'Ya existe un puesto llamado "{nombre_puesto}".')
            return redirect('puestos_lista')
        cursor.execute("""
            UPDATE cat_puestos SET nombre_puesto=%s, estatus=%s, fecha_modificacion=%s
            WHERE id_puesto=%s
        """, [nombre_puesto, estatus, datetime.now(), id_puesto])
    messages.success(request, f'Puesto "{nombre_puesto}" actualizado correctamente.')
    return redirect('puestos_lista')
