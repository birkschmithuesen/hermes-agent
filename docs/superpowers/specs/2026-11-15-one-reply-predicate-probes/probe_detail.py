"""Read-only probe 4: detail of the stays where H, LB and U disagree (t_52540fe8)."""
import glob, json, os, sqlite3

AUDIT = ("BLOCKED: ", "SCHEDULED: ", "UNBLOCK: ", "CHANGES REQUESTED: ")
EXIT = {"specified", "promoted", "decomposed", "status", "unblocked", "claimed", "completed",
        "archived", "blocked", "scheduled", "reopened", "dependency_wait", "ready", "spawned"}
paths = sorted(p for p in glob.glob(os.path.expanduser("~/.hermes/kanban/boards/*/kanban.db")) if os.path.getsize(p) > 0)

def norm(s):
    return str(s or "").strip().lower()

for p in paths:
    board = p.split("/")[-2]
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    for lp in c.execute("SELECT id, task_id FROM task_events WHERE kind='block_loop_detected'").fetchall():
        asg = (c.execute("SELECT assignee FROM tasks WHERE id=?", (lp["task_id"],)).fetchone() or {"assignee": None})["assignee"]
        h = lb = u = None
        trail = []
        exit_kind = None
        for ev in c.execute("SELECT kind, payload, created_at FROM task_events WHERE task_id=? AND id>? ORDER BY id",
                            (lp["task_id"], lp["id"])):
            if ev["kind"] != "commented":
                if ev["kind"] in EXIT or ev["kind"] == "block_loop_detected":
                    exit_kind = (ev["kind"], (json.loads(ev["payload"] or "null") or {}).get("author") if ev["payload"] else None)
                    break
                continue
            au = str((json.loads(ev["payload"] or "null") or {}).get("author") or "").strip()
            bodies = [r[0] for r in c.execute(
                "SELECT body FROM task_comments WHERE task_id=? AND author=? AND created_at BETWEEN ? AND ?",
                (lp["task_id"], au, int(ev["created_at"]) - 1, int(ev["created_at"])))]
            not_audit = any(not str(b).startswith(AUDIT) for b in bodies)
            trail.append((au, (bodies or [""])[0][:45]))
            if h is None and norm(au) and norm(au) not in {norm(asg), "auto-decomposer"}:
                h = au
            if lb is None and au == "birk" and au != asg:
                lb = au
            if u is None and au == "birk" and au != asg and not_audit:
                u = au
        if (bool(h), bool(lb), bool(u)) not in {(True, True, True), (False, False, False)}:
            print(board, lp["task_id"], "assignee=", asg, "H/LB/U=", h, lb, u, "exit=", exit_kind)
            for t in trail:
                print("     ", t)
