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

def login_view(request):
    if request.session.get('usuario_id'):
        return redirect('dashboard')
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT u.id_usuario, u.nombre, u.apellido_paterno, u.apellido_materno,
                       u.fk_rol, u.estatus, r.descripcion
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
    }
    return render(request, 'dashboard.html', context)

def logout_view(request):
    request.session.flush()
    return redirect('login')

@login_requerido
def tickets_lista(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT id_equipo, nombre_equipo FROM cat_equipos ORDER BY nombre_equipo")
        equipos = [{'id_equipo': r[0], 'nombre_equipo': r[1]} for r in cursor.fetchall()]
        cursor.execute("""
            SELECT id_ticket, titulo, area, dirigido_personal,
                   estatus, fecha_creacion, fk_equipo, fk_nombre_equipo,
                   estatus_usuario, descripcion
            FROM ticket_usuario
            ORDER BY fecha_creacion DESC
        """)
        cols = ['id_ticket','titulo','area','dirigido_personal',
                'estatus','fecha_creacion','fk_equipo','fk_nombre_equipo','estatus_usuario','descripcion']
        tickets = [dict(zip(cols, r)) for r in cursor.fetchall()]
        cursor.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN estatus='PEN' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN estatus='PRO' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN estatus='FIN' THEN 1 ELSE 0 END)
            FROM ticket_usuario
        """)
        s = cursor.fetchone()
        stats = {'total': s[0], 'pendiente': s[1] or 0, 'proceso': s[2] or 0, 'finalizado': s[3] or 0}
    ctx = {
        'tickets': tickets,
        'equipos': equipos,
        'stats':   stats,
        'username': request.session.get('usuario_username', ''),
        'nombre':   request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'rol':      request.session.get('usuario_rol', 'Usuario'),
    }
    return render(request, 'tickets/lista.html', ctx)

@login_requerido
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
def ticket_datos(request, id_ticket):
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
def ticket_foto(request, id_ticket, n):
    from django.http import HttpResponse, Http404
    col = f'foto{n}'
    if col not in ('foto1', 'foto2', 'foto3', 'foto4'):
        raise Http404
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {col} FROM ticket_usuario WHERE id_ticket = %s", [id_ticket])
        row = cursor.fetchone()
    if not row or row[0] is None:
        raise Http404
    return HttpResponse(bytes(row[0]), content_type='image/jpeg')

@login_requerido
def ticket_actualizar(request, id_ticket):
    if request.method != 'POST':
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
def ticket_cerrar(request, id_ticket):
    if request.method != 'POST':
        return redirect('tickets_lista')
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE ticket_usuario
            SET estatus_usuario=TRUE, fecha_cierre=%s
            WHERE id_ticket=%s AND estatus_usuario=FALSE
        """, [datetime.now(), id_ticket])
    messages.success(request, 'Ticket cerrado y enviado a T.I correctamente.')
    return redirect('tickets_lista')

@login_requerido
def equipos_lista(request):
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
def equipo_form(request, id_equipo=None):
    context = {
        'username':  request.session.get('usuario_username', ''),
        'nombre':    request.session.get('usuario_nombre', ''),
        'apellido':  request.session.get('usuario_paterno', ''),
        'rol':       request.session.get('usuario_rol', 'Usuario'),
        'editando':  id_equipo is not None,
        'id_equipo': id_equipo,
    }
    return render(request, 'equipos/form.html', context)

@login_requerido
def usuarios_lista(request):
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
                telefono, estatus, fecha_creacion, fk_rol
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, [
            id_usuario, username, password, email,
            nombre, apellido_paterno, apellido_materno,
            telefono, estatus, now, fk_rol,
        ])
    messages.success(request, f'Usuario {nombre} {apellido_paterno} dado de alta correctamente (ID: {id_usuario}).')
    return redirect('usuarios_lista')

@login_requerido
def usuario_datos(request, id_usuario):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id_usuario, username, email,
                   nombre, apellido_paterno, apellido_materno,
                   telefono, estatus, fk_rol, password
            FROM usuarios WHERE id_usuario = %s
        """, [id_usuario])
        row = cursor.fetchone()
    if not row:
        return JsonResponse({'error': 'No encontrado'}, status=404)
    cols = ['id_usuario','username','email',
            'nombre','apellido_paterno','apellido_materno',
            'telefono','estatus','fk_rol','password']
    data = {k: (v if v is not None else '') for k, v in zip(cols, row)}
    return JsonResponse(data)

@login_requerido
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
    now              = datetime.now()
    with connection.cursor() as cursor:
        if password:
            cursor.execute("""
                UPDATE usuarios SET
                    username=%s, password=%s, email=%s,
                    nombre=%s, apellido_paterno=%s, apellido_materno=%s,
                    telefono=%s, estatus=%s, fk_rol=%s,
                    fecha_modificacion=%s
                WHERE id_usuario = %s
            """, [username, password, email, nombre, apellido_paterno,
                  apellido_materno, telefono, estatus, fk_rol, now, id_usuario])
        else:
            cursor.execute("""
                UPDATE usuarios SET
                    username=%s, email=%s,
                    nombre=%s, apellido_paterno=%s, apellido_materno=%s,
                    telefono=%s, estatus=%s, fk_rol=%s,
                    fecha_modificacion=%s
                WHERE id_usuario = %s
            """, [username, email, nombre, apellido_paterno,
                  apellido_materno, telefono, estatus, fk_rol, now, id_usuario])
    messages.success(request, f'Usuario {nombre} {apellido_paterno} actualizado correctamente.')
    return redirect('usuarios_lista')

@login_requerido
def configuracion_vista(request):
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
        'niveles': niveles, 'permisos': permisos, 'roles': roles,
        'username': request.session.get('usuario_username', ''),
        'nombre':   request.session.get('usuario_nombre', ''),
        'apellido': request.session.get('usuario_paterno', ''),
        'rol':      request.session.get('usuario_rol', 'Usuario'),
    }
    return render(request, 'configuracion/lista.html', ctx)

@login_requerido
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
