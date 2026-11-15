"""Read-only probe 3: comments written DURING a loop-breaker triage stay (between the latest
block_loop_detected and the first status-changing event after it), judged by
  H  = BLH _hold today (case-insensitive, any non-worker, no body filter)
  LB = releasing_comment_author today (exact, release_authors=('birk',), no body filter)
  U  = unified (exact, release_authors=('birk',), + CLI audit-note body filter)
(t_52540fe8)."""
import glob, json, os, sqlite3, collections

AUDIT = ("BLOCKED: ", "SCHEDULED: ", "UNBLOCK: ", "CHANGES REQUESTED: ")
EXIT = {"specified", "promoted", "decomposed", "status", "unblocked", "claimed", "completed",
        "archived", "blocked", "scheduled", "reopened", "dependency_wait", "ready", "spawned"}
paths = sorted(p for p in glob.glob(os.path.expanduser("~/.hermes/kanban/boards/*/kanban.db")) if os.path.getsize(p) > 0)
paths.append(os.path.expanduser("~/.hermes/kanban.db"))

def norm(s):
    return str(s or "").strip().lower()

kinds_after = collections.Counter()
stays = 0
verdict = collections.Counter()
rows_out = []
for p in paths:
    board = p.split("/")[-2]
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    for lp in c.execute("SELECT id, task_id FROM task_events WHERE kind='block_loop_detected'").fetchall():
        stays += 1
        asg = c.execute("SELECT assignee FROM tasks WHERE id=?", (lp["task_id"],)).fetchone()
        asg = asg["assignee"] if asg else None
        h = lb = u = None
        for ev in c.execute("SELECT kind, payload, created_at FROM task_events WHERE task_id=? AND id>? ORDER BY id",
                            (lp["task_id"], lp["id"])):
            if ev["kind"] != "commented":
                kinds_after[ev["kind"]] += 1
                if ev["kind"] in EXIT or ev["kind"] == "block_loop_detected":
                    break
                continue
            au = str((json.loads(ev["payload"] or "null") or {}).get("author") or "").strip()
            bodies = [r[0] for r in c.execute(
                "SELECT body FROM task_comments WHERE task_id=? AND author=? AND created_at BETWEEN ? AND ?",
                (lp["task_id"], au, int(ev["created_at"]) - 1, int(ev["created_at"])))]
            not_audit = any(not str(b).startswith(AUDIT) for b in bodies)
            if h is None and norm(au) and norm(au) not in {norm(asg), "auto-decomposer"}:
                h = au
            if lb is None and au and au not in ("auto-decomposer", asg or "") and au == "birk":
                lb = au
            if u is None and au and au not in ("auto-decomposer", asg or "") and au == "birk" and not_audit:
                u = au
            if (lb and not not_audit and au == "birk"):
                rows_out.append((board, lp["task_id"], au, (bodies or [""])[0][:50]))
        verdict[(bool(h), bool(lb), bool(u))] += 1

print("loop stays:", stays)
print("verdict counts (H, LB, U) -> stays:", dict(verdict))
print("audit-note comments by a release author inside a stay (LB counts them, U not):", rows_out)
print("non-comment event kinds seen after a loop (first ones):", kinds_after.most_common(20))
