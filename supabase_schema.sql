-- =============================================================
-- ESQUEMA SQL - Sistema de Control de Mesoneros
-- Ejecutar TODO este script en Supabase: Panel -> SQL Editor -> New Query
-- =============================================================

create extension if not exists pgcrypto;

-- -------------------------------------------------------------
-- Tabla de usuarios (evaluadores y administrador general)
-- -------------------------------------------------------------
create table if not exists usuarios (
    id uuid primary key default gen_random_uuid(),
    nombre_usuario text unique not null,
    nombre_completo text not null,
    password_hash text not null,
    rol text not null check (rol in ('evaluador', 'admin_general')),
    activo boolean not null default true,
    created_at timestamptz not null default now()
);

-- -------------------------------------------------------------
-- Tabla de mesoneros (los 15 evaluados)
-- -------------------------------------------------------------
create table if not exists mesoneros (
    id uuid primary key default gen_random_uuid(),
    nombre_completo text not null,
    activo boolean not null default true,
    created_at timestamptz not null default now()
);

-- -------------------------------------------------------------
-- Tabla de evaluaciones (cada error / amonestación queda como
-- una fila individual, con fecha, quien evaluo y justificacion)
-- -------------------------------------------------------------
create table if not exists evaluaciones (
    id uuid primary key default gen_random_uuid(),
    fecha date not null default current_date,
    mesonero_id uuid not null references mesoneros(id) on delete cascade,
    evaluador_id uuid not null references usuarios(id),
    tipo text not null check (tipo in ('error_estandar', 'amonestacion_grave')),
    justificacion text not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_evaluaciones_fecha on evaluaciones (fecha);
create index if not exists idx_evaluaciones_mesonero on evaluaciones (mesonero_id);

-- -------------------------------------------------------------
-- Tabla de logs / rastro de auditoria
-- -------------------------------------------------------------
create table if not exists logs_auditoria (
    id uuid primary key default gen_random_uuid(),
    usuario_id uuid references usuarios(id),
    nombre_usuario text,
    accion text not null,
    detalle text,
    fecha_hora timestamptz not null default now()
);

create index if not exists idx_logs_fecha on logs_auditoria (fecha_hora);

-- -------------------------------------------------------------
-- (Opcional pero recomendado) Activar Row Level Security.
-- La app se conecta con la "service_role key" (ver guía paso 2),
-- la cual SIEMPRE puede pasar por encima de estas reglas, así que
-- esto es solo una capa extra de seguridad por si alguna vez usas
-- la "anon key" en algún otro lugar.
-- -------------------------------------------------------------
alter table usuarios enable row level security;
alter table mesoneros enable row level security;
alter table evaluaciones enable row level security;
alter table logs_auditoria enable row level security;

-- No se crean políticas "allow" para el rol anon a propósito:
-- esto bloquea cualquier acceso público directo a estas tablas.

-- -------------------------------------------------------------
-- Sembrar el primer usuario Administrador General
-- Usuario:     admin
-- Contraseña:  admin123   <-- CAMBIA ESTA CONTRASEÑA en tu primer
--              inicio de sesión (créate un usuario nuevo desde el
--              panel de Administración y desactiva/borra este,
--              o pide que se agregue la opción de cambio de clave).
-- -------------------------------------------------------------
insert into usuarios (nombre_usuario, nombre_completo, password_hash, rol)
values (
    'admin',
    'Administrador General',
    '$2b$12$h00NDHpVsvMpj0Tj7NWGleh60DNtS7dfOTCuXDWI/L07Uwe3.eBmi',
    'admin_general'
)
on conflict (nombre_usuario) do nothing;
