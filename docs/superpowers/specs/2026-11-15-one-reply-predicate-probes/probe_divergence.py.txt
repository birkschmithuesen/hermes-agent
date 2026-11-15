"""Read-only probe 2: post-loop comments that _hold counts as replies but the LB predicate
(release_authors=('birk',), audit-note filter) would not (t_52540fe8)."""
import glob, json, os, sqlite3, collections

AUDIT = ("BLOCKED: ", "SCHEDULED: ", "UNBLOCK: ", "CHANGES REQUESTED: ")
paths = sorted(p for p in glob.glob(os.path.expanduser("~/.hermes/kanban/boards/*/kanban.db")) if os.path.getsize(p) > 0)
paths.append(os.path.expanduser("~/.hermes/kanban.db"))

def norm(s):
    return str(s or "").strip().lower()

loops_total = 0
first_reply_by = collections.Counter()      # who would release under _hold (latest-wins there; we take any)
cards_hold_replied = 0
cards_lb_replied = 0
cards_diverge = []
audit_only = []
robo_rows = []
for p in paths:
    board = p.split("/")[-2]
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    for r in c.execute("SELECT task_id, created_at, substr(body,1,60) b FROM task_comments WHERE author='Robo'"):
        robo_rows.append((board, r["task_id"], r["created_at"], r["b"]))
    loops = c.execute("SELECT task_id, MAX(id) lid FROM task_events WHERE kind='block_loop_detected' GROUP BY task_id").fetchall()
    for lp in loops:
        loops_total += 1
        asg = c.execute("SELECT assignee FROM tasks WHERE id=?", (lp["task_id"],)).fetchone()
        asg = asg["assignee"] if asg else None
        hold_auth, lb_auth = [], []
        for ev in c.execute("SELECT payload, created_at FROM task_events WHERE task_id=? AND kind='commented' AND id>? ORDER BY id", (lp["task_id"], lp["lid"])):
            au = str((json.loads(ev["payload"] or "null") or {}).get("author") or "").strip()
            if norm(au) and norm(au) not in {norm(asg), "auto-decomposer"}:
                hold_auth.append(au)
                bodies = [row[0] for row in c.execute(
                    "SELECT body FROM task_comments WHERE task_id=? AND author=? AND created_at BETWEEN ? AND ?",
                    (lp["task_id"], au, int(ev["created_at"]) - 1, int(ev["created_at"])))]
                is_audit = bool(bodies) and all(str(b).startswith(AUDIT) for b in bodies)
                if is_audit:
                    audit_only.append((board, lp["task_id"], au, bodies[0][:40]))
                if au == "birk" and bodies and not is_audit:
                    lb_auth.append(au)
        if hold_auth:
            cards_hold_replied += 1
        if lb_auth:
            cards_lb_replied += 1
        if bool(hold_auth) != bool(lb_auth):
            cards_diverge.append((board, lp["task_id"], asg, sorted(set(hold_auth))))
        for a in set(hold_auth):
            first_reply_by[a] += 1

print("cards with block_loop_detected:", loops_total)
print("cards with a post-loop reply per _hold (case-insens., any non-worker):", cards_hold_replied)
print("cards with a post-loop reply per LB (exact, release_authors=birk, no audit note):", cards_lb_replied)
print("authors counted by _hold after a loop (cards):", dict(first_reply_by))
print("audit-note-only comments counted by _hold:", audit_only)
print("divergent cards:", len(cards_diverge))
for d in cards_diverge:
    print("  ", d)
print("'Robo' rows:", robo_rows)
