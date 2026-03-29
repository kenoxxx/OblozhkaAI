-- Выполни в Supabase → SQL Editor

-- Таблица пользователей (без face_photo_url — фото не хранятся постоянно)
CREATE TABLE IF NOT EXISTS public.users (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT      NOT NULL UNIQUE,
    username         TEXT        DEFAULT '',
    generations_used INTEGER     NOT NULL DEFAULT 0,
    balance          INTEGER     NOT NULL DEFAULT 1, -- Стартовый баланс (1 беспл. генерация)
    referrer_id      BIGINT,                         -- ID того, кто пригласил
    bonus_received   BOOLEAN     DEFAULT FALSE,      -- Получил ли пригласивший бонус за этого юзера
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_user_id ON public.users(user_id);

-- Таблица истории обложек
CREATE TABLE IF NOT EXISTS public.user_history (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    file_id     TEXT        NOT NULL,
    prompt      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_history_user_id ON public.user_history(user_id);

-- Создай bucket для временных фото (Storage → New bucket):
-- Название: faces
-- Public: ON (включено)
--
-- Фото загружается перед генерацией и удаляется сразу после.
-- Максимальный размер bucket: 50 MB (хватает — файлы живут секунды).
