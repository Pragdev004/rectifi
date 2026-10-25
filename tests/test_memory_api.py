"""The Memory page's API: list, add, edit, delete, forget everything, and the category rules you taught."""

from api_support import make_client

from rectifi import db


def test_add_list_edit_delete():
    c = make_client()
    assert c.get("/api/memory").json() == {"memories": [], "rules": []}
    made = c.post("/api/memory", json={"kind": "preference", "text": "I prefer short answers"})
    assert made.status_code == 201
    item = made.json()
    assert item["kind"] == "preference" and item["text"] == "I prefer short answers" and item["created_at"] and len(item["id"]) == 32
    assert c.get("/api/memory").json()["memories"] == [item]
    edited = c.patch(f"/api/memory/{item['id']}", json={"text": "I prefer very short answers", "kind": "fact"}).json()
    assert edited["text"] == "I prefer very short answers" and edited["kind"] == "fact" and edited["created_at"] == item["created_at"]
    assert c.delete(f"/api/memory/{item['id']}").json() == {"deleted": True}
    assert c.delete(f"/api/memory/{item['id']}").status_code == 404
    assert c.get("/api/memory").json()["memories"] == []


def test_input_is_validated_and_account_numbers_are_masked():
    c = make_client()
    assert c.post("/api/memory", json={"kind": "mood", "text": "x y"}).status_code == 422
    assert c.post("/api/memory", json={"kind": "fact", "text": "  "}).status_code == 422
    assert c.post("/api/memory", json={"kind": "fact", "text": "x" * 501}).status_code == 422
    saved = c.post("/api/memory", json={"kind": "fact", "text": "account 0012345678901"}).json()
    assert "0012345678901" not in saved["text"]
    assert c.patch("/api/memory/not-a-key", json={"text": "x"}).status_code == 404
    assert c.patch("/api/memory/" + "a" * 32, json={"text": "x"}).status_code == 404
    assert c.patch(f"/api/memory/{saved['id']}", json={"kind": "mood"}).status_code == 422


def test_forget_everything_clears_memories_but_not_taught_rules():
    c = make_client()
    for i in range(3):
        c.post("/api/memory", json={"kind": "fact", "text": f"thing number {i}"})
    with db.connect() as conn:
        cat = conn.execute("SELECT id FROM categories WHERE name = 'Health'").fetchone()["id"]
        conn.execute("INSERT INTO merchant_rules (pattern, category_id, source) VALUES ('cult fit', %s, 'learned')", (cat,))
    assert c.delete("/api/memory").json() == {"deleted": 3}
    body = c.get("/api/memory").json()
    assert body["memories"] == [] and [r["pattern"] for r in body["rules"]] == ["cult fit"]


def test_taught_rules_can_be_removed_but_built_in_ones_cannot():
    c = make_client()
    with db.connect() as conn:
        cat = conn.execute("SELECT id FROM categories WHERE name = 'Health'").fetchone()["id"]
        taught = conn.execute("INSERT INTO merchant_rules (pattern, category_id, source) VALUES ('cult fit', %s, 'learned') RETURNING id", (cat,)).fetchone()["id"]
        built_in = conn.execute("SELECT id FROM merchant_rules WHERE source = 'default' LIMIT 1").fetchone()["id"]
    rules = c.get("/api/memory").json()["rules"]
    assert rules == [{"id": taught, "pattern": "cult fit", "category": "Health", "created_at": rules[0]["created_at"]}]
    assert c.delete(f"/api/memory/rules/{built_in}").status_code == 404
    assert c.delete(f"/api/memory/rules/{taught}").json() == {"deleted": True}
    assert c.get("/api/memory").json()["rules"] == []
    assert c.delete("/api/memory/rules/abc").status_code == 404


def test_the_memory_routes_keep_the_api_guards():
    bare = make_client(headers={})
    assert bare.get("/api/memory").status_code == 403 and bare.delete("/api/memory").status_code == 403
