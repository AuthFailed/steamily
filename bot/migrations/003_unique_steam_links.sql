DO $$
BEGIN
    -- Keep the duplicate check and index creation atomic against account writes.
    LOCK TABLE accounts IN SHARE ROW EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM accounts GROUP BY steam_id HAVING count(*) > 1) THEN
        RAISE EXCEPTION 'Duplicate Steam account links prevent startup. See docs/steam-account-links.md for recovery.'
            USING HINT = 'SELECT steam_id, array_agg(telegram_id ORDER BY telegram_id) FROM accounts '
                         'GROUP BY steam_id HAVING count(*) > 1;';
    END IF;
    CREATE UNIQUE INDEX IF NOT EXISTS accounts_steam_id_key ON accounts (steam_id);
END $$;
