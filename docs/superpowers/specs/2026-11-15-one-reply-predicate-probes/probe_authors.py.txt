"""Read-only probe: author spellings in kanban comments across all boards (t_52540fe8)."""
import glob, json, os, sqlite3, collections

paths = sorted(p for p in glob.glob(os.path.expanduser("~/.hermes/kanban/boards/*/kanban.db")) if os.path.getsize(p) > 0)
paths.append(os.path.expanduser("~/.hermes/kanban.db"))

def norm(s):
    return str(s or "").strip().lower()

comment_authors = collections.Counter()
event_authors = collections.Counter()
assignees = collections.Counter()
per_board = {}
# case-only mismatches between a comment author and the card's assignee
assignee_case_mismatch = collections.Counter()
# after block_loop_detected: comments whose decision would differ between _norm and exact
loop_divergent = []
loop_comments = 0
for p in paths:
    board = p.split("/")[-2] if p.endswith("boards/" + p.split("/")[-2] + "/kanban.db") else "root"
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("SELECT c.task_id, c.author, t.assignee FROM task_comments c LEFT JOIN tasks t ON t.id=c.task_id").fetchall()
    except sqlite3.Error as e:
        print("skip", p, e); continue
    per_board[board] = len(rows)
    for r in rows:
        comment_authors[r["author"]] += 1
        a, s = r["author"], r["assignee"]
        if a and s and a != s and norm(a) == norm(s):
            assignee_case_mismatch[(a, s)] += 1
    for r in c.execute("SELECT assignee FROM tasks"):
        assignees[r["assignee"]] += 1
    for r in c.execute("SELECT payload FROM task_events WHERE kind='commented'"):
        try:
            event_authors[(json.loads(r["payload"] or "null") or {}).get("author")] += 1
        except Exception:
            event_authors["<badjson>"] += 1
    # comments after a block_loop_detected event, judged both ways
    loops = c.execute("SELECT task_id, MAX(id) lid FROM task_events WHERE kind='block_loop_detected' GROUP BY task_id").fetchall()
    for lp in loops:
        asg = c.execute("SELECT assignee FROM tasks WHERE id=?", (lp["task_id"],)).fetchone()
        asg = asg["assignee"] if asg else None
        for ev in c.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='commented' AND id>?", (lp["task_id"], lp["lid"])):
            au = (json.loads(ev["payload"] or "null") or {}).get("author")
            loop_comments += 1
            ci = bool(norm(au)) and norm(au) not in {norm(asg), "auto-decomposer"}
            ex_raw = str(au or "").strip()
            ex = bool(ex_raw) and ex_raw not in ("auto-decomposer", asg or "")
            if ci != ex:
                loop_divergent.append((board, lp["task_id"], au, asg))

groups = collections.defaultdict(set)
for a in list(comment_authors) + list(event_authors) + list(assignees):
    groups[norm(a)].add(a)
variants = {k: sorted(map(repr, v)) for k, v in groups.items() if len(v) > 1}
nonlower = {a: n for a, n in comment_authors.items() if a and a != a.strip().lower()}
ws = {a: n for a, n in comment_authors.items() if a and a != a.strip()}

print("boards:", per_board)
print("total comments:", sum(comment_authors.values()), "distinct authors:", len(comment_authors))
print("distinct commented-event authors:", len(event_authors))
print("authors (comments) not lowercase:", nonlower)
print("authors with surrounding whitespace:", ws)
print("spellings differing only in case/whitespace (comments+events+assignees):", variants)
print("comment author == assignee only case-insensitively:", dict(assignee_case_mismatch))
print("comments after block_loop_detected:", loop_comments, "divergent (_norm vs exact):", loop_divergent)
print("top comment authors:", comment_authors.most_common(25))
print("non-lowercase assignees:", {a: n for a, n in assignees.items() if a and a != a.lower()})
