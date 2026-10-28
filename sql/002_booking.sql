-- Clinic Concierge, part 2: appointments, leads, human handoffs and conversation memory.
-- Applied automatically by the chat app (app/booking.py) and the seed command (python -m app.seed).

create table if not exists providers (
    id            text primary key,
    name          text not null,
    title         text,
    can_evaluate  boolean not null default false,
    focus         text
);

-- One row per bookable hour with one provider. status: open | booked | blocked
create table if not exists slots (
    id            bigserial primary key,
    provider_id   text not null references providers(id),
    starts_at     timestamptz not null,
    ends_at       timestamptz not null,
    status        text not null default 'open',
    unique (provider_id, starts_at)
);
create index if not exists slots_open_starts on slots (starts_at) where status = 'open';

-- Anyone who gave contact details. One per (session, phone) so a chat doesn't create duplicates.
create table if not exists leads (
    id            bigserial primary key,
    session_id    uuid,
    name          text not null,
    phone         text not null,
    email         text,
    reason        text,
    insurance     text,
    urgency       text,
    status        text not null default 'new',          -- new | booked | handed_off
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (session_id, phone)
);

create table if not exists appointments (
    id            bigserial primary key,
    slot_id       bigint not null unique references slots(id),   -- unique: a slot can't be booked twice
    lead_id       bigint references leads(id),
    visit_type    text not null,                                  -- evaluation | follow_up
    patient_name  text not null,
    phone         text not null,
    email         text,
    reason        text,
    status        text not null default 'booked',                 -- booked | cancelled
    created_at    timestamptz not null default now()
);

create table if not exists handoffs (
    id            bigserial primary key,
    session_id    uuid,
    lead_id       bigint references leads(id),
    reason        text not null,
    summary       text not null,
    status        text not null default 'open',                   -- open | done
    created_at    timestamptz not null default now()
);

-- Conversation memory. `state` holds the booking flow (collected details, a booking awaiting "yes").
create table if not exists sessions (
    id            uuid primary key,
    state         jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create table if not exists messages (
    id            bigserial primary key,
    session_id    uuid not null references sessions(id) on delete cascade,
    role          text not null,                                  -- user | assistant
    content       text not null,
    route         text,                                           -- answer | booking | confirm | handoff
    meta          jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);
create index if not exists messages_session on messages (session_id, id);

-- Every tool call the agent makes, for debugging and evals.
create table if not exists tool_calls (
    id            bigserial primary key,
    session_id    uuid,
    tool          text not null,
    arguments     jsonb not null,
    result        jsonb not null,
    ok            boolean not null,
    created_at    timestamptz not null default now()
);

-- Not readable through Supabase's web API (leads hold names and phone numbers): see 001_schema.sql.
alter table providers enable row level security;
alter table slots enable row level security;
alter table leads enable row level security;
alter table appointments enable row level security;
alter table handoffs enable row level security;
alter table sessions enable row level security;
alter table messages enable row level security;
alter table tool_calls enable row level security;
