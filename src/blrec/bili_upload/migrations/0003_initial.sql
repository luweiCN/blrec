CREATE TABLE bili_account_selection (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    primary_account_id INTEGER NOT NULL REFERENCES bili_accounts(id)
);

INSERT INTO bili_account_selection(id,primary_account_id)
SELECT 1,id
FROM bili_accounts
WHERE state = 'active'
ORDER BY id
LIMIT 1;
