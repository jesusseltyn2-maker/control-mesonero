-- =============================================================
-- ESQUEMA SQL - Sistema de Control de Personal por Áreas
-- Este archivo es para INSTALACIONES NUEVAS desde cero.
-- Si ya tenías la app instalada, usa el script de MIGRACIÓN que
-- te haya dado el asistente en el chat, no este archivo completo.
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
-- Catálogo de áreas del negocio (Mesoneros, Cocina, Barra, etc.)
-- Cada área tiene su propio tope de errores estándar por día.
-- -------------------------------------------------------------
create table if not exists areas (
    id uuid primary key default gen_random_uuid(),
    nombre text unique not null,
    max_errores_estandar integer not null default 3,
    activo boolean not null default true,
    created_at timestamptz not null default now()
);

insert into areas (nombre) values
    ('Mesoneros'), ('Panadería'), ('Pastelería'), ('Producción'), ('Cocina'),
    ('Cajeras'), ('Aeropuerto'), ('Administración'), ('Depósitos'), ('Barra'), ('Limpieza')
on conflict (nombre) do nothing;

-- -------------------------------------------------------------
-- Catálogo de turnos fijos (Mañana, Tarde, Noche, o los que
-- definas). Cada trabajador tiene un turno fijo asignado.
-- -------------------------------------------------------------
create table if not exists turnos (
    id uuid primary key default gen_random_uuid(),
    nombre text unique not null,
    orden integer not null default 1,
    activo boolean not null default true,
    created_at timestamptz not null default now()
);

insert into turnos (nombre, orden) values
    ('Mañana', 1), ('Noche', 2)
on conflict (nombre) do nothing;

-- -------------------------------------------------------------
-- Tabla de trabajadores (de cualquier área, no solo mesoneros)
-- -------------------------------------------------------------
create table if not exists mesoneros (
    id uuid primary key default gen_random_uuid(),
    nombre_completo text not null,
    area_id uuid references areas(id),
    turno_id uuid references turnos(id),
    activo boolean not null default true,
    created_at timestamptz not null default now()
);

-- -------------------------------------------------------------
-- Tabla de evaluaciones (cada error / amonestación individual)
-- -------------------------------------------------------------
create table if not exists evaluaciones (
    id uuid primary key default gen_random_uuid(),
    fecha date not null default current_date,
    turno_id uuid references turnos(id),
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
-- Tabla de cierres de turno: registra qué turno (Mañana/Tarde/
-- Noche) se cerró cada día, quién lo cerró y a qué hora exacta.
-- Un cierre aplica a TODAS las áreas a la vez.
-- -------------------------------------------------------------
create table if not exists cierres_turno (
    id uuid primary key default gen_random_uuid(),
    fecha date not null default current_date,
    turno_id uuid references turnos(id),
    evaluador_id uuid not null references usuarios(id),
    fecha_hora timestamptz not null default now()
);

create index if not exists idx_cierres_fecha on cierres_turno (fecha);

-- -------------------------------------------------------------
-- Row Level Security. La app se conecta con la "service_role key"
-- (ver guía paso a paso), que siempre puede pasar por encima de
-- estas reglas — esto es una capa extra de seguridad.
-- -------------------------------------------------------------
alter table usuarios enable row level security;
alter table areas enable row level security;
alter table turnos enable row level security;
alter table mesoneros enable row level security;
alter table evaluaciones enable row level security;
alter table logs_auditoria enable row level security;
alter table cierres_turno enable row level security;

-- -------------------------------------------------------------
-- Sembrar el primer usuario Administrador General
-- Usuario:     admin
-- Contraseña:  admin123   <-- CAMBIA ESTA CONTRASEÑA en tu primer
--              inicio de sesión desde "Mi cuenta".
-- -------------------------------------------------------------
insert into usuarios (nombre_usuario, nombre_completo, password_hash, rol)
values (
    'admin',
    'Administrador General',
    '$2b$12$h00NDHpVsvMpj0Tj7NWGleh60DNtS7dfOTCuXDWI/L07Uwe3.eBmi',
    'admin_general'
)
on conflict (nombre_usuario) do nothing;
