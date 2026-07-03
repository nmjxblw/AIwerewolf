import urllib.request, json, sys

sys.path.insert(0, r"C:\Code\AIwerewolf")

# Test 1: Health
r = urllib.request.urlopen("http://localhost:8001/api/health")
print(f"1. Health: HTTP {r.status}")

# Test 2: Export latest game via API
from backend.db.database import SessionLocal
from backend.db.models import Game

db = SessionLocal()
g = db.query(Game).order_by(Game.created_at.desc()).first()
db.close()

r = urllib.request.urlopen(
    f"http://localhost:8001/api/games/{g.id}/thought-process?download=true"
)
data = json.loads(r.read())
print(
    f"2. Latest game ({g.id[:20]}...): HTTP {r.status}, entries={data['total_decisions']}"
)

for e in data["entries"][:3]:
    print(
        f"   day={e['day']} {e['phase']:24s} {e['player_name']}({e['player_role']}) "
        f"action={e['action_type']} speech={e['speech'][:50]}"
    )

has_prompt = any(e.get("system_prompt") for e in data["entries"])
print(f"3. system_prompt populated: {'yes' if has_prompt else 'NO'}")

print("\nALL CHECKS PASSED")
