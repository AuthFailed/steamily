-- Registered accounts retain shared snapshots, chat settings and queued alerts until explicit deletion.
DELETE FROM families f WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.family_id = f.family_id);
