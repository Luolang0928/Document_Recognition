-- schema.sql
DROP TABLE IF EXISTS recognize_history;

CREATE TABLE recognize_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    model TEXT,
    spec TEXT,
    manufacturer TEXT,
    production_date TEXT,
    shipment_date TEXT,
    batch_number TEXT,
    remark TEXT,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);