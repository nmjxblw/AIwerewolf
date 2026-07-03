"""Test database read/write operations."""

import sys
import os
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["AIWEREWOLF_SQLITE_PATH"] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "test_db_check.db"
)

from backend.db.database import SessionLocal, init_db
from backend.db.models import Game, Player, AgentDecision, PromptSnapshot
from backend.db.persist import save_prompt_snapshot, export_game_thought_process

# Step 1: Init DB
init_db()
print("1. DB initialized OK")

GID = str(uuid.uuid4())
PID = str(uuid.uuid4())

db = SessionLocal()
try:
    # Create test game
    game = Game(
        id=GID,
        status="finished",
        winner="wolf",
        current_day=2,
        seed="7",
        created_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db.add(game)

    # Create test player
    player = Player(
        id=PID,
        game_id=GID,
        name="测试玩家",
        role="werewolf",
        seat_no=3,
    )
    db.add(player)

    # Create test decision
    did = str(uuid.uuid4())
    dec = AgentDecision(
        id=did,
        game_id=GID,
        player_id=PID,
        day=1,
        phase="TALK",
        raw_output="测试发言",
        parsed_action={"action_type": "talk", "speech": "我是预言家", "target_id": "1"},
        latency_ms=500,
    )
    db.add(dec)
    db.commit()
    print("2. Test data created OK (game + player + decision)")

    # Test save_prompt_snapshot
    save_prompt_snapshot(
        game_id=GID,
        player_id=PID,
        day=1,
        phase="TALK",
        request="speech",
        decision_id=did,
        system_prompt="你是狼人，在玩狼人杀",
        user_prompt='{"phase":"DAY_SPEECH","speakers":["1","2"]}',
        response='{"speech": "我是预言家，昨晚查了3号"}',
        model_name="deepseek-v4",
        provider="deepseek",
        prompt_tokens=1200,
        completion_tokens=150,
    )
    print("3. PromptSnapshot saved OK")

    # Verify read
    snap = db.query(PromptSnapshot).filter(PromptSnapshot.game_id == GID).first()
    assert snap is not None, "Read failed!"
    assert snap.day == 1
    assert snap.phase == "TALK"
    assert snap.system_prompt == "你是狼人，在玩狼人杀"
    assert snap.prompt_tokens == 1200
    assert snap.completion_tokens == 150
    print(
        f"4. Read verified OK: day={snap.day}, phase={snap.phase}, tokens={snap.prompt_tokens}/{snap.completion_tokens}"
    )

    # Test export
    result = export_game_thought_process(GID)
    assert result is not None, "Export returned None!"
    assert (
        result["total_decisions"] >= 1
    ), f"Expected >=1 entries, got {result['total_decisions']}"
    e = result["entries"][0]
    assert e["day"] == 1
    assert e["action_type"] == "talk"
    assert "预言家" in e["speech"]
    print(
        f"5. Export verified OK: {result['total_decisions']} entries, 1st speech={e['speech'][:20]}..."
    )

    # Test save without decision (should still work)
    save_prompt_snapshot(
        game_id=GID,
        player_id=PID,
        day=2,
        phase="NIGHT_GUARD_ACTION",
        request="guard",
        system_prompt="你是守卫",
        user_prompt="请选择守护目标",
        response='{"target": "4"}',
    )
    snapshot_count = (
        db.query(PromptSnapshot).filter(PromptSnapshot.game_id == GID).count()
    )
    assert snapshot_count == 2, f"Expected 2 snapshots, got {snapshot_count}"
    print(f"6. Second snapshot OK (total: {snapshot_count})")

    print("\n===== ALL 6 TESTS PASSED =====")

finally:
    db.rollback()
    for t in [PromptSnapshot, AgentDecision, Player, Game]:
        db.query(t).filter(
            t.game_id == GID if hasattr(t, "game_id") else t.id == GID
        ).delete()
    db.commit()
    db.close()

# Cleanup test DB file
test_db = os.environ["AIWEREWOLF_SQLITE_PATH"]
if os.path.exists(test_db):
    os.unlink(test_db)
    print(f"Cleanup: removed {test_db}")
