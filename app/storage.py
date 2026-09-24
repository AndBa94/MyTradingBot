import json
import sqlite3
import os
from datetime import datetime, timezone

class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS trades(
            id TEXT PRIMARY KEY,symbol TEXT,side TEXT,entry REAL,exit REAL,pnl REAL,
            opened_at TEXT,closed_at TEXT,reason TEXT,fees REAL DEFAULT 0,
            setup TEXT DEFAULT '',score_10 REAL DEFAULT 0,score_components TEXT DEFAULT '{}',
            forensic TEXT DEFAULT '{}')""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(trades)").fetchall()}
        if "fees" not in columns:
            self.db.execute("ALTER TABLE trades ADD COLUMN fees REAL DEFAULT 0")
        if "setup" not in columns:
            self.db.execute("ALTER TABLE trades ADD COLUMN setup TEXT DEFAULT ''")
        if "score_10" not in columns:
            self.db.execute("ALTER TABLE trades ADD COLUMN score_10 REAL DEFAULT 0")
        if "score_components" not in columns:
            self.db.execute("ALTER TABLE trades ADD COLUMN score_components TEXT DEFAULT '{}'")
        if "forensic" not in columns:
            self.db.execute("ALTER TABLE trades ADD COLUMN forensic TEXT DEFAULT '{}'")
        self.db.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,value TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS positions(
            id TEXT PRIMARY KEY,symbol TEXT,side TEXT,entry REAL,quantity REAL,
            stop_loss REAL,take_profits TEXT,opened_at TEXT,leverage INTEGER,
            pnl REAL,status TEXT,initial_quantity REAL,realized_pnl REAL,
            tp_index INTEGER,last_price REAL,initial_stop_loss REAL,
            entry_fee REAL,fees REAL,setup TEXT DEFAULT '',score_10 REAL DEFAULT 0,
            score_components TEXT DEFAULT '{}',forensic TEXT DEFAULT '{}')""")
        position_columns = {row[1] for row in self.db.execute("PRAGMA table_info(positions)").fetchall()}
        if "setup" not in position_columns:
            self.db.execute("ALTER TABLE positions ADD COLUMN setup TEXT DEFAULT ''")
        if "score_10" not in position_columns:
            self.db.execute("ALTER TABLE positions ADD COLUMN score_10 REAL DEFAULT 0")
        if "score_components" not in position_columns:
            self.db.execute("ALTER TABLE positions ADD COLUMN score_components TEXT DEFAULT '{}'")
        if "forensic" not in position_columns:
            self.db.execute("ALTER TABLE positions ADD COLUMN forensic TEXT DEFAULT '{}'")
        self.db.commit()

    @staticmethod
    def _entry_forensics(position):
        risk = abs(float(position.entry) - float(position.stop_loss))
        first_tp = float(position.take_profits[0]) if position.take_profits else None
        final_tp = float(position.take_profits[-1]) if position.take_profits else None
        return {
            "price": float(position.entry),
            "stop_loss": float(position.stop_loss),
            "first_tp": first_tp,
            "final_tp": final_tp,
            "risk_price": risk,
            "risk_pct": (risk / abs(float(position.entry)) * 100.0) if position.entry else 0.0,
            "tp1_r": (abs(first_tp - position.entry) / risk) if first_tp is not None and risk > 0 else None,
            "final_r": (abs(final_tp - position.entry) / risk) if final_tp is not None and risk > 0 else None,
            "leverage": int(position.leverage),
        }

    def add_trade(self, position, exit_price, pnl, reason, fees=0.0):
        forensic = dict(getattr(position, "forensic", {}) or {})
        forensic["entry"] = self._entry_forensics(position)
        forensic["setup"] = str(getattr(position, "setup", "") or "")
        forensic["score_10"] = float(getattr(position, "score_10", 0.0))
        forensic["score_components"] = dict(getattr(position, "score_components", {}) or {})
        forensic["exit"] = {
            "price": float(exit_price),
            "reason": str(reason),
            "pnl": float(pnl),
            "fees_total": float(fees),
            "closed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }
        self.db.execute(
            """INSERT OR REPLACE INTO trades
               (id,symbol,side,entry,exit,pnl,opened_at,closed_at,reason,fees,
                setup,score_10,score_components,forensic)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                position.id, position.symbol, position.side, position.entry,
                exit_price, pnl, position.opened_at.isoformat(),
                forensic["exit"]["closed_at"], reason, fees,
                getattr(position, "setup", ""),
                float(getattr(position, "score_10", 0.0)),
                json.dumps(getattr(position, "score_components", {}) or {}),
                json.dumps(forensic),
            )
        )
        self.db.commit()

    def all_history(self):
        c = self.db.execute("SELECT * FROM trades ORDER BY closed_at ASC")
        cols = [x[0] for x in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]

    def history(self, limit=20):
        c = self.db.execute("SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (int(limit),))
        cols = [x[0] for x in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]

    def statistics(self):
        rows = self.all_history()
        pnls = [float(x["pnl"]) for x in rows]
        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        curve = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in pnls:
            curve += pnl
            peak = max(peak, curve)
            max_drawdown = max(max_drawdown, peak - curve)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

        by_setup = {}
        for row in rows:
            setup = str(row.get("setup") or "UNKNOWN")
            bucket = by_setup.setdefault(setup, {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0})
            bucket["trades"] += 1
            bucket["net_pnl"] += float(row["pnl"])
            if float(row["pnl"]) > 0:
                bucket["wins"] += 1
            elif float(row["pnl"]) < 0:
                bucket["losses"] += 1
        for bucket in by_setup.values():
            bucket["win_rate"] = bucket["wins"] / bucket["trades"] * 100 if bucket["trades"] else 0.0

        score_bands = {}
        for row in rows:
            score = float(row.get("score_10") or 0.0)
            band = "7.5-8.0" if score < 8.0 else "8.0-8.5" if score < 8.5 else "8.5-9.0" if score < 9.0 else "9.0-10"
            b = score_bands.setdefault(band, {"trades": 0, "wins": 0, "net_pnl": 0.0})
            b["trades"] += 1
            b["net_pnl"] += float(row["pnl"])
            if float(row["pnl"]) > 0:
                b["wins"] += 1
        for band in score_bands.values():
            band["win_rate"] = band["wins"] / band["trades"] * 100 if band["trades"] else 0.0

        reasons = {}
        for row in rows:
            try:
                forensic = json.loads(row.get("forensic") or "{}")
            except Exception:
                forensic = {}
            exit_data = forensic.get("exit", {})
            reason = str(exit_data.get("reason") or row.get("reason") or "UNKNOWN")
            b = reasons.setdefault(reason, {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0})
            b["trades"] += 1
            b["net_pnl"] += float(row["pnl"])
            if float(row["pnl"]) > 0:
                b["wins"] += 1
            elif float(row["pnl"]) < 0:
                b["losses"] += 1
        for b in reasons.values():
            b["win_rate"] = b["wins"] / b["trades"] * 100 if b["trades"] else 0.0

        self_analysis = {
            "by_setup": by_setup,
            "by_score": score_bands,
            "by_exit_reason": reasons,
            "sample_warning": "Need a larger out-of-sample sample before changing thresholds automatically.",
        }

        return {
            "trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": profit_factor,
            "avg_win": (gross_profit / len(wins)) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
            "net_pnl": sum(pnls),
            "max_drawdown": max_drawdown,
            "total_fees": sum(float(x.get("fees") or 0.0) for x in rows),
            "self_analysis": self_analysis,
        }

    def save_position(self, p):
        forensic = dict(getattr(p, "forensic", {}) or {})
        forensic["entry"] = self._entry_forensics(p)
        forensic["setup"] = str(getattr(p, "setup", "") or "")
        forensic["score_10"] = float(getattr(p, "score_10", 0.0))
        forensic["score_components"] = dict(getattr(p, "score_components", {}) or {})
        self.db.execute(
            """INSERT OR REPLACE INTO positions
               (id,symbol,side,entry,quantity,stop_loss,take_profits,opened_at,leverage,
                pnl,status,initial_quantity,realized_pnl,tp_index,last_price,initial_stop_loss,
                entry_fee,fees,setup,score_10,score_components,forensic)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p.id, p.symbol, p.side, p.entry, p.quantity, p.stop_loss,
             json.dumps(p.take_profits), p.opened_at.isoformat(), p.leverage, p.pnl,
             p.status, p.initial_quantity, p.realized_pnl, p.tp_index, p.last_price,
             p.initial_stop_loss, p.entry_fee, p.fees, getattr(p, "setup", ""),
             float(getattr(p, "score_10", 0.0)),
             json.dumps(getattr(p, "score_components", {}) or {}),
             json.dumps(forensic))
        )
        self.db.commit()

    def load_positions(self):
        from app.models import Position
        c = self.db.execute("SELECT * FROM positions WHERE status='OPEN'")
        out = {}
        for r in c.fetchall():
            (pid, symbol, side, entry, quantity, stop_loss, take_profits, opened_at, leverage,
             pnl, status, initial_quantity, realized_pnl, tp_index, last_price, initial_stop_loss,
             entry_fee, fees, setup, score_10, score_components, forensic) = r
            out[pid] = Position(
                pid, symbol, side, float(entry), float(quantity), float(stop_loss),
                json.loads(take_profits), datetime.fromisoformat(opened_at), int(leverage),
                float(pnl), status, float(initial_quantity), float(realized_pnl), int(tp_index),
                float(last_price), float(initial_stop_loss), float(entry_fee), float(fees),
                str(setup or ""), float(score_10 or 0.0),
                json.loads(score_components or "{}"), json.loads(forensic or "{}")
            )
        return out

    def delete_position(self, position_id):
        self.db.execute("DELETE FROM positions WHERE id=?", (position_id,))
        self.db.commit()

    def clear_positions(self):
        self.db.execute("DELETE FROM positions")
        self.db.commit()

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
        self.db.execute("DELETE FROM positions")
        self.db.commit()
