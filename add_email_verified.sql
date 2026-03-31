-- Correr UMA VEZ no terminal:
-- sqlite3 database.db < add_email_verified.sql

-- Adicionar coluna de verificação de email
-- DEFAULT 0 = não verificado
-- Os utilizadores de teste existentes ficam com 0 — precisam de verificar
ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0;

-- OPCIONAL: se quiseres que os utilizadores de teste já existentes
-- fiquem como verificados (para não bloqueares o teu próprio acesso),
-- descomenta a linha abaixo:
UPDATE users SET email_verified = 1;
