import json
import sqlite3
import os
from datetime import datetime

class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS trades(
            id TEXT PRIMARY KEY,symbol TEXT,side TEXT,entry REAL,exit REAL,pnl REAL,
            opened_at TEXT,closed_at TEXT,reason TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,value TEXT NOT NULL)""")
        self.db.commit()

    def add_trade(self, position, exit_price, pnl, reason):
        self.db.execute(
            "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
            (
                position.id, position.symbol, position.side, position.entry,
                exit_price, pnl, position.opened_at.isoformat(),
                datetime.utcnow().isoformat(), reason
            )
        )
        self.db.commit()

    def history(self):
        c = self.db.execute("SELECT * FROM trades ORDER BY closed_at DESC LIMIT 20")
        cols = [x[0] for x in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]

    def get_settings(self):
        c = self.db.execute("SELECT key,value FROM settings")
        out = {}
        for key, value in c.fetchall():
            try:
                out[key] = json.loads(value)
            except Exception:
                out[key] = value
        return out

    def save_settings(self, values):
        for key, value in values.items():
            self.db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                (key, json.dumps(value))
            )
        self.db.commit()

    def reset(self):
        self.db.execute("DELETE FROM trades")
        self.db.commit()
