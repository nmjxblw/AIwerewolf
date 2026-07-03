import sys

sys.path.insert(0, r"C:\Code\AIwerewolf")
from backend.db.database import SessionLocal
from backend.db.models import Game, PromptSnapshot
from backend.db.persist import export_game_thought_process

db = SessionLocal()

# Test export for the game with snapshots
game_id = "d30c637e-1a03-40b7-be82-778598e94995"
result = export_game_thought_process(game_id)
if result:
    print(f"Export OK: total_decisions={result['total_decisions']}")
    for e in result["entries"][:3]:
        print(
            f"  day={e['day']} phase={e['phase']} player={e['player_name']} "
            f"action={e['action_type']} speech={e['speech'][:40]}..."
        )
else:
    print("Export returned None!")

# Also list all games
print()
print("=== All games ===")
games = db.query(Game.id, Game.status).order_by(Game.created_at.desc()).limit(20).all()
for g in games:
    snaps = db.query(PromptSnapshot).filter(PromptSnapshot.game_id == g.id).count()
    print(f"  {g.id} status={g.status} snaps={snaps}")
db.close()
