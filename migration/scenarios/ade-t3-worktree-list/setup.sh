python3 - <<'EOF'
import json, os, sqlite3
d = os.environ["W"] + "/t3/userdata"; os.makedirs(d, exist_ok=True)
db = sqlite3.connect(d + "/statev2.sqlite")
db.execute("create table projection_projects (project_id text, title text, workspace_root text, deleted_at text)")
db.executemany("insert into projection_projects values (?,?,?,?)", [
    ("prj-1", "widgets", "/nonexistent/widgets", None), ("prj-old", "gone", "/nonexistent/gone", "2019-05-01T00:00:00Z"),
    ("prj-2", "feat", "/nonexistent/widgets-feat", None), ("prj-3", "tools", "/nonexistent/tools", None)])
db.commit(); db.close()
json.dump({"manifest": {"currentModels": {"claudeAgent": ["claude-opus-x", "claude-sonnet-x"], "codex": ["gpt-x"]}}},
          open(d + "/model-manifest.json", "w"))
EOF
